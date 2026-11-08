import copy
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from analysis.benford import analyze_benford
from analysis.hypotheses import HypothesisConfig, binomial_upper_tail, chi_square_8_sf, holm_adjust, run_hypothesis_tests
from llm.explain import (GeminiError, prepare_evidence, validate_narrative, render_explanation,
                         request_gemini, explain_report, load_saved_report, make_request)
from history import HistoryStore
from run_pipeline import main

ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / 'tests/fixtures/statistics.json').read_text())


def valid_answer(evidence):
    return {'summary': 'The sample score is {{score}}, in the {{severity}} category. It describes unusual patterns, not proof of fake followers.',
            'strongest_signals': [{'feature_id': k, 'explanation': '{{' + k + '}} describes this observed sample.'} for k in evidence['top_contributors']],
            'hypothesis_tests': '{{test_benford_goodness_of_fit}} Other rates lack a prespecified baseline. The sampling assumptions are unverified.',
            'uncertainty': 'The range is {{score_interval}}. It applies only within this sample.',
            'limitations': ['Quiet readers can be legitimate.', 'The sample is not established as representative.'],
            'next_steps': ['Obtain an independently justified reference.']}


class HypothesisTests(unittest.TestCase):
    def test_binomial_golden(self):
        self.assertAlmostEqual(binomial_upper_tail(3, 15, .1), .18406106910639114, places=13)
        for n in (1, 4, 10, 30):
            for k in range(n + 1):
                expected = sum(math.comb(n, i) for i in range(k, n + 1)) / 2 ** n
                self.assertAlmostEqual(binomial_upper_tail(k, n, .5), expected, places=12)
        self.assertEqual(binomial_upper_tail(0, 300, .2), 1)
        self.assertGreater(binomial_upper_tail(300, 300, .001), 0)

    def test_chi_square_golden(self):
        self.assertEqual(chi_square_8_sf(0), 1)
        self.assertAlmostEqual(chi_square_8_sf(15.50731305586545), .05, places=12)
        self.assertGreater(chi_square_8_sf(2000), 0)

    def test_holm(self):
        result = holm_adjust([.01, .04, .03])
        for actual, expected in zip(result, [.03, .06, .06]):
            self.assertAlmostEqual(actual, expected)
        self.assertAlmostEqual(holm_adjust([.01], 5)[0], .05)
        self.assertEqual(holm_adjust([]), [])

    def test_real_sample_default_and_baselines(self):
        s = example()
        default = run_hypothesis_tests(s['benford'], s['features'])
        self.assertEqual(default['multiple_testing']['computed_tests'], 1)
        self.assertTrue(all(t['status'] == 'not_tested' for t in default['tests'][1:]))
        ref = HypothesisConfig(reference_rates={'low_activity_rate': {'rate': .1, 'source': 'Hypothetical unit-test assumption, not real population data'}})
        tested = run_hypothesis_tests(s['benford'], s['features'], ref)
        self.assertEqual(tested['multiple_testing']['planned_family_size'], 2)
        low = next(t for t in tested['tests'] if t['id'] == 'low_activity_rate')
        self.assertTrue(low['reject_reference_at_alpha'])
        self.assertAlmostEqual(low['effect_size_percentage_points'], 66.66666666666667)

    def test_gates_and_empty(self):
        f = example()['features']
        for counts, reason in [([], 'Fewer'), ([1, 1000] * 20, 'below five'), ([1, 9] * 100, 'two orders')]:
            r = run_hypothesis_tests(analyze_benford(counts), f)['tests'][0]
            self.assertEqual(r['status'], 'not_tested')
            self.assertIn(reason, r['reason'])

    def test_invalid_config(self):
        for value in [0, 1, True, float('nan')]:
            with self.assertRaises(ValueError): HypothesisConfig(alpha=value)
        for refs in [{'made_up': {}}, {'low_activity_rate': {'rate': .1, 'source': ''}},
                     {'low_activity_rate': {'rate': 0, 'source': 'test'}}]:
            with self.assertRaises(ValueError): HypothesisConfig(reference_rates=refs)


