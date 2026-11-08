import csv
import json
import math
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from analysis.benford import analyze_benford, first_digit
from analysis.config import Config
from analysis.features import extract_features, profile_features, quantile
from analysis.pipeline import analyze
from analysis.scoring import calculate_anomaly_score, severity
from data_io import load_profiles
from models import FollowerProfile

AS_OF = datetime(2026, 11, 7, tzinfo=timezone.utc)


class BenfordTests(unittest.TestCase):
    def test_digits_and_invalids(self):
        self.assertEqual([first_digit(v) for v in [132, 5842, 76, 1103, 298, 9134]], [1, 5, 7, 1, 2, 9])
        for value in [0, -2, None, True, 1.2, '12']:
            self.assertIsNone(first_digit(value))

    def test_known_uniform_distribution(self):
        result = analyze_benford(range(1, 10))
        expected = sum(abs(1/9 - math.log10(1 + 1/d)) for d in range(1, 10))/9
        self.assertAlmostEqual(result['mad'], expected)
        self.assertAlmostEqual(sum(r['expected_proportion'] for r in result['chart_data']), 1)

    def test_empty_and_zero(self):
        result = analyze_benford([0, 0, None, -1])
        self.assertIsNone(result['mad'])
        self.assertEqual(result['n_zero_excluded'], 2)
        self.assertEqual(result['n_missing_or_invalid'], 2)


class FeatureTests(unittest.TestCase):
    def test_known_features(self):
        profiles = [FollowerProfile('a', 0, 99, 0, AS_OF - timedelta(days=10), False, False, 0),
                    FollowerProfile('b', 99, 0, 100, AS_OF - timedelta(days=100), False, True, 20)]
        f, rows = extract_features(profiles, AS_OF, Config())
        for name in ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate'):
            self.assertEqual(f[name]['value'], .5)
            self.assertEqual(f[name]['n_valid'], 2)
        self.assertEqual(f['median_account_age_days'], 55)
        self.assertEqual(f['average_profile_completeness'], .5)
        self.assertAlmostEqual(rows[0]['imbalance'], math.log(100))
        self.assertAlmostEqual(f['follower_count_distribution']['sample_std_log1p'], math.log(100) / math.sqrt(2))

    def test_unknown_not_zero_and_future_dates(self):
        profiles = [FollowerProfile('missing'), FollowerProfile('future', created_at=AS_OF + timedelta(days=1))]
        f, rows = extract_features(profiles, AS_OF, Config())
        self.assertIsNone(f['low_activity_rate']['value'])
        self.assertIsNone(f['incomplete_profile_rate']['value'])
        self.assertIsNone(f['new_account_rate']['value'])
        self.assertEqual(f['future_created_at_count'], 1)
        self.assertIsNone(rows[0]['profile_completeness'])

    def test_threshold_boundaries(self):
        p = FollowerProfile('edge', 1, 19, 3, AS_OF - timedelta(days=30), False, True, 0)
        f = profile_features(p, AS_OF, Config())
        self.assertTrue(f['extreme_ratio'])
        self.assertTrue(f['new_account'])
        self.assertTrue(f['low_activity'])
        self.assertFalse(f['incomplete_profile'])
        self.assertEqual(quantile([0, 10, 20, 30], .25), 7.5)

    def test_invalid_direct_inputs(self):
        with self.assertRaises(ValueError):
            FollowerProfile('invalid', followers_count=-1)
        with self.assertRaises(ValueError):
            FollowerProfile('invalid', is_bot='false')


