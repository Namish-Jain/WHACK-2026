import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict, replace
from datetime import datetime, timezone, timedelta
from io import StringIO
from pathlib import Path

from analysis.config import Config
from analysis.validation_lab import (make_baseline, plant_scenario, mask_profiles, seeded, measure,
                                     run_validation, SCENARIOS, numeric_summary)
from analysis.audience_network import analyze_network, load_latest_snapshots, strong_identity
from history import HistoryStore, pack_profile
from models import FollowerProfile
from research_reports import validation_html, network_html
from run_pipeline import main

AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


class SyntheticTests(unittest.TestCase):
    def test_baseline_bounds_and_identity(self):
        rows = make_baseline(100, AS_OF, seeded(1))
        self.assertEqual(len({r.profile_key for r in rows}), 100)
        self.assertTrue(all(10 <= r.followers_count < 10000 and r.posts_count >= 10 for r in rows))
        self.assertTrue(all((AS_OF-r.created_at).days >= 180 for r in rows))

    def test_zero_dose_is_exact_baseline_and_input_is_unchanged(self):
        baseline = make_baseline(80, AS_OF, seeded(2))
        original = list(baseline)
        for scenario in SCENARIOS:
            planted, count = plant_scenario(baseline, scenario, 0, AS_OF, seeded(3))
            self.assertEqual(planted, baseline)
            self.assertEqual(count, 0)
            plant_scenario(baseline, scenario, 1, AS_OF, seeded(3))
        self.assertEqual(baseline, original)

    def test_nested_dose_and_exact_counts(self):
        baseline = make_baseline(51, AS_OF, seeded(2))
        a, na = plant_scenario(baseline, 'quiet_readers', .1, AS_OF, seeded(3))
        b, nb = plant_scenario(baseline, 'quiet_readers', .5, AS_OF, seeded(3))
        self.assertEqual((na, nb), (5, 26))
        first = {i for i in range(51) if a[i] != baseline[i]}
        second = {i for i in range(51) if b[i] != baseline[i]}
        self.assertLess(first, second)
        for i in first:
            self.assertEqual(a[i], b[i])

    def test_only_intended_features_change(self):
        base = make_baseline(100, AS_OF, seeded(4))
        old = measure(base, AS_OF, Config())
        burst, _ = plant_scenario(base, 'recent_creation_burst', 1, AS_OF, seeded(5))
        new = measure(burst, AS_OF, Config())
        self.assertEqual(new['rates']['new_account_rate'], 1)
        self.assertEqual(old['rates']['new_account_rate'], 0)
        self.assertEqual(old['benford_mad'], new['benford_mad'])
        self.assertAlmostEqual(new['anomaly_score'] - old['anomaly_score'], 10)
        bots, _ = plant_scenario(base, 'declared_automation', 1, AS_OF, seeded(5))
        self.assertTrue(all(p.is_bot for p in bots))
        self.assertEqual(old['anomaly_score'], measure(bots, AS_OF, Config())['anomaly_score'])

    def test_complete_missingness_produces_unavailable_score(self):
        base = make_baseline(50, AS_OF, seeded(4))
        masked, count = mask_profiles(base, 1, seeded(1))
        self.assertEqual(count, 300)
        self.assertIsNone(measure(masked, AS_OF, Config())['anomaly_score'])
        same, count = mask_profiles(base, 0, seeded(1))
        self.assertEqual((same, count), (base, 0))

    def test_reproducible_lab_and_report_arithmetic(self):
        kwargs = dict(sample_sizes=(50,), injection_rates=(.5, 1), missing_rates=(0, 1), replicates=3, seed=7)
        report, trials = run_validation(AS_OF, **kwargs)
        self.assertEqual((report, trials), run_validation(AS_OF, **kwargs))
        self.assertEqual(report['injection_rates'], [0, .5, 1])
        self.assertEqual(len(trials), 5*3*2*3)
        for summary in report['summaries']:
            rows = [t for t in trials if all(t[k] == summary[k] for k in ('scenario','sample_size','injection_rate','missing_rate'))]
            expected = numeric_summary([r['anomaly_score'] for r in rows])
            self.assertEqual(summary['score'], expected)
            self.assertEqual(sum(summary['category_counts'].values()), 3)
            if summary['missing_rate'] == 1:
                self.assertEqual(summary['high_score_denominator'], 0)
                self.assertIsNone(summary['high_score_rate'])
        json.dumps(report, allow_nan=False)

    def test_invalid_experiment_arguments(self):
        for kwargs in [{'sample_sizes': (0,)}, {'replicates': 0}, {'missing_rates': (float('nan'),)}, {'injection_rates': (1.1,)}]:
            with self.assertRaises(ValueError):
                run_validation(AS_OF, **kwargs)


