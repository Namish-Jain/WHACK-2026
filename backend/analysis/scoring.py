from .config import COMPONENTS


def severity(score):
    if score is None:
        return 'insufficient_data'
    return 'low' if score <= 30 else 'moderate' if score <= 60 else 'high' if score <= 80 else 'very_high'


def calculate_anomaly_score(benford, features, config, fixed_components=None):
    """Weighted heuristic; fixed_components freezes bootstrap component selection."""
    specs = {'benford': (min(1.0, benford['mad'] / config.benford_mad_full_scale) if benford['mad'] is not None else None, benford['n_valid'])}
    for component, key in [('ratio', 'extreme_ratio_rate'), ('activity', 'low_activity_rate'),
                           ('new_account', 'new_account_rate'), ('profile', 'incomplete_profile_rate')]:
        specs[component] = (features[key]['value'], features[key]['n_valid'])
    components = {}
    for name in COMPONENTS:
        value, n = specs[name]
        reason = None
        if config.weights[name] == 0:
            reason = 'disabled_by_weight'
        elif value is None:
            reason = 'no_valid_observations'
        elif fixed_components is not None:
            if name not in fixed_components:
                reason = 'excluded_in_original_sample'
        elif n < config.minimum_feature_sample:
            reason = 'too_few_valid_observations'
        elif name == 'benford' and benford['log10_span'] < config.benford_minimum_log10_span:
            reason = 'insufficient_count_range_for_benford_heuristic'
        components[name] = {'normalized_value': value, 'n_valid': n, 'configured_weight': config.weights[name],
                            'included': reason is None, 'exclusion_reason': reason}
    total_weight = sum(c['configured_weight'] for c in components.values() if c['included'])
    for c in components.values():
        c['effective_weight'] = c['configured_weight'] / total_weight if c['included'] and total_weight else 0.0
        c['contribution_points'] = 100 * c['effective_weight'] * c['normalized_value'] if c['included'] else 0.0
    missing_fixed = fixed_components is not None and any(not components[name]['included'] for name in fixed_components)
    score = sum(c['contribution_points'] for c in components.values()) if total_weight and not missing_fixed else None
    return {'anomaly_score': score, 'severity': severity(score), 'components': components,
            'configured_weight_coverage': total_weight / sum(config.weights.values()),
            'interpretation': 'Uncalibrated audience anomaly indicator; not a probability or evidence that followers are fake.'}
