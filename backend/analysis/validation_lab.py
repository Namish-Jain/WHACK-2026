"""Reproducible synthetic stress tests. Never trains or modifies the real score."""
import hashlib
import math
import random
import statistics
from collections import Counter
from dataclasses import replace
from datetime import timedelta

from models import FollowerProfile
from .benford import analyze_benford
from .config import Config
from .features import extract_features, quantile
from .scoring import calculate_anomaly_score

SCENARIOS = {
    'high_following_low_activity': {
        'kind': 'planted_pattern', 'description': 'Selected profiles have 0–9 followers, 1,000–5,000 follows, and 0–3 posts.'},
    'recent_creation_burst': {
        'kind': 'planted_pattern', 'description': 'Selected profiles were created within the last seven days; other fields stay unchanged.'},
    'combined_pattern': {
        'kind': 'planted_pattern', 'description': 'High following/low posting plus recent creation, no avatar, and empty bio.'},
    'quiet_readers': {
        'kind': 'benign_control', 'description': 'Old, otherwise complete profiles with balanced counts but 0–3 posts; benign by construction.'},
    'declared_automation': {
        'kind': 'benign_control', 'description': 'Old, complete profiles marked as bots with 100–10,000 posts; benign by construction.'},
}
MASK_FIELDS = ('followers_count', 'following_count', 'posts_count', 'created_at', 'has_profile_picture', 'bio_length')


def seeded(seed, *parts):
    key = '|'.join(map(str, (seed,) + parts)).encode()
    return random.Random(int.from_bytes(hashlib.sha256(key).digest(), 'big'))


def make_baseline(size, as_of, rng):
    """An intentionally simple, fully specified reference generator, not real users."""
    if type(size) is not int or size < 1 or as_of.tzinfo is None:
        raise ValueError('Baseline needs positive size and a timezone-aware reference time')
    profiles = []
    for i in range(size):
        followers = int(10 ** rng.uniform(1, 4))
        profiles.append(FollowerProfile(
            username=f'simulated_{i}', profile_key=f'synthetic:{i}', followers_count=followers,
            following_count=max(0, round((followers + 1) * rng.uniform(.5, 2) - 1)),
            posts_count=rng.randint(10, 1000), created_at=as_of - timedelta(days=rng.uniform(180, 3650)),
            is_bot=False, has_profile_picture=True, bio_length=rng.randint(20, 200), display_name=f'Simulated {i}',
        ))
    return profiles


def plant_scenario(baseline, scenario, fraction, as_of, rng):
    if scenario not in SCENARIOS or not math.isfinite(fraction) or not 0 <= fraction <= 1:
        raise ValueError('Unknown scenario or invalid injection fraction')
    count = math.floor(len(baseline) * fraction + .5)
    order = list(range(len(baseline)))
    rng.shuffle(order)
    result = list(baseline)
    # Same seed gives nested selected sets and the same changes at each dose.
    for i in order[:count]:
        changes = {}
        if scenario in ('high_following_low_activity', 'combined_pattern'):
            changes.update(followers_count=rng.randint(0, 9), following_count=rng.randint(1000, 5000), posts_count=rng.randint(0, 3))
        if scenario in ('recent_creation_burst', 'combined_pattern'):
            changes['created_at'] = as_of - timedelta(days=rng.uniform(0, 7))
        if scenario == 'combined_pattern':
            changes.update(has_profile_picture=False, bio_length=0)
        if scenario == 'quiet_readers':
            changes['posts_count'] = rng.randint(0, 3)
        if scenario == 'declared_automation':
            changes.update(is_bot=True, posts_count=rng.randint(100, 10000))
        result[i] = replace(baseline[i], **changes)
    return result, count


def mask_profiles(profiles, probability, rng):
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError('Missingness probability must lie in [0, 1]')
    missing = 0
    result = []
    for profile in profiles:
        changes = {}
        for field in MASK_FIELDS:
            if rng.random() < probability:
                changes[field] = None
                missing += 1
        result.append(replace(profile, **changes))
    return result, missing