def record(key, username=None, created=None, posts=1):
    return pack_profile(FollowerProfile(username or key.rsplit('/',1)[-1]+'@followers.example',
                                       followers_count=10, following_count=20, posts_count=posts,
                                       created_at=created or AS_OF-timedelta(days=5), has_profile_picture=True,
                                       bio_length=10, profile_key=key))


def snapshot(account, records, timestamp=AS_OF):
    return {'account': account, 'run_id': account + ':run', 'collected_at': timestamp.isoformat(),
            'profiles': records, 'metadata': {'reported_followers_count': 100}}


class NetworkTests(unittest.TestCase):
    def test_exact_overlap_and_graph_membership(self):
        a, b, c = [record('https://followers.example/users/'+k) for k in ('a','b','c')]
        report, graph = analyze_network([snapshot('x@example.org',[a,b]), snapshot('y@example.org',[b,c])], min_shared=1)
        pair = report['pairs'][0]
        self.assertEqual((pair['shared_count'],pair['union_count']), (1,3))
        self.assertAlmostEqual(pair['jaccard'], 1/3)
        self.assertEqual(pair['overlap_coefficient'], .5)
        self.assertEqual(report['shared_follower_count'], 1)
        self.assertEqual(len(graph['nodes']), 5)
        self.assertEqual(len(graph['edges']), 4)
        self.assertEqual(pair['shared_profile_metrics']['new_and_low_activity_rate']['value'], 1)
        self.assertEqual(report['connected_target_groups'], [['x@example.org','y@example.org']])

    def test_duplicates_disjoint_and_empty_audiences(self):
        a = record('https://followers.example/users/a')
        report, graph = analyze_network([snapshot('x@example.org',[a,a]),snapshot('y@example.org',[])])
        self.assertEqual(report['identity_quality']['duplicate_memberships_removed'], 1)
        self.assertEqual(report['pairs'][0]['jaccard'], 0)
        self.assertIsNone(report['pairs'][0]['overlap_coefficient'])
        self.assertEqual(len(graph['edges']), 1)
        empty, _ = analyze_network([snapshot('x@example.org',[]),snapshot('y@example.org',[])])
        self.assertIsNone(empty['pairs'][0]['jaccard'])
        self.assertEqual(analyze_network([])[0]['status'], 'insufficient_accounts')
        self.assertEqual(analyze_network([snapshot('x@example.org',[a])])[0]['pairs'], [])

    def test_newest_measurement_and_unknown_joint_denominator(self):
        a = record('https://followers.example/users/a')
        newer = dict(a, posts_count=100)
        report, _ = analyze_network([snapshot('x@example.org',[a]),snapshot('y@example.org',[newer], AS_OF+timedelta(days=1))])
        metrics = report['pairs'][0]['shared_profile_metrics']
        self.assertEqual(metrics['low_activity_rate']['value'], 0)
        self.assertEqual(report['snapshot_span_days'], 1)
        conflict, _ = analyze_network([snapshot('x@example.org',[a]),snapshot('y@example.org',[newer])])
        metrics = conflict['pairs'][0]['shared_profile_metrics']
        self.assertIsNone(metrics['new_and_low_activity_rate']['value'])
        self.assertEqual(metrics['new_and_low_activity_rate']['n_missing'], 1)

    def test_scoped_ids_do_not_collide_across_instances(self):
        a = record('https://a.example|id:1', username='local_a')
        b = record('https://b.example|id:1', username='local_b')
        report, _ = analyze_network([snapshot('x@example.org',[a]),snapshot('y@example.org',[b])])
        self.assertEqual(report['shared_follower_count'], 0)

    def test_unambiguous_handle_resolution_and_conflicting_actors(self):
        a = record('https://a.example/users/alice', username='alice@a.example')
        weak = record('https://view.example|id:2', username='alice@a.example')
        report, _ = analyze_network([snapshot('x@example.org',[a]),snapshot('y@example.org',[weak])])
        self.assertEqual(report['shared_follower_count'], 1)
        other = dict(a, profile_key='https://other.example/users/alice')
        conflict, _ = analyze_network([snapshot('x@example.org',[a]),snapshot('y@example.org',[other]),snapshot('z@example.org',[weak])])
        self.assertEqual(conflict['shared_follower_count'], 0)
        self.assertEqual(conflict['identity_quality']['ambiguous_handle_count'], 1)
        self.assertNotEqual(strong_identity('https://a.example/users/a'), strong_identity('https://a.example/users/a/'))

    def test_reports_escape_untrusted_content(self):
        payload = '</script><script>alert(1)</script>'
        a = record('https://followers.example/users/a', username=payload)
        report, _ = analyze_network([snapshot('x@example.org',[a]),snapshot('y@example.org',[a])])
        output = network_html(report)
        self.assertNotIn(payload, output)
        self.assertIn('\\u003c/script', output)


