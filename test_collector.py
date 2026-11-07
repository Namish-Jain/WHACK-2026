import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

path = Path(__file__).resolve().parents[1] / 'outputs' / 'collect_mastodon.py'
spec = importlib.util.spec_from_file_location('collector', path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CollectorTests(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(m.parse_target('@person@example.org'), ('https://example.org', 'person'))
        self.assertEqual(m.parse_target('https://example.org/@person'), ('https://example.org', 'person'))
        with self.assertRaises(m.CollectionError):
            m.parse_target('person')

    def test_missing_values_and_bio(self):
        row = m.normalize({'username': 'one', 'note': '<p>A &amp; B</p><p>C</p>', 'followers_count': 0}, 'https://example.org', 'now')
        self.assertEqual(row['username'], 'one@example.org')
        self.assertEqual(row['bio'], 'A & B C')
        self.assertEqual(row['bio_length'], 7)
        self.assertEqual(row['followers_count'], 0)
        self.assertIsNone(row['posts_count'])
        self.assertIsNone(row['is_bot'])
        self.assertIsNone(row['has_profile_picture'])

    def test_avatar(self):
        row = m.normalize({'avatar': 'https://example.org/avatars/original/missing.png'}, 'https://example.org', 'now')
        self.assertIs(row['has_profile_picture'], False)

    def test_links(self):
        self.assertEqual(m.next_page('<https://example.org/a?max_id=99>; rel="next", <x>; rel="prev"', 'https://example.org/a'), 'https://example.org/a?max_id=99')
        self.assertIsNone(m.next_page(None, 'https://example.org/a'))

    def test_cross_instance(self):
        with self.assertRaises(m.CollectionError):
            m.MastodonAdapter('https://example.org', 'dummy').get('https://other.example/a')

    def test_deduplication(self):
        adapter = m.MastodonAdapter('https://example.org')
        responses = iter([({'id': 'target'}, {}), ([{'id': '1'}, {'id': '2'}], {'Link': '<https://example.org/next>; rel="next"'}), ([{'id': '2'}, {'id': '3'}], {})])
        adapter.get = lambda url: next(responses)
        _, records, reason, error = adapter.collect('person', 3)
        self.assertEqual([a['id'] for a in records], ['1', '2', '3'])
        self.assertIsNone(error)

    def test_hidden(self):
        adapter = m.MastodonAdapter('https://example.org')
        adapter.get = lambda url: ({'id': 'target', 'hide_collections': True}, {})
        self.assertEqual(adapter.collect('person', 300)[2], 'hidden_network')

    def test_partial(self):
        adapter = m.MastodonAdapter('https://example.org')
        def get(url):
            if 'lookup' in url:
                return {'id': 'target'}, {}
            if 'followers' in url:
                return [{'id': '1'}], {'Link': '<https://example.org/next>; rel="next"'}
            raise m.CollectionError('rate limited')
        adapter.get = get
        _, records, reason, error = adapter.collect('person', 300)
        self.assertEqual(len(records), 1)
        self.assertEqual(reason, 'partial_error')
        self.assertEqual(error, 'rate limited')

    def test_export(self):
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / 'data'
            m.export(out, {}, [{'id': '1', 'username': 'one', 'bot': False}], {'instance': 'https://example.org', 'collected_at': 'now'})
            records = json.loads((out / 'followers_normalized.json').read_text())
            self.assertIsNone(records[0]['followers_count'])
            self.assertEqual(len(list(out.iterdir())), 5)


if __name__ == '__main__':
    unittest.main()