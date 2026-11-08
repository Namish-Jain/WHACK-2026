from .benford import analyze_benford
from .bootstrap import bootstrap_intervals
from .hypotheses import run_hypothesis_tests
from .config import Config
from .features import extract_features
from .scoring import calculate_anomaly_score, severity


def analyze(profiles, as_of, config=None, metadata=None, quality=None, bootstrap_replicates=1000, seed=42, hypothesis_config=None):
    config = config or Config()
    profiles = list(profiles)
    metadata = metadata or {}
    benford = analyze_benford(p.followers_count for p in profiles)
    features, rows = extract_features(profiles, as_of, config)
    scoring = calculate_anomaly_score(benford, features, config)
    b_component = scoring['components']['benford']
    benford['signal'] = severity(100 * b_component['normalized_value']) if b_component['included'] else 'not_scored'
    benford['scoring_eligible'] = b_component['included']
    warnings = [
        'Score weights, normalizations, thresholds, and sample-size gates are hackathon heuristics, not learned or validated against authenticity labels.',
        'Follower counts are not established to follow Benford\'s law. A mismatch alone is not evidence of manipulation; range and sample-size gates do not establish applicability.',
        'This sample is not established as random or representative. Its feature rates do not estimate the whole audience without additional sampling assumptions.',
        'Low posting activity can describe readers; declared bots can be legitimate; a sparse profile does not establish inauthenticity.',
        'Avatar presence inherits the collector\'s URL heuristic. Last-status dates do not measure logins and are not used in the anomaly score.',
    ]
    for name, component in scoring['components'].items():
        if not component['included']:
            warnings.append(f"Score component {name} excluded: {component['exclusion_reason']}. Available weights are renormalized.")
    if features['future_created_at_count']:
        warnings.append('Future creation dates were excluded from age features.')
    if quality and quality['invalid_values_by_field']:
        warnings.append('Malformed input values were treated as missing; see input_quality.')
    if metadata.get('error') or metadata.get('stop_reason') in ('partial_error', 'hidden_network'):
        warnings.append('Collection metadata reports an incomplete or inaccessible collection.')
    if metadata.get('sample_size') is not None and metadata['sample_size'] != len(profiles):
        warnings.append('Analysis row count differs from the sample size in collection metadata.')
    bootstrap = bootstrap_intervals(profiles, as_of, config, scoring, bootstrap_replicates, seed)
    hypothesis_testing = run_hypothesis_tests(benford, features, hypothesis_config)
    summary = {
        'sample_size': len(profiles), 'as_of': as_of.isoformat(), 'benford_mad': benford['mad'],
        'benford_signal': benford['signal'], 'benford_valid_sample_size': benford['n_valid'],
        **{k: features[k]['value'] for k in ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate')},
        'anomaly_score': scoring['anomaly_score'], 'severity': scoring['severity'],
        'feature_denominators': {k: features[k]['n_valid'] for k in ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate')},
        'score_components': scoring['components'], 'thresholds_and_weights': config.to_dict(),
        'bootstrap': bootstrap, 'hypothesis_testing': hypothesis_testing, 'limitations': warnings,
    }
    report = {'schema_version': '1.0', 'model_version': 'hackathon-heuristics-v1', 'sample_size': len(profiles),
              'as_of': as_of.isoformat(), 'config': config.to_dict(), 'input_quality': quality,
              'collection': {k: metadata.get(k) for k in ('target_input', 'collected_at', 'reported_followers_count', 'sampling_method', 'stop_reason', 'limitations')},
              'benford': benford, 'features': features, 'scoring': scoring, 'bootstrap': bootstrap, 'hypothesis_testing': hypothesis_testing, 'limitations': warnings}
    return report, summary, rows