class ScoreTests(unittest.TestCase):
    def test_weighted_arithmetic(self):
        f = {k: {'value': v, 'n_valid': 100} for k, v in zip(
            ['extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate'], [.2, .3, .4, .5])}
        score = calculate_anomaly_score({'mad': .03, 'n_valid': 100, 'log10_span': 3}, f, Config())
        self.assertAlmostEqual(score['anomaly_score'], 37.5)
        self.assertAlmostEqual(sum(c['contribution_points'] for c in score['components'].values()), 37.5)

    def test_missing_weight_renormalization(self):
        profiles = [FollowerProfile(str(i), posts_count=0) for i in range(30)]
        report, _, _ = analyze(profiles, AS_OF, bootstrap_replicates=0)
        self.assertEqual(report['scoring']['anomaly_score'], 100)
        self.assertEqual(report['scoring']['components']['activity']['effective_weight'], 1)
        self.assertAlmostEqual(report['scoring']['configured_weight_coverage'], .2)
        self.assertEqual(len([c for c in report['scoring']['components'].values() if c['included']]), 1)

    def test_empty_and_small_samples(self):
        for profiles in [[], [FollowerProfile('tiny', 1, 10, 0)]]:
            report, _, _ = analyze(profiles, AS_OF, bootstrap_replicates=0)
            self.assertIsNone(report['scoring']['anomaly_score'])
            self.assertEqual(report['scoring']['severity'], 'insufficient_data')
            json.dumps(report, allow_nan=False)

    def test_benford_range_gate_keeps_raw_measurement(self):
        profiles = [FollowerProfile(str(i), followers_count=5, posts_count=0) for i in range(40)]
        report, _, _ = analyze(profiles, AS_OF, bootstrap_replicates=0)
        self.assertIsNotNone(report['benford']['mad'])
        self.assertFalse(report['scoring']['components']['benford']['included'])
        self.assertEqual(report['benford']['signal'], 'not_scored')
        self.assertEqual(report['scoring']['components']['benford']['exclusion_reason'], 'insufficient_count_range_for_benford_heuristic')

    def test_fixed_bootstrap_components_do_not_renormalize_missing(self):
        f = {k: {'value': None, 'n_valid': 0} for k in ['extreme_ratio_rate', 'new_account_rate', 'incomplete_profile_rate']}
        f['low_activity_rate'] = {'value': 1.0, 'n_valid': 10}
        result = calculate_anomaly_score({'mad': None, 'n_valid': 0, 'log10_span': None}, f, Config(), fixed_components=['activity', 'ratio'])
        self.assertIsNone(result['anomaly_score'])

    def test_categories_and_configuration(self):
        self.assertEqual([severity(v) for v in [0, 30, 30.1, 60, 60.1, 80, 80.1, 100]],
                         ['low', 'low', 'moderate', 'moderate', 'high', 'high', 'very_high', 'very_high'])
        for values in [{'weights': {}}, {'new_account_days': -1}, {'benford_mad_full_scale': 0}, {'extreme_smoothed_ratio': float('nan')}]:
            with self.assertRaises(ValueError):
                Config(**values)

    def test_bootstrap_is_reproducible(self):
        profiles = [FollowerProfile(str(i), i * i, i*20, i%5, AS_OF - timedelta(days=i), False, bool(i%2), i) for i in range(40)]
        a = analyze(profiles, AS_OF, bootstrap_replicates=50, seed=7)[0]
        b = analyze(profiles, AS_OF, bootstrap_replicates=50, seed=7)[0]
        self.assertEqual(a, b)
        interval = a['bootstrap']['intervals']['anomaly_score']
        self.assertLessEqual(interval['lower'], interval['upper'])
        self.assertEqual(interval['valid_replicates'], 50)


class InputTests(unittest.TestCase):
    def test_csv_json_equivalence_and_invalid_values(self):
        records = [{'id': '0001', 'username': 'a', 'followers_count': 0, 'following_count': 20, 'posts_count': 0,
                    'created_at': '2026-11-01T00:00:00Z', 'is_bot': False, 'has_profile_picture': True, 'bio_length': 0},
                   {'id': '0002', 'username': 'b', 'followers_count': -1, 'following_count': 0, 'posts_count': None,
                    'created_at': 'invalid', 'is_bot': None, 'has_profile_picture': None, 'bio_length': None}]
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'followers.json').write_text(json.dumps(records))
            with (p/'followers.csv').open('w', newline='') as file:
                writer = csv.DictWriter(file, fieldnames=records[0])
                writer.writeheader()
                for row in records:
                    writer.writerow({k: str(v).lower() if type(v) is bool else v for k,v in row.items()})
            a, _, aq = load_profiles(p/'followers.json')
            b, _, bq = load_profiles(p/'followers.csv')
            self.assertEqual(a, b)
            self.assertEqual(aq['invalid_values_by_field'], {'followers_count': 1, 'created_at': 1})
            self.assertEqual(aq['invalid_values_by_field'], bq['invalid_values_by_field'])

    def test_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'data.json'
            p.write_text(json.dumps([{'id': '1', 'posts_count': 1}, {'id': '1', 'posts_count': 9}]))
            profiles, _, quality = load_profiles(p)
            self.assertEqual(len(profiles), 1)
            self.assertEqual(profiles[0].posts_count, 1)
            self.assertEqual(quality['duplicates_removed'], 1)


if __name__ == '__main__':
    unittest.main()