def measure(profiles, as_of, config):
    benford = analyze_benford(p.followers_count for p in profiles)
    features, _ = extract_features(profiles, as_of, config)
    scoring = calculate_anomaly_score(benford, features, config)
    return {
        'anomaly_score': scoring['anomaly_score'], 'severity': scoring['severity'],
        'active_components': [k for k, v in scoring['components'].items() if v['included']],
        'configured_weight_coverage': scoring['configured_weight_coverage'],
        'benford_mad': benford['mad'], 'benford_n_valid': benford['n_valid'],
        'rates': {k: features[k]['value'] for k in ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate')},
        'denominators': {k: features[k]['n_valid'] for k in ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate')},
    }


def numeric_summary(values):
    valid = [v for v in values if v is not None]
    return {'n_valid': len(valid), 'n_unavailable': len(values) - len(valid),
            'mean': statistics.mean(valid) if valid else None,
            'median': statistics.median(valid) if valid else None,
            'p025': quantile(valid, .025), 'p975': quantile(valid, .975)}


def run_validation(as_of, config=None, sample_sizes=(50, 100, 300),
                   injection_rates=(0, .1, .25, .5, .75, 1), missing_rates=(0, .1, .3), replicates=20, seed=42):
    config = config or Config()
    if as_of.tzinfo is None or type(replicates) is not int or replicates < 1 or type(seed) is not int:
        raise ValueError('Use a timezone-aware reference time, positive integer replicates, and integer seed')
    if not sample_sizes or any(type(n) is not int or n < 1 for n in sample_sizes):
        raise ValueError('Sample sizes must be positive integers')
    for rates in (injection_rates, missing_rates):
        if not rates or any(not math.isfinite(r) or not 0 <= r <= 1 for r in rates):
            raise ValueError('Injection and missingness rates must lie in [0, 1]')
    sizes, doses, missingness = sorted(set(sample_sizes)), sorted(set(injection_rates)), sorted(set(missing_rates))
    if 0 not in doses:
        doses.insert(0, 0.0)
    trials, grouped = [], {}
    for size in sizes:
        for replicate in range(replicates):
            base = make_baseline(size, as_of, seeded(seed, 'baseline', size, replicate))
            baseline_results = {}
            for missing_rate in missingness:
                masked, _ = mask_profiles(base, missing_rate, seeded(seed, 'mask', size, replicate))
                baseline_results[missing_rate] = measure(masked, as_of, config)
            for scenario in SCENARIOS:
                for dose in doses:
                    planted, planted_count = plant_scenario(base, scenario, dose, as_of, seeded(seed, 'plant', size, replicate, scenario))
                    for missing_rate in missingness:
                        masked, missing_count = mask_profiles(planted, missing_rate, seeded(seed, 'mask', size, replicate))
                        measured = measure(masked, as_of, config)
                        reference = baseline_results[missing_rate]
                        score, baseline_score = measured['anomaly_score'], reference['anomaly_score']
                        comparable = measured['active_components'] == reference['active_components']
                        trial = {'scenario': scenario, 'kind': SCENARIOS[scenario]['kind'], 'sample_size': size,
                                 'injection_rate': dose, 'injected_profiles': planted_count,
                                 'actual_injection_rate': planted_count / size, 'missing_rate': missing_rate,
                                 'actual_missing_field_rate': missing_count / (size * len(MASK_FIELDS)),
                                 'replicate': replicate, **measured,
                                 'paired_baseline_score': baseline_score,
                                 'same_active_components_as_baseline': comparable,
                                 'paired_score_change': score - baseline_score if score is not None and baseline_score is not None else None,
                                 'comparable_score_change': score - baseline_score if comparable and score is not None and baseline_score is not None else None}
                        trials.append(trial)
                        grouped.setdefault((scenario, size, dose, missing_rate), []).append(trial)
    summaries = []
    for (scenario, size, dose, missing_rate), rows in sorted(grouped.items()):
        scored = [r['anomaly_score'] for r in rows if r['anomaly_score'] is not None]
        summaries.append({
            'scenario': scenario, 'kind': SCENARIOS[scenario]['kind'], 'sample_size': size,
            'injection_rate': dose, 'actual_injection_rate': rows[0]['actual_injection_rate'],
            'missing_rate': missing_rate, 'replicates': replicates,
            'score': numeric_summary([r['anomaly_score'] for r in rows]),
            'paired_score_change': numeric_summary([r['paired_score_change'] for r in rows]),
            'comparable_score_change': numeric_summary([r['comparable_score_change'] for r in rows]),
            'component_set_changed_replicates': sum(not r['same_active_components_as_baseline'] for r in rows),
            'high_score_count': sum(s > 60 for s in scored), 'high_score_denominator': len(scored),
            'high_score_rate': sum(s > 60 for s in scored) / len(scored) if scored else None,
            'category_counts': dict(Counter(r['severity'] for r in rows)),
            'active_component_patterns': dict(Counter(','.join(r['active_components']) or 'none' for r in rows)),
            'mean_rates': {k: numeric_summary([r['rates'][k] for r in rows]) for k in rows[0]['rates']},
        })
    report = {
        'schema_version': '1.0', 'dataset_kind': 'synthetic_validation_only', 'generator_version': 'synthetic-v1',
        'as_of': as_of.isoformat(), 'seed': seed, 'config': config.to_dict(), 'scenarios': SCENARIOS,
        'sample_sizes': sizes, 'injection_rates': doses, 'missing_rates': missingness,
        'replicates_per_condition': replicates, 'trial_count': len(trials), 'summaries': summaries,
        'generator': 'Baseline: floor(10**Uniform(1,4)) followers; smoothed follow ratio Uniform(0.5,2); '
                     '10–1,000 posts; 180–3,650 days old; avatar present and bio length 20–200. '
                     'Each dose replaces round-half-up(n*dose) profiles in a seeded nested random subset. '
                     'Missingness masks each of six scoring fields independently with the requested probability.',
        'limitations': [
            'All profiles are synthetic. Scenario labels are design assumptions, not real-world authenticity labels.',
            'Score intervals are 2.5/97.5 percentiles across simulated replicates, not population confidence intervals.',
            'High-score rate means score > 60 among scored replicates. On benign controls it is a simulated false-alarm diagnostic, not a measured real-world false-positive rate.',
            'Missingness can exclude scoring components and renormalize weights. Comparable-score changes require identical active components.',
            'The generator is intentionally simple. A good result here does not validate accuracy on real networks.',
            'This experiment does not change weights, thresholds, or saved real account history.',
        ],
    }
    return report, trials
