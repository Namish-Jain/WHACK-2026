"""Gemini REST integration with aggregate inputs and locally bound numerical facts."""
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from analysis.hypotheses import HypothesisConfig, run_hypothesis_tests
from analyze import render_report
from data_io import write_json
from history import canonical_account

DEFAULT_MODEL = 'gemini-3.8-flash'
PROMPT_PATH = Path(__file__).with_name('prompt.txt')
PLACEHOLDER = re.compile(r'\{\{([a-z_]+)\}\}')
FEATURE_LABELS = {'benford': 'First-digit pattern', 'ratio': 'Following imbalance',
                  'activity': 'Low posting activity', 'new_account': 'Recently created accounts',
                  'profile': 'Sparse profiles'}
RATE_COMPONENTS = {'ratio': 'extreme_ratio_rate', 'activity': 'low_activity_rate',
                   'new_account': 'new_account_rate', 'profile': 'incomplete_profile_rate'}
RESPONSE_SCHEMA = {'type': 'OBJECT', 'properties': {
    'summary': {'type': 'STRING'},
    'strongest_signals': {'type': 'ARRAY', 'items': {'type': 'OBJECT', 'properties': {
        'feature_id': {'type': 'STRING', 'enum': list(FEATURE_LABELS)}, 'explanation': {'type': 'STRING'}},
        'required': ['feature_id', 'explanation']}},
    'hypothesis_tests': {'type': 'STRING'}, 'uncertainty': {'type': 'STRING'},
    'limitations': {'type': 'ARRAY', 'items': {'type': 'STRING'}},
    'next_steps': {'type': 'ARRAY', 'items': {'type': 'STRING'}}},
    'required': ['summary', 'strongest_signals', 'hypothesis_tests', 'uncertainty', 'limitations', 'next_steps']}


class GeminiError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GeminiError('Gemini redirected the request; credentials were not forwarded.')


def format_number(value, digits=2):
    return 'unavailable' if value is None else f'{value:.{digits}f}'


def test_sentence(test, alpha):
    if test['p_value_holm'] is None:
        return 'Not tested: ' + test['reason']
    decision = 'the result differs from the stated reference under the test assumptions' if test['reject_reference_at_alpha'] else 'the test did not find enough evidence of a difference from the stated reference'
    p = test['p_value_holm']
    ptext = 'at most 1e-300' if p <= 1e-300 else f'{p:.6g}'
    return f'{decision} (adjusted p = {ptext}, decision cutoff = {alpha:g}); this does not determine whether followers are genuine'


