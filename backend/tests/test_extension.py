import copy
import http.client
import io
import json
import os
import socket
import ssl
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, Request, build_opener
from urllib.response import addinfourl

from collect_mastodon import CollectionError
from history import HistoryStore
from llm.brief import generate_brief, validate_reason
from llm.explain import GeminiError, load_saved_report
from local_server import AnalysisService, LocalServer, normalize_profile, result_for
from public_transport import PublicAPIRedirect, PublicHTTPSConnection, PublicMastodonAdapter
from run_pipeline import collect_to_store

ROOT = Path(__file__).resolve().parents[1]


def example():
    return json.loads((ROOT / 'tests/fixtures/statistics.json').read_text())


class ExtensionTests(unittest.TestCase):
    def test_dns_failure_explains_cached_profiles_without_opening_socket(self):
        for code in (socket.EAI_NONAME, socket.EAI_AGAIN):
            with patch('public_transport.socket.getaddrinfo', side_effect=socket.gaierror(code, 'test DNS failure')), patch('public_transport.socket.socket') as constructor:
                with self.assertRaisesRegex(CollectionError, r'Cannot resolve the profile server mastodon.host.*cached federated copy'):
                    PublicHTTPSConnection('mastodon.host').connect()
                constructor.assert_not_called()

    def test_remote_profile_uses_home_server_not_viewing_server(self):
        self.assertEqual(normalize_profile('https://mastodon.social/@Ace@mastodon.host'), '@ace@mastodon.host')

    def test_safe_lookup_redirect_changes_api_host_and_strips_credentials(self):
        adapter = PublicMastodonAdapter('https://old.example.org')
        handler = PublicAPIRedirect(adapter)
        req = Request('https://old.example.org/api/v1/accounts/lookup?acct=Alice',
                      headers={'Authorization': 'secret', 'Cookie': 'private'})
        redirected = handler.redirect_request(req, None, 301, '', {}, 'https://new.example.org/api/v1/accounts/lookup?acct=Alice')
        self.assertEqual(adapter.instance, 'https://new.example.org')
        self.assertFalse(redirected.has_header('Authorization'))
        self.assertFalse(redirected.has_header('Cookie'))
        self.assertEqual(adapter.requests[0]['status'], 301)

    def test_sportsbots_rewrite_and_query_changes_explain_unsupported_api(self):
        adapter = PublicMastodonAdapter('https://sportsbots.xyz')
        handler = PublicAPIRedirect(adapter)
        req = Request('https://sportsbots.xyz/api/v1/accounts/lookup?acct=PitcherList')
        for url in ['https://www.sportsbots.xyz/api/v1/accounts/lookup_acct_PitcherList',
                    'https://www.sportsbots.xyz/api/v1/accounts/lookup',
                    'https://www.sportsbots.xyz/api/v1/accounts/lookup?acct=SomeoneElse',
                    'https://www.sportsbots.xyz/auth/sign_in']:
            with self.assertRaisesRegex(CollectionError, 'does not provide the public Mastodon API'):
                handler.redirect_request(req, None, 301, '', {}, url)
        self.assertEqual(adapter.instance, 'https://sportsbots.xyz')

    def test_redirect_rejects_downgrade_credentials_and_mid_collection_host_move(self):
        adapter = PublicMastodonAdapter('https://example.org')
        handler = PublicAPIRedirect(adapter)
        req = Request('https://example.org/api/v1/accounts/lookup?acct=Alice')
        for url in ['http://example.org/api/v1/accounts/lookup?acct=Alice',
                    'https://user:password@example.org/api/v1/accounts/lookup?acct=Alice',
                    'https://example.org:8000/api/v1/accounts/lookup?acct=Alice',
                    'file:///tmp/anything']:
            with self.assertRaises(CollectionError):
                handler.redirect_request(req, None, 301, '', {}, url)
        with self.assertRaisesRegex(CollectionError, 'mid-collection'):
            handler.redirect_request(Request('https://example.org/api/v1/accounts/1/followers?limit=80'), None, 301, '', {},
                                     'https://other.example.org/api/v1/accounts/1/followers?limit=80')

    def test_redirect_loop_is_bounded(self):
        class Loop(HTTPSHandler):
            def https_open(self, req):
                host = 'https://a.example.org' if 'b.example.org' in req.full_url else 'https://b.example.org'
                response = addinfourl(io.BytesIO(b''), {'Location': host + '/api/v1/accounts/lookup?acct=Alice'}, req.full_url, 301)
                response.msg = 'Moved'
                return response
        adapter = PublicMastodonAdapter('https://a.example.org')
        opener = build_opener(PublicAPIRedirect(adapter), Loop())
        with self.assertRaises(HTTPError) as error:
            opener.open('https://a.example.org/api/v1/accounts/lookup?acct=Alice')
        error.exception.close()
        self.assertLessEqual(len(adapter.requests), 5)

    def test_redirected_collection_uses_resolved_source_instance(self):
        class AliasAdapter:
            def __init__(self, instance, token):
                self.instance = 'https://actual.example.org'
                self.requests = []
            def collect(self, username, limit):
                return {'id': 'target', 'followers_count': 1}, [{'id': 'one', 'acct': 'reader', 'followers_count': 5}], 'limit_reached', None
        with tempfile.TemporaryDirectory() as tmp:
            store = HistoryStore(Path(tmp) / 'history')
            source = collect_to_store('@alice@alias.example.org', store, 1, adapter_factory=AliasAdapter)
            rows = json.loads((source / 'followers_normalized.json').read_text())
            metadata = json.loads((source / 'collection_metadata.json').read_text())
            self.assertEqual(rows[0]['source_instance'], 'https://actual.example.org')
            self.assertEqual(rows[0]['username'], 'reader@actual.example.org')
            self.assertEqual(metadata['target_input'], '@alice@alias.example.org')

    def test_profile_formats(self):
        for value in ['@alice@example.org', 'alice@example.org', 'https://example.org/@alice',
                      'https://example.org/users/alice', 'https://mastodon.social/@alice@example.org']:
            self.assertEqual(normalize_profile(value), '@alice@example.org')
        for value in [None, {}, 'http://example.org/@alice', 'https://example.org/@alice?q=x',
                      'https://secret@example.org/@alice', 'https://example.org:8000/@alice',
                      'https://example.org/@alice/123', 'https://example.org/', '']:
            with self.assertRaises((ValueError, CollectionError)):
                normalize_profile(value)

    def test_ratings_and_unavailable_never_binary_fake(self):
        report = example()
        for severity, score in [('low', 0), ('moderate', 50), ('high', 75), ('very_high', 90), ('insufficient_data', None)]:
            report['scoring'].update(severity=severity, anomaly_score=score)
            result = result_for(report, 'saved')
            self.assertEqual(result['score'], score)
            self.assertNotIn('fake', result['label'])
            self.assertEqual(len(result['tests']), 5)
            self.assertIn('not whether the profile owner is fake', result['limitation'])
            self.assertLess(len(result['summary'].split()), 85)

    def test_compact_stats_expose_existing_analysis_values(self):
        stats = result_for(example(), 'saved')['stats_analysis']
        self.assertAlmostEqual(stats['goodness_of_fit'], 14.029288758264371)
        self.assertAlmostEqual(stats['adjusted_p_value'], 0.08100520262698216)
        self.assertAlmostEqual(stats['following_imbalance'], .49)
        self.assertAlmostEqual(stats['low_activity'], 23 / 30)
        self.assertFalse(stats['p_value_at_floor'])

    def test_compact_stats_keep_withheld_and_missing_values_unavailable(self):
        report = example()
        report['benford']['n_valid'] = 0
        report['features']['low_activity_rate'].update(value=None, n_valid=0, n_flagged=0)
        stats = result_for(report, 'saved')['stats_analysis']
        self.assertIsNone(stats['goodness_of_fit'])
        self.assertIsNone(stats['adjusted_p_value'])
        self.assertIsNone(stats['low_activity'])

    def test_gemini_brief_validation(self):
        good = 'The sample contains many quiet accounts and accounts that follow far more people than follow them.'
        self.assertEqual(validate_reason({'reason': good}), good)
        for bad in ['This account is fake.', 'There are 30 quiet followers.',
                    'These patterns prove manipulation in this audience.',
                    'The sample has quiet accounts. They may just be readers.', '<script>alert(1)</script>',
                    'The audience appears statistically significant in this sample.', 'Too short.']:
            with self.assertRaises(GeminiError):
                validate_reason({'reason': bad})

    def test_gemini_request_contains_tests_but_no_key_in_files(self):
        response = Mock()
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: None
        response.read.return_value = json.dumps({'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': json.dumps({'reason': 'The sample contains many quiet accounts and accounts with sparse profiles.'})}]}}]}).encode()
        opener = Mock()
        opener.open.return_value = response
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'GEMINI_API_KEY': 'test-only-key'}):
            out = Path(tmp) / 'brief'
            reason = generate_brief(example(), out, opener=opener)
            self.assertIn('quiet', reason)
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 120)
            body = json.loads(opener.open.call_args.args[0].data)
            self.assertIn('hypothesis_testing', body['contents'][0]['parts'][0]['text'])
            self.assertEqual(body['generationConfig']['maxOutputTokens'], 4096)
            self.assertFalse(any('test-only-key' in p.read_text() for p in out.iterdir()))

    def test_brief_transport_errors_are_distinct_and_never_expose_secrets(self):
        errors = [(TimeoutError('secret-test-key'), 'too long'),
                  (URLError(TimeoutError('secret-test-key')), 'too long'),
                  (URLError(ssl.SSLCertVerificationError('secret-test-key')), 'TLS certificate'),
                  (URLError(socket.gaierror('secret-test-key')), 'DNS'),
                  (URLError('secret-test-key'), 'Could not connect'),
                  (ConnectionResetError('secret-test-key'), 'interrupted')]
        for failure, expected in errors:
            with self.subTest(kind=type(failure).__name__), tempfile.TemporaryDirectory() as tmp:
                opener = Mock(); opener.open.side_effect = failure
                with self.assertRaisesRegex(GeminiError, expected) as error:
                    generate_brief(example(), Path(tmp) / 'brief', opener=opener, api_key='secret-test-key')
                self.assertNotIn('secret-test-key', str(error.exception))

    def test_brief_malformed_response_and_model_specific_thinking(self):
        for model in ('gemini-3.8-flash', 'gemini-other'):
            with tempfile.TemporaryDirectory() as tmp:
                opener = Mock(); opener.open.return_value = io.BytesIO(b'not JSON')
                with self.assertRaisesRegex(GeminiError, 'unreadable response'):
                    generate_brief(example(), Path(tmp) / 'brief', model=model, opener=opener, api_key='test-key')
                config = json.loads(opener.open.call_args.args[0].data)['generationConfig']
                if model == 'gemini-3.8-flash':
                    self.assertEqual(config['thinkingConfig'], {'thinkingLevel': 'low'})
                else:
                    self.assertNotIn('thinkingConfig', config)

    def test_public_transport_blocks_private_mixed_dns_and_does_not_forward_token(self):
        for addresses in [['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'], ['8.8.8.8', '127.0.0.1']]:
            records = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443)) for ip in addresses]
            with patch('public_transport.socket.getaddrinfo', return_value=records), patch('public_transport.socket.socket') as constructor:
                with self.assertRaises(CollectionError):
                    PublicHTTPSConnection('example.org').connect()
                constructor.assert_not_called()
        self.assertIsNone(PublicMastodonAdapter('https://example.org', 'do-not-forward').token)

    def test_public_transport_uses_validated_ip_not_second_dns_lookup(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('8.8.8.8', 443))]
        context, sock = Mock(), Mock()
        with patch('public_transport.socket.getaddrinfo', return_value=records) as dns, patch('public_transport.socket.socket', return_value=sock):
            conn = PublicHTTPSConnection('example.org', context=context)
            conn.connect()
            dns.assert_called_once()
            sock.connect.assert_called_once_with(('8.8.8.8', 443))
            context.wrap_socket.assert_called_once_with(sock, server_hostname='example.org')

    def test_missing_key_stops_before_collecting_or_showing_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AnalysisService(tmp)
            with patch.dict(os.environ, {'GEMINI_API_KEY': '', 'GOOGLE_API_KEY': ''}), patch('local_server.collect_to_store') as collect:
                with self.assertRaisesRegex(GeminiError, 'Connection'):
                    service.submit({'account': '@bagder@mastodon.social', 'mode': 'saved', 'use_gemini': True})
            collect.assert_not_called()
            self.assertFalse((Path(tmp) / 'history.sqlite3').exists())
            self.assertFalse(service.jobs)

    def test_provider_failure_keeps_completed_statistics(self):
        with tempfile.TemporaryDirectory() as tmp, patch('local_server.load_saved_report', return_value=example()), patch.dict(os.environ, {'GEMINI_API_KEY': 'test-only-key'}), patch('local_server.generate_brief', side_effect=GeminiError('Provider unavailable')):
            service = AnalysisService(tmp)
            job = self.wait_job(service, service.submit({'account': '@bagder@mastodon.social', 'mode': 'saved', 'use_gemini': True})['id'])
            self.assertEqual(job['state'], 'explanation_failed')
            self.assertEqual(job['result']['summary_notice'], 'Provider unavailable')
            self.assertIsNone(job['result']['summary'])
            self.assertIsNone(job['result']['summary_source'])
            self.assertEqual(job['result']['sample_size'], 300)

    def test_progress_step_tracks_backend_stage_and_retains_failed_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = AnalysisService(tmp)
            service.jobs['test'] = {'id': 'test', 'state': 'running', 'step': 0}
            for stage, expected in [('Reading followers', 0), ('Analysing patterns', 1),
                                    ('Writing explanation', 2)]:
                service.update('test', stage=stage)
                self.assertEqual(service.get('test')['step'], expected)
            service.update('test', state='explanation_failed', stage='Explanation unavailable')
            self.assertEqual(service.get('test')['step'], 2)
            service.update('test', state='complete', stage='Complete')
            self.assertEqual(service.get('test')['step'], 3)

    def test_explanation_retry_uses_saved_report_without_recollecting(self):
        with tempfile.TemporaryDirectory() as tmp, patch('local_server.load_saved_report', return_value=example()) as load, patch.dict(os.environ, {'GEMINI_API_KEY': 'test-only-key'}), patch('local_server.collect_to_store') as collect:
            service = AnalysisService(tmp)
            with patch('local_server.generate_brief', side_effect=GeminiError('Provider unavailable')):
                job = self.wait_job(service, service.submit({'account': '@bagder@mastodon.social', 'mode': 'saved', 'use_gemini': True})['id'])
            reason = 'Quiet accounts and sparse profiles contributed to this assessment.'
            with patch('local_server.generate_brief', return_value=reason) as generate:
                service.retry(job['id'])
                retried = self.wait_job(service, job['id'])
            self.assertEqual(retried['state'], 'complete')
            self.assertEqual(retried['result']['gemini_reason'], reason)
            self.assertEqual(retried['result']['presentation_version'], 2)
            self.assertEqual(generate.call_args.kwargs['api_key'], 'test-only-key')
            load.assert_called_once()
            collect.assert_not_called()

    def test_hidden_followers_fail_without_rating(self):
        with tempfile.TemporaryDirectory() as tmp, patch('local_server.collect_to_store', side_effect=ValueError('Hidden follower network')):
            service = AnalysisService(tmp)
            job = self.wait_job(service, service.submit({'account': '@alice@example.org'})['id'])
            self.assertEqual(job['state'], 'failed')
            self.assertIsNone(job['result'])

    def test_fresh_collection_merges_history_and_displays_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def source(name, start, end, date):
                directory = root / name
                directory.mkdir()
                rows = [{'id': str(i), 'source_instance': 'https://example.org', 'username': f'u{i}@example.org',
                         'followers_count': i + 1, 'following_count': 300, 'posts_count': 0,
                         'created_at': '2026-01-01T00:00:00Z', 'has_profile_picture': True, 'bio_length': 5} for i in range(start, end)]
                (directory / 'followers_normalized.json').write_text(json.dumps(rows))
                (directory / 'collection_metadata.json').write_text(json.dumps({'target_input': '@alice@example.org', 'collected_at': date,
                    'instance': 'https://example.org', 'sample_size': len(rows), 'reported_followers_count': 100, 'stop_reason': 'limit_reached'}))
                return directory
            store = HistoryStore(root / 'history')
            store.ingest(source('first', 0, 30, '2026-11-01T00:00:00Z'), replicates=0)
            later = source('second', 10, 40, '2026-11-02T00:00:00Z')
            service = AnalysisService(store.directory)
            with patch('local_server.collect_to_store', return_value=later):
                job = self.wait_job(service, service.submit({'account': '@alice@example.org'})['id'])
            self.assertEqual(job['state'], 'complete', job['error'])
            self.assertEqual(job['result']['sample_size'], 30)
            self.assertEqual(load_saved_report(store.directory, '@alice@example.org', 'cumulative')['sample_size'], 40)
            self.assertEqual(load_saved_report(store.directory, '@alice@example.org')['sample_size'], 30)

    def test_running_job_survives_popup_and_duplicate_submission(self):
        entered, release = threading.Event(), threading.Event()
        def wait_report(*args):
            entered.set()
            release.wait(3)
            return example()
        with tempfile.TemporaryDirectory() as tmp, patch('local_server.load_saved_report', side_effect=wait_report):
            service = AnalysisService(tmp)
            body = {'account': '@bagder@mastodon.social', 'mode': 'saved'}
            first = service.submit(body)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(service.submit(body)['id'], first['id'])
                with self.assertRaises(RuntimeError):
                    service.submit({'account': '@alice@example.org'})
            finally:
                release.set()
                self.wait_job(service, first['id'])

    @staticmethod
    def wait_job(service, job_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = service.get(job_id)
            if job['state'] != 'running':
                return job
            time.sleep(.02)
        raise AssertionError('Job did not finish')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = LocalServer(AnalysisService(self.temp.name), 'local-test-token', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method='GET', path='/health', headers=None, body=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        status = response.status
        conn.close()
        return status, json.loads(data) if data else None

    def test_auth_origin_host_and_preflight(self):
        auth = {'X-Audience-Token': 'local-test-token', 'Origin': 'chrome-extension://' + 'a' * 32}
        self.assertEqual(self.request()[0], 401)
        self.assertEqual(self.request(headers=auth)[0], 200)
        self.assertEqual(self.request(headers=dict(auth, Origin='https://example.org'))[0], 403)
        self.assertEqual(self.request(headers=dict(auth, Host='evil.example'))[0], 403)
        self.assertEqual(self.request('OPTIONS', headers={'Origin': auth['Origin']})[0], 204)
        self.assertEqual(self.request('OPTIONS', headers={'Origin': 'null'})[0], 403)

    def test_bad_request_and_unknown_job(self):
        headers = {'X-Audience-Token': 'local-test-token', 'Content-Type': 'application/json'}
        self.assertEqual(self.request('POST', '/jobs', headers, '{')[0], 400)
        self.assertEqual(self.request('POST', '/jobs', headers, json.dumps({'account': 'not a profile'}))[0], 400)
        self.assertEqual(self.request(path='/jobs/' + 'a' * 32, headers=headers)[0], 404)

    def test_http_job_roundtrip(self):
        headers = {'X-Audience-Token': 'local-test-token', 'Content-Type': 'application/json',
                   'Origin': 'chrome-extension://' + 'a' * 32}
        with patch('local_server.load_saved_report', return_value=example()):
            status, job = self.request('POST', '/jobs', headers, json.dumps({'account': '@bagder@mastodon.social', 'mode': 'saved'}))
            self.assertEqual(status, 202)
            ExtensionTests.wait_job(self.server.service, job['id'])
        status, completed = self.request(path='/jobs/' + job['id'], headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(completed['state'], 'complete')
        self.assertEqual(completed['result']['sample_size'], 300)
        self.assertEqual(completed['result']['label'], 'Moderate bot risk')


if __name__ == '__main__':
    unittest.main()
