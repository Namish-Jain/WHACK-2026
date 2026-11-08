import math
from dataclasses import asdict, dataclass, field

COMPONENTS = ('benford', 'ratio', 'activity', 'new_account', 'profile')


@dataclass(frozen=True)
class Config:
    low_activity_max_posts: int = 3
    extreme_smoothed_ratio: float = 10.0
    new_account_days: float = 30.0
    incomplete_max_fraction: float = 1 / 3
    benford_mad_full_scale: float = 0.06
    minimum_feature_sample: int = 30
    benford_minimum_log10_span: float = 2.0
    weights: dict = field(default_factory=lambda: dict(zip(COMPONENTS, (0.35, 0.25, 0.20, 0.10, 0.10))))

    def __post_init__(self):
        for name in ('low_activity_max_posts', 'minimum_feature_sample'):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name == 'minimum_feature_sample' else 0):
                raise ValueError(f'{name} has an invalid integer value')
        for name in ('extreme_smoothed_ratio', 'new_account_days', 'incomplete_max_fraction', 'benford_mad_full_scale', 'benford_minimum_log10_span'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f'{name} must be finite')
        if self.extreme_smoothed_ratio <= 1 or self.new_account_days <= 0 or self.benford_mad_full_scale <= 0:
            raise ValueError('Ratio must exceed one; age and MAD scale must be positive')
        if not 0 <= self.incomplete_max_fraction <= 1 or self.benford_minimum_log10_span < 0:
            raise ValueError('Invalid completeness threshold or Benford span')
        if set(self.weights) != set(COMPONENTS):
            raise ValueError('Weights must contain exactly: ' + ', '.join(COMPONENTS))
        if any(isinstance(w, bool) or not isinstance(w, (int, float)) or not math.isfinite(w) or w < 0 for w in self.weights.values()) or sum(self.weights.values()) <= 0:
            raise ValueError('Weights must be finite, nonnegative, and sum to a positive value')

    def to_dict(self):
        return asdict(self)
