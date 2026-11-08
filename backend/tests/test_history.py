import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from analysis.config import Config
from history import HistoryStore
from run_pipeline import main
from collect_mastodon import CollectionError, MastodonAdapter, parse_target


def row(identifier, posts=0):
    return {'id': identifier, 'source_instance': 'https://example.org', 'username': identifier + '@example.org',
            'followers_count': 10, 'following_count': 300, 'posts_count': posts,
            'created_at': '2026-01-01T00:00:00Z', 'is_bot': False, 'has_profile_picture': True, 'bio_length': 15}


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = HistoryStore(self.root / 'store')

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name, rows, target='@alice@example.org', date='2026-11-01T00:00:00Z', **extra):
        folder = self.root / name
        folder.mkdir()
        (folder/'followers_normalized.json').write_text(json.dumps(rows))
        metadata = {'target_input': target, 'collected_at': date, 'instance': 'https://example.org',
                    'sample_size': len(rows), 'reported_followers_count': 100, 'stop_reason': 'limit_reached', **extra}
        (folder/'collection_metadata.json').write_text(json.dumps(metadata))
        return folder

    def ingest(self, source, **kwargs):
        return self.store.ingest(source, config=Config(minimum_feature_sample=1), replicates=0, **kwargs)

    def read(self, run, kind='cumulative', file='statistics.json'):
        return json.loads((Path(run['run_directory'])/kind/file).read_text())

    def test_merge_newest_record_wins_without_averaging_scores(self):
        first = self.ingest(self.source('one', [row('a'), row('b')]))
        second = self.ingest(self.source('two', [row('b', 20), row('c', 20)], date='2026-11-02T00:00:00Z'))
        self.assertEqual(second['cumulative_unique_rows'], 3)
        self.assertEqual(second['new_identities'], 1)
        self.assertEqual(self.read(first)['sample_size'], 2)
        self.assertEqual(self.read(second)['features']['low_activity_rate']['value'], 1/3)
        self.assertEqual(self.read(second, 'snapshot')['features']['low_activity_rate']['value'], 0)
        self.assertIn('Historical union', self.read(second)['limitations'][-1])

    def test_idempotent_import(self):
        source = self.source('one', [row('a')])
        first, second = self.ingest(source), self.ingest(source)
        self.assertEqual(first['run_id'], second['run_id'])
        self.assertEqual(second['status'], 'already_imported')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 1)

    def test_separate_accounts_with_overlapping_followers(self):
        self.ingest(self.source('alice', [row('a')]))
        other = self.ingest(self.source('bob', [row('a'), row('b')], target='@bob@example.org'))
        self.assertEqual(other['previous_unique_rows'], 0)
        comparison = self.store.refresh_exports()['accounts']
        self.assertEqual([r['cumulative_observed_followers'] for r in comparison], [1, 2])

    def test_backfill_does_not_revert_newer_data(self):
        self.ingest(self.source('new', [row('a', 20)], date='2026-11-03T00:00:00Z'))
        older = self.ingest(self.source('old', [row('a', 0), row('b', 0)]))
        self.assertTrue(older['historical_backfill'])
        self.assertEqual(self.read(older)['features']['low_activity_rate']['value'], .5)
        comparison = self.store.refresh_exports()['accounts'][0]
        self.assertEqual(comparison['snapshot_sample_size'], 1)
        self.assertEqual(comparison['cumulative_observed_followers'], 2)
        self.assertTrue(comparison['latest_collected_at'].startswith('2026-11-03'))

    def test_account_alias_url_and_case(self):
        self.ingest(self.source('handle', [row('a')], target='@Alice@example.org'))
        second = self.ingest(self.source('url', [row('a')], target='https://example.org/@alice', date='2026-11-02T00:00:00Z'))
        self.assertEqual(second['previous_unique_rows'], 1)

    def test_conflicting_timestamp_rejected_and_history_preserved(self):
        self.ingest(self.source('one', [row('a')]))
        with self.assertRaisesRegex(ValueError, 'Conflicting records'):
            self.ingest(self.source('two', [row('a', 20)]))
        self.assertEqual(self.store.refresh_exports()['accounts'][0]['snapshot_feature_rates']['low_activity_rate'], 1)

    def test_failed_or_wrong_account_import_rejected(self):
        self.ingest(self.source('one', [row('a')]))
        bad = self.source('bad', [row('b')], error='rate limited', stop_reason='partial_error')
        with self.assertRaises(ValueError):
            self.ingest(bad)
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.ingest(self.source('other', [row('b')]), account='@bob@example.org')
        self.assertEqual(len(self.store.refresh_exports()['accounts']), 1)

    def test_different_config_suppresses_score_delta(self):
        self.ingest(self.source('one', [row('a')]))
        self.store.ingest(self.source('two', [row('a')], date='2026-11-02T00:00:00Z'), config=Config(minimum_feature_sample=2), replicates=0)
        entry = self.store.refresh_exports()['accounts'][0]
        self.assertFalse(entry['previous_snapshot_comparable_settings'])
        self.assertIsNone(entry['score_change_from_previous_snapshot'])

    def test_collect_command_wires_collection_into_history(self):
        source = self.source('scrape', [row('a')])
        with patch('run_pipeline.collect_to_store', return_value=source) as collect, redirect_stdout(StringIO()):
            result = main(['collect', '@alice@example.org', '--store', str(self.store.directory), '--bootstrap', '0'])
        self.assertEqual(result, 0)
        collect.assert_called_once()
        self.assertEqual(len(self.store.refresh_exports()['accounts']), 1)

    def test_reopen_store_persists_history_and_repairs_exports(self):
        run = self.ingest(self.source('one', [row('a')]))
        report = Path(run['run_directory'])/'snapshot'/'report.md'
        report.unlink()
        reopened = HistoryStore(self.store.directory)
        self.assertEqual(len(reopened.refresh_exports()['accounts']), 1)
        self.assertTrue(report.exists())

    def test_concurrent_imports_do_not_lose_followers(self):
        one = self.source('one', [row('a'), row('b')])
        two = self.source('two', [row('b', 20), row('c')], date='2026-11-02T00:00:00Z')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.ingest, [one, two]))
        self.assertEqual(len(results), 2)
        self.assertEqual(self.store.refresh_exports()['accounts'][0]['cumulative_observed_followers'], 3)

    def test_live_adapter_collection_export_and_analysis(self):
        raw = [{'id': 'a', 'username': 'a', 'acct': 'a', 'followers_count': 2,
                'following_count': 10, 'statuses_count': 3, 'created_at': '2026-01-01T00:00:00Z',
                'bot': False, 'note': '<p>Example</p>', 'avatar': 'https://example.org/accounts/avatars/a.png'}]
        with patch('run_pipeline.MastodonAdapter') as adapter, redirect_stdout(StringIO()):
            adapter.return_value.collect.return_value = ({'id': 'target', 'followers_count': 100}, raw, 'limit_reached', None)
            adapter.return_value.requests = []
            result = main(['collect', '@alice@example.org', '--store', str(self.store.directory), '--bootstrap', '0'])
        self.assertEqual(result, 0)
        self.assertEqual(len(list((self.store.directory/'collections').glob('*/followers_normalized.json'))), 1)
        self.assertEqual(self.store.refresh_exports()['accounts'][0]['snapshot_sample_size'], 1)

    def test_bridge_handle_fails_with_actionable_message_before_network(self):
        instance, username = parse_target('@tomgauld.bsky.social@bsky.brid.gy\n')
        self.assertEqual(username, 'tomgauld.bsky.social')
        with self.assertRaisesRegex(CollectionError, 'bridged Bluesky'):
            MastodonAdapter(instance)


if __name__ == '__main__':
    unittest.main()