def prepare_evidence(report, hypothesis_config=None):
    """Allowlist aggregate fields. Never forward raw profile rows, paths, bios or keys."""
    # Recompute from aggregates so old saved runs also receive the new test layer.
    if hypothesis_config is None:
        stored = report.get('hypothesis_testing', {}).get('config', {})
        hypothesis_config = HypothesisConfig(**stored)
    tests = run_hypothesis_tests(report['benford'], report['features'], hypothesis_config)
    score = report['scoring']
    included = [(name, c) for name, c in score['components'].items() if c['included'] and c['contribution_points'] > 0]
    top = [name for name, _ in sorted(included, key=lambda item: -item[1]['contribution_points'])[:3]]
    interval = report['bootstrap'].get('intervals', {}).get('anomaly_score')
    interval_text = (f"{format_number(interval['lower'])}–{format_number(interval['upper'])} (within-sample bootstrap sensitivity)"
                     if interval and interval['lower'] is not None and interval['upper'] is not None else 'unavailable; no usable bootstrap score interval')
    target = report.get('collection', {}).get('target_input') or 'unspecified target'
    facts = {'target_account': target, 'score': format_number(score['anomaly_score']) + (' / 100' if score['anomaly_score'] is not None else ''),
             'severity': score['severity'], 'sample_size': str(report['sample_size']), 'score_interval': interval_text,
             'benford_mad': format_number(report['benford']['mad'], 6),
             'benford': f"an average first-digit gap of {format_number(report['benford']['mad'], 6)} from {report['benford']['n_valid']} positive follower counts",
             'alpha': format_number(tests['alpha'])}
    cfg = report['config']
    facts.update(low_activity_rule=f"{cfg['low_activity_max_posts']} or fewer posts",
                 ratio_rule=f"a following-to-follower ratio of at least {cfg['extreme_smoothed_ratio']:g}, after adding one to both counts",
                 new_account_rule=f"at most {cfg['new_account_days']:g} days old",
                 profile_rule=f"completeness at most {cfg['incomplete_max_fraction']:.3g}, based on an avatar, a bio and any posts")
    for component, key in RATE_COMPONENTS.items():
        feature = report['features'][key]
        facts[key] = (f"{100 * feature['value']:.2f}% ({feature['n_flagged']} of {feature['n_valid']} usable profiles)"
                      if feature['value'] is not None else 'unavailable')
        facts[component] = facts[key]
    for component, values in score['components'].items():
        facts[component + '_contribution'] = f"{values['contribution_points']:.2f} score points"
    for test in tests['tests']:
        facts['test_' + test['id']] = test_sentence(test, tests['alpha'])
    quality = report.get('input_quality') or {}
    return {'schema_version': 'explanation-input-v1', 'target_account': target,
            'as_of': report['as_of'], 'dataset_kind': report.get('dataset_kind', 'collection_snapshot'),
            'sample_size': report['sample_size'],
            'sampling': {k: report.get('collection', {}).get(k) for k in ('sampling_method', 'reported_followers_count', 'stop_reason')},
            'data_quality': {k: quality.get(k) for k in ('duplicates_removed', 'missing_values_by_field', 'invalid_values_by_field')},
            'scoring': copy.deepcopy(score), 'thresholds_and_weights': copy.deepcopy(report['config']),
            'features': {k: copy.deepcopy(report['features'][k]) for k in RATE_COMPONENTS.values()},
            'benford': {k: report['benford'][k] for k in ('mad', 'n_valid', 'n_zero_excluded', 'log10_span')},
            'bootstrap': copy.deepcopy(report['bootstrap']), 'hypothesis_testing': tests,
            'limitations': list(report.get('limitations', [])), 'top_contributors': top, 'facts': facts}


def validate_narrative(value, evidence):
    required = set(RESPONSE_SCHEMA['required'])
    if not isinstance(value, dict) or set(value) != required:
        raise GeminiError('Gemini response did not match the explanation schema.')
    texts = []
    for name in ('summary', 'hypothesis_tests', 'uncertainty'):
        if not isinstance(value[name], str) or not value[name].strip():
            raise GeminiError('Gemini omitted a required explanation.')
        texts.append(value[name])
    for name, minimum, maximum in [('limitations', 2, 5), ('next_steps', 1, 3)]:
        array = value[name]
        if not isinstance(array, list) or not minimum <= len(array) <= maximum or any(not isinstance(t, str) or not t.strip() for t in array):
            raise GeminiError('Gemini returned invalid explanation items.')
        texts.extend(array)
    signals = value['strongest_signals']
    if not isinstance(signals, list) or len(signals) != len(evidence['top_contributors']):
        raise GeminiError('Gemini omitted or invented a strongest signal.')
    seen = set()
    for item in signals:
        if not isinstance(item, dict) or set(item) != {'feature_id', 'explanation'} or not isinstance(item['explanation'], str):
            raise GeminiError('Gemini returned an invalid signal.')
        feature = item['feature_id']
        if feature not in evidence['top_contributors'] or feature in seen:
            raise GeminiError('Gemini cited an unsupported or duplicate signal.')
        seen.add(feature)
        texts.append(item['explanation'])
    for index, text in enumerate(texts):
        if not text.strip() or len(text) > 3500:
            raise GeminiError('Gemini explanation length was invalid.')
        references = PLACEHOLDER.findall(text)
        if any(ref not in evidence['facts'] for ref in references):
            raise GeminiError('Gemini cited an unknown numerical fact.')
        remainder = PLACEHOLDER.sub('', text)
        if re.search(r'\d|[{}<>]|https?://|\]\(', remainder):
            raise GeminiError(f'Gemini inserted a numerical literal or unsupported formatting in text item {index}; use only verified placeholders and plain prose.')
    # Authoritative score, interval and test table are always rendered locally, even when prose omits a placeholder.
    if sum(map(len, texts)) > 11000:
        raise GeminiError('Gemini explanation was too long.')
    return value


