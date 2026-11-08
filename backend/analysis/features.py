import math
import statistics


def quantile(values, probability):
    """Linear interpolation at index (n - 1) * p (including one-value inputs)."""
    if not 0 <= probability <= 1:
        raise ValueError('Probability must lie in [0, 1]')
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lo, hi = math.floor(index), math.ceil(index)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def median(values):
    return statistics.median(values) if values else None


def rate(values):
    valid = [v for v in values if v is not None]
    return {'value': sum(valid) / len(valid) if valid else None,
            'n_valid': len(valid), 'n_flagged': sum(valid), 'n_missing': len(values) - len(valid)}


def profile_features(profile, as_of, config):
    if as_of.tzinfo is None:
        raise ValueError('as_of must include timezone information')
    age = (as_of - profile.created_at).total_seconds() / 86400 if profile.created_at else None
    future_created = age is not None and age < 0
    age = None if future_created else age
    idle = (as_of.date() - profile.last_status_at.date()).days if profile.last_status_at else None
    idle = None if idle is not None and idle < 0 else idle
    imbalance = (math.log1p(profile.following_count) - math.log1p(profile.followers_count)
                 if profile.following_count is not None and profile.followers_count is not None else None)
    parts = [profile.has_profile_picture,
             profile.bio_length > 0 if profile.bio_length is not None else None,
             profile.posts_count > 0 if profile.posts_count is not None else None]
    # Complete-case denominator prevents unknown avatars being scored as absent.
    completeness = sum(parts) / 3 if all(p is not None for p in parts) else None
    return {
        'username': profile.username,
        'imbalance': imbalance,
        'extreme_ratio': ((profile.following_count + 1) / (profile.followers_count + 1) >= config.extreme_smoothed_ratio) if imbalance is not None else None,
        'low_activity': profile.posts_count <= config.low_activity_max_posts if profile.posts_count is not None else None,
        'account_age_days': age,
        'future_created_at': future_created,
        'new_account': age <= config.new_account_days if age is not None else None,
        'profile_completeness': completeness,
        'incomplete_profile': completeness <= config.incomplete_max_fraction if completeness is not None else None,
        'has_display_name': bool(profile.display_name.strip()) if profile.display_name is not None else None,
        'is_bot': profile.is_bot,
        'days_since_last_status': idle,
    }


def extract_features(profiles, as_of, config):
    rows = [profile_features(p, as_of, config) for p in profiles]
    present = lambda key: [r[key] for r in rows if r[key] is not None]
    counts = [p.followers_count for p in profiles if p.followers_count is not None]
    logs = [math.log1p(v) for v in counts]
    metrics = {name: rate([r[field] for r in rows]) for name, field in (
        ('extreme_ratio_rate', 'extreme_ratio'), ('low_activity_rate', 'low_activity'),
        ('new_account_rate', 'new_account'), ('incomplete_profile_rate', 'incomplete_profile'),
        ('declared_bot_rate', 'is_bot'), ('display_name_rate', 'has_display_name'))}
    metrics.update({
        'median_imbalance': median(present('imbalance')),
        'median_account_age_days': median(present('account_age_days')),
        'average_profile_completeness': statistics.mean(present('profile_completeness')) if present('profile_completeness') else None,
        'future_created_at_count': sum(r['future_created_at'] for r in rows),
        'last_activity': {'n_valid': len(present('days_since_last_status')),
                          'n_missing_or_future': len(rows) - len(present('days_since_last_status')),
                          'median_days_since_last_status': median(present('days_since_last_status'))},
        'follower_count_distribution': {
            'n_valid': len(counts), 'n_missing': len(profiles) - len(counts),
            'median': median(counts),
            'iqr_log1p': quantile(logs, .75) - quantile(logs, .25) if logs else None,
            'sample_std_log1p': statistics.stdev(logs) if len(logs) >= 2 else None,
            'minimum': min(counts) if counts else None, 'maximum': max(counts) if counts else None,
        },
    })
    return metrics, rows