class ExplanationTests(unittest.TestCase):
    def test_evidence_privacy_and_fact_binding(self):
        report = example()
        report['collection']['private_note'] = 'DO_NOT_FORWARD'
        report['profile_features'] = [{'username': 'DO_NOT_FORWARD'}]
        report['input_quality']['source_path'] = 'DO_NOT_FORWARD'
        evidence = prepare_evidence(report)
        self.assertNotIn('DO_NOT_FORWARD', json.dumps(evidence))
        self.assertEqual(evidence['facts']['score'], '57.26 / 100')
        answer = valid_answer(evidence)
        validate_narrative(answer, evidence)
        rendered = render_explanation(answer, evidence)
        self.assertIn('57.26 / 100', rendered)
        self.assertNotIn('{{', rendered)
        self.assertIn('not a fake-follower probability', rendered)
        self.assertEqual(example()['scoring'], report['scoring'])

    def test_reject_invented_numbers_and_facts(self):
        evidence = prepare_evidence(example())
        for text in ['The score is 99.', 'It has {{invented_fact}}.', '<script>unsafe</script>']:
            answer = valid_answer(evidence)
            answer['limitations'][0] = text
            with self.assertRaises(GeminiError): validate_narrative(answer, evidence)
        answer = valid_answer(evidence)
        answer['strongest_signals'][0]['feature_id'] = 'new_account'
        with self.assertRaises(GeminiError): validate_narrative(answer, evidence)

    def test_missing_key_and_invalid_model(self):
        evidence = prepare_evidence(example())
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(GeminiError): request_gemini(evidence)
        with self.assertRaises(GeminiError): request_gemini(evidence, api_key='test', model='../bad')

    def test_request_transport_and_response_validation(self):
        evidence = prepare_evidence(example())
        answer = valid_answer(evidence)
        raw = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps(answer)}]}}],
               'usageMetadata': {'totalTokenCount': 100}}
        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return io.BytesIO(json.dumps(raw).encode())
        opener = Opener()
        parsed, info = request_gemini(evidence, api_key='TEST_SECRET', opener=opener)
        self.assertEqual(parsed, answer)
        self.assertNotIn('TEST_SECRET', opener.request.full_url)
        self.assertNotIn('TEST_SECRET', opener.request.data.decode())
        self.assertEqual(opener.request.get_header('X-goog-api-key'), 'TEST_SECRET')
        raw['candidates'][0]['finishReason'] = 'MAX_TOKENS'
        with self.assertRaises(GeminiError): request_gemini(evidence, api_key='test', opener=opener)

    def test_format_repair_is_bounded_and_records_usage(self):
        evidence = prepare_evidence(example())
        invalid = valid_answer(evidence); invalid['summary'] += ' Invented 999.'
        valid = valid_answer(evidence)
        class Opener:
            calls = 0
            def open(self, request, timeout):
                self.calls += 1
                result = invalid if self.calls == 1 else valid
                return io.BytesIO(json.dumps({'candidates':[{'finishReason':'STOP','content':{'parts':[{'text':json.dumps(result)}]}}], 'usageMetadata':{'totalTokenCount':10}}).encode())
        opener = Opener()
        _, info = request_gemini(evidence, api_key='test', opener=opener)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(info['usage']['totalTokenCount'], 20)
        self.assertTrue(info['format_repair_used'])

    def test_provider_error_does_not_echo_secret(self):
        evidence = prepare_evidence(example())
        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 403, 'TEST_SECRET', {}, io.BytesIO(b'TEST_SECRET'))
        with self.assertRaises(GeminiError) as result:
            request_gemini(evidence, api_key='TEST_SECRET', opener=Opener())
        self.assertNotIn('TEST_SECRET', str(result.exception))

    def test_prepare_only_and_failure_preserve_statistics(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'prepared'
            with patch('llm.explain.request_gemini') as client:
                result = explain_report(example(), out, prepare_only=True)
                client.assert_not_called()
            self.assertEqual(result['status'], 'prepared')
            self.assertTrue((out / 'statistics.json').is_file())
            with self.assertRaises(FileExistsError): explain_report(example(), out, prepare_only=True)
            failed = Path(root) / 'failed'
            with patch('llm.explain.request_gemini', side_effect=GeminiError('Test failure')):
                with self.assertRaises(GeminiError): explain_report(example(), failed)
            self.assertEqual(json.loads((failed / 'status.json').read_text())['status'], 'failed')
            self.assertFalse((failed / 'explanation.md').exists())
            self.assertTrue((failed / 'statistical_summary.md').exists())

    def test_success_exports_verified_values(self):
        report = example(); evidence = prepare_evidence(report)
        with tempfile.TemporaryDirectory() as root, patch('llm.explain.request_gemini', return_value=(valid_answer(evidence), {'provider':'Gemini'})):
            result = explain_report(report, Path(root) / 'out')
            self.assertEqual(result['status'], 'generated')
            self.assertIn('57.26', Path(result['report']).read_text())

    def test_collect_explanation_failure_keeps_history(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source = root / 'sample.json'
            source.write_text(json.dumps([{'username':'follower@example.org','followers_count':1,'posts_count':0}]))
            store = root / 'history'
            with patch('run_pipeline.explain_report', side_effect=GeminiError('simulated provider outage')), patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                code = main(['import', str(source), '--account', '@target@example.org', '--collected-at', '2026-01-01T00:00:00Z', '--store', str(store), '--bootstrap', '0', '--explain'])
            self.assertEqual(code, 3)
            self.assertEqual(load_saved_report(store, '@target@example.org')['sample_size'], 1)

    def test_authoritative_values_render_when_prose_omits_placeholders(self):
        evidence = prepare_evidence(example())
        answer = valid_answer(evidence)
        answer['summary'] = 'The sample shows some unusual patterns that deserve a closer look.'
        answer['hypothesis_tests'] = 'The comparison is exploratory and depends on unverified assumptions.'
        answer['uncertainty'] = 'Changing the sampled profiles can move the score.'
        validate_narrative(answer, evidence)
        rendered = render_explanation(answer, evidence)
        self.assertIn(evidence['facts']['score'], rendered)
        self.assertIn(evidence['facts']['score_interval'], rendered)
        self.assertIn('adjusted p =', rendered)

    def test_insufficient_score_stays_unavailable(self):
        from analysis.pipeline import analyze
        from analysis.config import Config
        from datetime import datetime, timezone
        report, _, _ = analyze([], datetime(2026, 1, 1, tzinfo=timezone.utc), Config(), bootstrap_replicates=0)
        evidence = prepare_evidence(report)
        self.assertEqual(evidence['facts']['score'], 'unavailable')
        self.assertEqual(evidence['top_contributors'], [])
        validate_narrative(valid_answer(evidence), evidence)

    def test_saved_history_read_only_and_cli(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); store = HistoryStore(root / 'history')
            rows = [{'id': str(i), 'source_instance':'https://example.org', 'username':f'user{i}',
                     'followers_count': 10 ** (i % 4), 'following_count':100,'posts_count':20} for i in range(120)]
            source = root / 'sample.json'; source.write_text(json.dumps(rows))
            result = store.ingest(source, account='@target@example.org', collected_at='2026-01-01T00:00:00Z', replicates=0)
            original = store.db_path.read_bytes()
            report = load_saved_report(store.directory, '@target@example.org')
            self.assertEqual(report['sample_size'], 120)
            self.assertEqual(store.db_path.read_bytes(), original)
            with patch('sys.stdout', new_callable=io.StringIO):
                code = main(['explain', '@target@example.org', '--store', str(store.directory), '--out', str(root/'explanation'), '--prepare-only'])
            self.assertEqual(code, 0)
            self.assertEqual(store.db_path.read_bytes(), original)
            same = store.ingest(source, account='@target@example.org', collected_at='2026-01-01T00:00:00Z', replicates=0)
            self.assertEqual(same['run_id'], result['run_id'])
            changed = store.ingest(source, account='@target@example.org', collected_at='2026-01-01T00:00:00Z', replicates=0, hypothesis_config=HypothesisConfig(alpha=.01))
            self.assertNotEqual(changed['run_id'], result['run_id'])