def request_gemini(evidence, api_key=None, model=None, opener=None, _repair=None):
    api_key = api_key or os.getenv('GEMINI_API_KEY')
    model = model or os.getenv('GEMINI_MODEL') or DEFAULT_MODEL
    if not api_key or not api_key.strip():
        raise GeminiError('Set GEMINI_API_KEY locally, or use --prepare-only to inspect the request without calling Gemini.')
    if not re.fullmatch(r'gemini-[A-Za-z0-9._-]+', model):
        raise GeminiError('Invalid Gemini model name.')
    body = make_request(evidence)
    if _repair is not None:
        previous, issue = _repair
        body['contents'].extend([
            {'role': 'model', 'parts': [{'text': previous}]},
            {'role': 'user', 'parts': [{'text': 'The local validator rejected that answer: ' + issue +
                ' Return a corrected full JSON answer with qualitative prose only. Do not repeat any numbers or use any placeholders, including for thresholds, sample sizes and p-values. '
                'The application inserts every measured value. Do not write digits, comparison symbols, HTML, or links. Preserve the statistical meaning and use simple language.'}]}])
    request = Request(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
                      data=json.dumps(body, allow_nan=False).encode(),
                      headers={'Content-Type': 'application/json', 'x-goog-api-key': api_key.strip()}, method='POST')
    opener = opener or build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=45) as response:
            raw = json.load(response)
    except HTTPError as exc:
        exc.close()
        hints = {400: 'Check model support and request configuration.', 401: 'Check the local API key.',
                 403: 'The key lacks access, is restricted, or has been disabled.', 404: 'Choose an available model with --model.',
                 429: 'Quota or rate limit reached. Check your Google project before retrying.'}
        raise GeminiError(f'Gemini HTTP {exc.code}. ' + hints.get(exc.code, 'The provider request failed.')) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise GeminiError('Gemini request failed or returned invalid JSON. Statistical outputs are preserved.') from None
    candidates = raw.get('candidates', []) if isinstance(raw, dict) else []
    if not candidates or candidates[0].get('finishReason') != 'STOP':
        raise GeminiError('Gemini returned a blocked, empty or truncated answer. No explanation was accepted.')
    parts = candidates[0].get('content', {}).get('parts', [])
    text = ''.join(p.get('text', '') for p in parts if not p.get('thought'))
    try:
        answer = json.loads(text)
    except (ValueError, TypeError):
        raise GeminiError('Gemini answer was not valid structured JSON.') from None
    usage = raw.get('usageMetadata', {})
    usage = {k: usage[k] for k in ('promptTokenCount', 'candidatesTokenCount', 'totalTokenCount') if k in usage}
    try:
        validate_narrative(answer, evidence)
    except GeminiError as exc:
        if _repair is not None:
            raise
        answer, info = request_gemini(evidence, api_key, model, opener, _repair=(text, str(exc)))
        info['attempts'] = 2
        info['format_repair_used'] = True
        info['usage'] = {k: usage.get(k, 0) + info['usage'].get(k, 0) for k in set(usage) | set(info['usage'])}
        return answer, info
    return answer, {'provider': 'Gemini', 'requested_model': model, 'returned_model': raw.get('modelVersion'),
                    'attempts': 1, 'format_repair_used': _repair is not None,
                    'usage': usage, 'generated_at': datetime.now(timezone.utc).isoformat()}


def make_request(evidence):
    return {'systemInstruction': {'parts': [{'text': PROMPT_PATH.read_text(encoding='utf-8')}]},
            'contents': [{'role': 'user', 'parts': [{'text': json.dumps(evidence, ensure_ascii=False, allow_nan=False)}]}],
            'generationConfig': {'responseMimeType': 'application/json', 'responseSchema': RESPONSE_SCHEMA,
                                 'maxOutputTokens': 4096}}


def resolve_text(text, evidence):
    return PLACEHOLDER.sub(lambda match: evidence['facts'][match[1]], text)


