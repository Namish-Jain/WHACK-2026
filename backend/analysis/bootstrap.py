"""Seeded row resampling, conditional on the observed convenience sample."""
import random
from .benford import analyze_benford
from .features import extract_features, quantile
from .scoring import calculate_anomaly_score


def bootstrap_intervals(profiles, as_of, config, base_scoring, replicates=1000, seed=42):
    if type(replicates) is not int or replicates < 0:
        raise ValueError('replicates must be a nonnegative integer')
    if replicates == 0 or len(profiles) < 2:
        return {'status': 'disabled' if replicates == 0 else 'insufficient_data', 'replicates': 0, 'seed': seed}
    rng = random.Random(seed)
    active = [name for name, c in base_scoring['components'].items() if c['included']]
    metrics = {k: [] for k in ('benford_mad', 'extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate', 'anomaly_score')}
    for _ in range(replicates):
        sample = [profiles[rng.randrange(len(profiles))] for _ in profiles]
        b = analyze_benford(p.followers_count for p in sample)
        f, _ = extract_features(sample, as_of, config)
        score = calculate_anomaly_score(b, f, config, fixed_components=active)['anomaly_score']
        values = {'benford_mad': b['mad'], 'anomaly_score': score}
        values.update({name: f[name]['value'] for name in metrics if name in f})
        for name, value in values.items():
            if value is not None:
                metrics[name].append(value)
    return {
        'status': 'computed', 'replicates': replicates, 'seed': seed,
        'method': 'Row bootstrap with replacement; percentile 2.5 and 97.5; original score components and weights held fixed.',
        'interpretation': 'Within-sample sensitivity only. Does not correct API-order selection bias, federation gaps, network dependence, or uncalibrated scoring.',
        'intervals': {name: {'lower': quantile(values, .025), 'upper': quantile(values, .975),
                             'valid_replicates': len(values), 'missing_replicates': replicates - len(values)}
                      for name, values in metrics.items()},
    }
