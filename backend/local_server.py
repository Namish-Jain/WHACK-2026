#!/usr/bin/env python3
"""Local companion for the unpacked Chrome extension. Python 3.10+, stdlib only."""
import argparse
import copy
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from analysis.hypotheses import HypothesisConfig
from collect_mastodon import CollectionError, parse_target
from history import HistoryStore, atomic_json, canonical_account
from llm.brief import generate_brief
from llm.settings import GeminiSettings
from llm.env_file import load_env_file
from llm.explain import GeminiError, load_saved_report, prepare_evidence, test_sentence
from public_transport import PublicMastodonAdapter
from run_pipeline import collect_to_store

ROOT = Path(__file__).resolve().parent
PORT = 8765
LABELS = {'low': 'Low bot risk', 'moderate': 'Moderate bot risk',
          'high': 'High bot risk', 'very_high': 'Very high bot risk',
          'insufficient_data': 'Not enough data'}
REASONS = {'benford': 'an unusual pattern in follower-count first digits',
           'ratio': 'accounts following many more people than follow them',
           'activity': 'accounts with very few posts', 'new_account': 'recently created accounts',
           'profile': 'sparsely filled-in profiles'}


def normalize_profile(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 500:
        raise ValueError('Paste a Mastodon profile URL or @username@server handle.')
    value = value.strip()
    # Mastodon also displays remote actors as /@username@remote.instance.
    if value.startswith('https://'):
        parsed = urlsplit(value)
        remote = re.fullmatch(r'/@([A-Za-z0-9_.]+)@([A-Za-z0-9.-]+)/?', parsed.path)
        if remote and not (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.port):
            value = '@' + remote[1] + '@' + remote[2]
    parse_target(value)
    return '@' + canonical_account(value)


def result_for(report, mode, run_id=None):
    evidence = prepare_evidence(report)
    score = report['scoring']['anomaly_score']
    severity = report['scoring']['severity']
    if score is None:
        introduction = 'There is not enough usable follower information to rate this audience.'
        reason = 'Try a profile with more publicly accessible followers; missing or hidden data is not evidence of a fake account.'
    else:
        introduction = f"The sampled audience shows {severity.replace('_', ' ')} anomaly risk ({score:.1f}/100)."
        drivers = [REASONS[k] for k in evidence['top_contributors'][:2]]
        reason = ('The largest score contributions come from ' + ' and '.join(drivers) + '.' if drivers
                  else 'None of the usable indicators contributed anomaly points in this sample.')
    return {'account': report['collection']['target_input'], 'label': LABELS[severity], 'severity': severity,
            'score': score, 'summary': introduction + ' ' + reason, 'introduction': introduction,
            'summary_source': 'Statistical rules', 'summary_notice': None,
            'sample_size': report['sample_size'], 'as_of': report['as_of'], 'mode': mode,
            'run_id': run_id, 'weight_coverage': report['scoring']['configured_weight_coverage'],
            'interval': evidence['facts']['score_interval'],
            'signals': [{'name': k, 'description': REASONS[k], 'measurement': evidence['facts'][k],
                         'points': report['scoring']['components'][k]['contribution_points']}
                        for k in evidence['top_contributors']],
            'tests': [{'name': t['id'].replace('_', ' '), 'result': test_sentence(t, evidence['hypothesis_testing']['alpha'])}
                      for t in evidence['hypothesis_testing']['tests']],
            'limitation': 'Bot risk is a heuristic indicator, not proof of bots. This assesses sampled followers, not whether the profile owner is fake. The score is not a probability; public API samples may be unrepresentative.',
            'hypothesis_note': 'Tests compare specified reference models under assumptions; they do not test whether someone is fake and do not change the score.'}


class AnalysisService:
    def __init__(self, store, hypotheses=None, model=None):
        self.store = Path(store).expanduser().resolve()
        self.hypotheses = hypotheses
        self.model = model
        self.settings = GeminiSettings(self.store / '.detest' / 'gemini.json', model)
        self.jobs = {}
        self.reports = {}
        self.lock = threading.Lock()

    def submit(self, body):
        if not isinstance(body, dict) or set(body) - {'account', 'mode', 'use_gemini'}:
            raise ValueError('Invalid analysis request.')
        account = normalize_profile(body.get('account'))
        mode = body.get('mode', 'fresh')
        use_gemini = body.get('use_gemini', False)
        if mode not in ('fresh', 'saved') or type(use_gemini) is not bool:
            raise ValueError('Invalid collection or explanation option.')
        if use_gemini and not self.settings.read()[0]:
            raise GeminiError('Add your Gemini API key in Connection.')
        with self.lock:
            for job in self.jobs.values():
                if job['state'] == 'running':
                    if (job['account'], job['mode'], job['use_gemini']) == (account, mode, use_gemini):
                        return copy.deepcopy(job)
                    raise RuntimeError('Another profile is being analysed. Wait for it to finish before starting a new one.')
            while len(self.jobs) >= 50:
                oldest = next(iter(self.jobs))
                del self.jobs[oldest]
                self.reports.pop(oldest, None)
            job_id = uuid4().hex
            job = {'id': job_id, 'state': 'running', 'stage': 'Starting', 'step': 0, 'account': account,
                   'mode': mode, 'use_gemini': use_gemini, 'result': None, 'error': None}
            self.jobs[job_id] = job
        threading.Thread(target=self.run, args=(job_id,), daemon=True).start()
        return self.get(job_id)

    def get(self, job_id):
        with self.lock:
            return copy.deepcopy(self.jobs.get(job_id))

    def update(self, job_id, **fields):
        stages = {'Reading profile': 0, 'Reading followers': 0, 'Analysing patterns': 1,
                  'Writing explanation': 2, 'Complete': 3}
        if fields.get('stage') in stages:
            fields['step'] = stages[fields['stage']]
        with self.lock:
            self.jobs[job_id].update(fields)

    def retry(self, job_id):
        if not self.settings.read()[0]:
            raise GeminiError('Add your Gemini API key in Connection.')
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job['state'] != 'explanation_failed' or job_id not in self.reports:
                raise ValueError('This explanation cannot be retried. Analyse the profile again.')
            if any(j['state'] == 'running' for j in self.jobs.values()):
                raise RuntimeError('Wait for the current analysis to finish.')
            report = self.reports[job_id]
            result = copy.deepcopy(job['result'])
            job.update(state='running', stage='Writing explanation', step=2, error=None)
        threading.Thread(target=self.finish, args=(job_id, report, result), daemon=True).start()
        return self.get(job_id)

    def finish(self, job_id, report, result):
        """Retry only generation; collected statistics remain committed and reusable."""
        try:
            job = self.get(job_id)
            result.update(presentation_version=2, gemini_reason=None)
            if job['use_gemini'] and result['score'] is not None:
                self.update(job_id, stage='Writing explanation')
                try:
                    key, model = self.settings.read()
                    if not key:
                        raise GeminiError('Add your Gemini API key in Connection.')
                    out = self.store / 'extension_results' / (job_id + '_gemini_' + uuid4().hex[:8])
                    reason = generate_brief(report, out, model, api_key=key)
                    result.update(summary=result['introduction'] + ' ' + reason,
                                  gemini_reason=reason, summary_source='Gemini + Python rating', summary_notice=None)
                except GeminiError as exc:
                    result.update(summary=None, summary_source=None, summary_notice=str(exc))
                    atomic_json(self.store / 'extension_results' / (job_id + '.json'), result)
                    self.update(job_id, state='explanation_failed', stage='Explanation unavailable',
                                result=result, error=str(exc))
                    return
            result['completed_at'] = datetime.now(timezone.utc).isoformat()
            atomic_json(self.store / 'extension_results' / (job_id + '.json'), result)
            self.update(job_id, state='complete', stage='Complete', result=result, error=None)
        except Exception:
            self.update(job_id, state='failed', stage='Unable to finish',
                        error='Could not save this result. Please try again.')

    def run(self, job_id):
        job = self.get(job_id)
        result = None
        try:
            run_id = None
            if job['mode'] == 'saved':
                self.update(job_id, stage='Reading profile')
                report = load_saved_report(self.store, job['account'])
            else:
                self.update(job_id, stage='Reading followers')
                store = HistoryStore(self.store)
                source = collect_to_store(job['account'], store, 300, adapter_factory=PublicMastodonAdapter)
                self.update(job_id, stage='Analysing patterns')
                imported = store.ingest(source, account=job['account'], replicates=1000, seed=42,
                                        hypothesis_config=self.hypotheses)
                run_id = imported['run_id']
                report = json.loads((store.directory / 'runs' / run_id / 'snapshot' / 'statistics.json').read_text())
            # Old snapshots get the hypothesis layer without rewriting their original history.
            report['hypothesis_testing'] = prepare_evidence(report, self.hypotheses)['hypothesis_testing']
            result = result_for(report, job['mode'], run_id)
            with self.lock:
                self.reports[job_id] = report
            self.finish(job_id, report, result)
        except (ValueError, OSError, sqlite3.Error, CollectionError) as exc:
            self.update(job_id, state='failed', stage='Unable to assess', error=str(exc))
        except Exception:
            # Do not leak a traceback or credentials over HTTP; preserve a usable UI error.
            self.update(job_id, state='failed', stage='Unable to assess',
                        error='The analysis could not finish. Check your saved collection files and try again.')


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service, token, port=PORT):
        self.service = service
        self.token = token
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # No request bodies, tokens, keys, or profile URLs in access logs.

    def origin_ok(self):
        origin = self.headers.get('Origin')
        return origin is None or re.fullmatch(r'chrome-extension://[a-p]{32}', origin) is not None

    def host_ok(self):
        return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

    def send_json(self, code, value):
        data = json.dumps(value, allow_nan=False).encode()
        self.send_response(code)
        if self.origin_ok() and self.headers.get('Origin'):
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
            self.send_header('Vary', 'Origin')
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self, auth=True):
        if not self.host_ok() or not self.origin_ok():
            self.send_json(403, {'error': 'Only the local Chrome extension may access this service.'})
            return False
        if auth and not hmac.compare_digest(self.headers.get('X-Audience-Token', '').encode(), self.server.token.encode()):
            self.send_json(401, {'error': 'Update your connection code in Connection.'})
            return False
        return True

    def do_OPTIONS(self):
        if not self.allowed(auth=False):
            return
        self.send_response(204)
        if self.headers.get('Origin'):
            self.send_header('Access-Control-Allow-Origin', self.headers['Origin'])
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Audience-Token')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()

    def do_GET(self):
        if not self.allowed():
            return
        if self.path == '/health':
            try:
                self.send_json(200, {'service': 'detest', 'api_version': 2, **self.server.service.settings.public()})
            except GeminiError as exc:
                self.send_json(200, {'service': 'detest', 'api_version': 2, 'gemini_available': False,
                                     'settings_error': str(exc)})
        elif re.fullmatch(r'/jobs/[a-f0-9]{32}', self.path):
            job = self.server.service.get(self.path.rsplit('/', 1)[1])
            self.send_json(200 if job else 404, job or {'error': 'This lookup is no longer in memory. Run it again or use a saved sample.'})
        else:
            self.send_json(404, {'error': 'Unknown endpoint.'})

    def do_POST(self):
        if not self.allowed():
            return
        retry = re.fullmatch(r'/jobs/([a-f0-9]{32})/retry', self.path)
        if self.path not in ('/jobs', '/settings') and not retry:
            self.send_json(404, {'error': 'Unknown endpoint.'})
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise ValueError('Send a small JSON request.')
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(size))
            if self.path == '/settings':
                if not isinstance(body, dict) or set(body) != {'api_key', 'model'}:
                    raise ValueError('Invalid settings request.')
                self.send_json(200, self.server.service.settings.save(body['api_key'], body['model']))
            elif retry:
                self.send_json(202, self.server.service.retry(retry[1]))
            else:
                self.send_json(202, self.server.service.submit(body))
        except (ValueError, CollectionError, TimeoutError) as exc:
            self.send_json(400, {'error': str(exc)})
        except RuntimeError as exc:
            self.send_json(409, {'error': str(exc)})
        except GeminiError as exc:
            self.send_json(428, {'error': str(exc)})
        except OSError:
            self.send_json(500, {'error': 'Could not save settings. Check access to your history folder.'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, default=ROOT / 'history')
    parser.add_argument('--hypotheses', type=Path)
    parser.add_argument('--model')
    args = parser.parse_args()
    try:
        load_env_file(ROOT.parent / '.env')
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    hypotheses = HypothesisConfig(**json.loads(args.hypotheses.read_text())) if args.hypotheses else None
    token = secrets.token_urlsafe(32)
    try:
        server = LocalServer(AnalysisService(args.store, hypotheses, args.model), token)
    except OSError:
        parser.exit(1, 'Port 8765 is in use. Stop the previous local server before starting this one.\n')
    print('\nDeTect is running at http://127.0.0.1:8765', flush=True)
    print('Connection code (paste into the extension):\n' + token, flush=True)
    print('Add or update your Gemini key in DeTect Connection.', flush=True)
    print('History: ' + str(args.store.resolve()) + '\nKeep this terminal open. Press Ctrl+C to stop.\n', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