def render_explanation(answer, evidence):
    text = lambda value: resolve_text(value, evidence)
    lines = ['# Audience explanation', '', f"Account: {evidence['target_account']}",
             f"Data captured: {evidence['as_of']}", f"Sample: {evidence['sample_size']} profiles", '',
             '**Score: ' + evidence['facts']['score'] + ' (' + evidence['facts']['severity'] + ')**', '',
             text(answer['summary']), '', '## What contributed most', '']
    for signal in answer['strongest_signals']:
        key = signal['feature_id']
        lines.append(f"- **{FEATURE_LABELS[key]}:** {text(signal['explanation'])} Observed measure: {evidence['facts'][key]}. Contribution: {evidence['facts'][key + '_contribution']}.")
    lines += ['', '## Hypothesis tests', '', text(answer['hypothesis_tests']), '',
              '| Test | Authoritative result |', '|---|---|']
    for test in evidence['hypothesis_testing']['tests']:
        lines.append(f"| {test['id']} | {test_sentence(test, evidence['hypothesis_testing']['alpha'])} |")
    lines += ['', '## How uncertain is it?', '', 'Score sensitivity range: ' + evidence['facts']['score_interval'] + '.', '', text(answer['uncertainty']), '', '## Limitations', '']
    lines += ['- ' + text(item) for item in answer['limitations']]
    lines += ['', '## Next steps', ''] + ['- ' + text(item) for item in answer['next_steps']]
    lines += ['', '## Interpretation boundaries', '',
              'The score is an uncalibrated heuristic, not a fake-follower probability. These API-order samples are not established as representative. '
              'P-values are conditional on unverified sampling, independence and reference assumptions. '
              'Rejecting a reference does not prove manipulation, and not rejecting it does not prove authenticity. '
              'Bootstrap ranges show sensitivity within this observed sample. Hypothesis tests do not change the score.', '',
              'Numbers and test decisions above are supplied by Python. Gemini generates the surrounding prose, which should still be reviewed.']
    if evidence['dataset_kind'] == 'cumulative_observed_followers':
        lines += ['', 'This is a historical union of observations at different dates, not a current follower census.']
    return '\n'.join(lines) + '\n'


def explain_report(report, out, hypothesis_config=None, model=None, prepare_only=False, api_key=None):
    out = Path(out)
    evidence = prepare_evidence(report, hypothesis_config)
    out.mkdir(parents=True, exist_ok=False)
    write_json(out / 'input.json', evidence)
    write_json(out / 'request.json', make_request(evidence))
    (out / 'prompt.txt').write_text(PROMPT_PATH.read_text(encoding='utf-8'), encoding='utf-8')
    augmented = copy.deepcopy(report)
    augmented['hypothesis_testing'] = evidence['hypothesis_testing']
    write_json(out / 'statistics.json', augmented)
    (out / 'statistical_summary.md').write_text(render_report(augmented), encoding='utf-8')
    status = {'status': 'prepared', 'output_directory': str(out.resolve()), 'network_request_made': False,
              'input_sha256': hashlib.sha256((out / 'input.json').read_bytes()).hexdigest()}
    write_json(out / 'status.json', status)
    if prepare_only:
        return status
    try:
        answer, info = request_gemini(evidence, api_key=api_key, model=model)
        write_json(out / 'explanation.json', {'narrative_template': answer, 'facts': evidence['facts'], 'generation': info})
        (out / 'explanation.md').write_text(render_explanation(answer, evidence), encoding='utf-8')
    except GeminiError as exc:
        status.update(status='failed', error=str(exc), network_request_made=None)
        write_json(out / 'status.json', status)
        raise GeminiError(str(exc) + f' Prepared input and statistics: {out.resolve()}') from None
    status.update(status='generated', network_request_made=True, report=str((out / 'explanation.md').resolve()), **info)
    write_json(out / 'status.json', status)
    return status


def load_saved_report(store, account, view='snapshot'):
    if view not in ('snapshot', 'cumulative'):
        raise ValueError('View must be snapshot or cumulative')
    account = canonical_account(account)
    database = Path(store).expanduser().resolve() / 'history.sqlite3'
    if not database.is_file():
        raise ValueError('No saved history. Collect or import a follower sample first.')
    order = 'collected_at DESC, sequence DESC' if view == 'snapshot' else 'sequence DESC'
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    try:
        row = db.execute(f'SELECT {view}_json FROM runs WHERE account=? ORDER BY {order} LIMIT 1', (account,)).fetchone()
    finally:
        db.close()
    if row is None:
        raise ValueError('No saved sample for that account. Collect or import it first.')
    return json.loads(row[0])['statistics']