class ResearchIntegrationTests(unittest.TestCase):
    def test_latest_snapshot_selection_and_read_only_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = HistoryStore(root/'history')
            for i, names in enumerate([('a','b'),('c',)]):
                source=root/f'input{i}'; source.mkdir()
                (source/'followers_normalized.json').write_text(json.dumps([
                    {'uri': 'https://followers.example/users/'+name, 'username':name+'@followers.example','posts_count':1} for name in names]))
                (source/'collection_metadata.json').write_text(json.dumps({'target_input':'@target@example.org','collected_at':(AS_OF+timedelta(days=i)).isoformat()}))
                store.ingest(source, replicates=0)
            before=store.db_path.read_bytes()
            selected=load_latest_snapshots(store.directory)
            self.assertEqual(len(selected), 1)
            self.assertEqual(len(selected[0]['profiles']), 1)
            self.assertEqual(selected[0]['profiles'][0]['username'], 'c@followers.example')
            self.assertEqual(store.db_path.read_bytes(), before)
            with redirect_stdout(StringIO()):
                code=main(['network','--store',str(store.directory),'--out',str(root/'network')])
            self.assertEqual(code, 0)
            self.assertEqual(store.db_path.read_bytes(), before)
            self.assertTrue((root/'network/report.html').exists())
            with self.assertRaises(ValueError):
                load_latest_snapshots(store.directory, ['missing@example.org'])

    def test_validation_cli_outputs_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'lab'
            argv=['validate','--sample-sizes','30','--injection-rates','0,1','--missing-rates','0,1','--replicates','2','--out',str(out)]
            with redirect_stdout(StringIO()):
                self.assertEqual(main(argv), 0)
            self.assertEqual(len((out/'trials.jsonl').read_text().splitlines()), 40)
            report=json.loads((out/'validation.json').read_text())
            self.assertEqual(report['dataset_kind'],'synthetic_validation_only')
            before=(out/'validation.json').read_bytes()
            with redirect_stderr(StringIO()):
                self.assertEqual(main(argv), 2)
            self.assertEqual((out/'validation.json').read_bytes(), before)
            self.assertFalse((out/'history.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
