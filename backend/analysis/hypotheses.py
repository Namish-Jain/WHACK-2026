"""Exploratory, reference-conditional tests; never changes the heuristic score."""
import math
from dataclasses import dataclass, field, asdict

RATE_NAMES = ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate')
P_FLOOR = 1e-300


@dataclass(frozen=True)
class HypothesisConfig:
    alpha: float = 0.05
    reference_rates: dict = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.alpha, bool) or not isinstance(self.alpha, (float, int)) or not math.isfinite(self.alpha) or not 0 < self.alpha < 1:
            raise ValueError('Hypothesis alpha must be finite and between zero and one')
        if not isinstance(self.reference_rates, dict) or set(self.reference_rates) - set(RATE_NAMES):
            raise ValueError('Unknown reference-rate feature')
        for ref in self.reference_rates.values():
            if not isinstance(ref, dict) or set(ref) != {'rate', 'source'}:
                raise ValueError('Each reference needs exactly rate and source')
            p = ref['rate']
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 < p < 1:
                raise ValueError('Reference rates must be finite and strictly between zero and one')
            if not isinstance(ref['source'], str) or not ref['source'].strip() or len(ref['source']) > 1000:
                raise ValueError('Document the prespecified source of each reference rate')

    def to_dict(self):
        return asdict(self)


def logsumexp(values):
    values = list(values)
    largest = max(values)
    return largest + math.log(math.fsum(math.exp(v - largest) for v in values))


def chi_square_8_sf(statistic):
    """Chi-square survival with df=8: exp(-t) * sum(t**j/j!, j=0..3)."""
    if not math.isfinite(statistic) or statistic < 0:
        raise ValueError('Chi-square statistic must be finite and nonnegative')
    t = statistic / 2
    if t == 0:
        return 1.0
    logp = -t + logsumexp([j * math.log(t) - math.lgamma(j + 1) for j in range(4)])
    return min(1.0, max(P_FLOOR, math.exp(logp)))


def binomial_upper_tail(k, n, p):
    """Exact-model P[X >= k], evaluated in log space; no normal approximation."""
    if type(n) is not int or type(k) is not int or not 0 <= k <= n or not 0 < p < 1 or not math.isfinite(p):
        raise ValueError('Invalid binomial arguments')
    if k == 0:
        return 1.0
    logs = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p) for i in range(k, n + 1)]
    return min(1.0, max(P_FLOOR, math.exp(logsumexp(logs))))


def holm_adjust(pvalues, family_size=None):
    """Holm adjustment; planned but skipped tests count as p=1."""
    m = len(pvalues) if family_size is None else family_size
    if type(m) is not int or m < len(pvalues):
        raise ValueError('Family size cannot be smaller than the tested family')
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in pvalues):
        raise ValueError('Invalid p-value')
    result = [None] * len(pvalues)
    running = 0.0
    for rank, index in enumerate(sorted(range(len(pvalues)), key=pvalues.__getitem__)):
        running = max(running, min(1.0, (m - rank) * pvalues[index]))
        result[index] = running
    return result


def run_hypothesis_tests(benford, features, config=None):
    config = config or HypothesisConfig()
    tests = []
    b = {'id': 'benford_goodness_of_fit', 'method': 'Pearson chi-square goodness-of-fit',
         'null_hypothesis': 'Independent positive follower counts have first-digit probabilities log10(1 + 1/d).',
         'alternative_hypothesis': 'The first-digit probabilities differ from that reference.',
         'n_valid': benford['n_valid'], 'degrees_of_freedom': 8, 'effect_size_mad': benford['mad'],
         'status': 'not_tested', 'p_value': None, 'statistic': None,
         'reason': None, 'assumptions': ['Independent sampled profiles under the specified digit model.',
             'Benford applicability to follower counts is unestablished.',
             'API-order samples do not establish representative population sampling.']}
    chart = benford['chart_data']
    expected = [r['expected_count'] for r in chart]
    b['minimum_expected_count'] = min(expected) if expected else None
    if benford['n_valid'] < 30:
        b['reason'] = 'Fewer than 30 positive counts (conservative application gate).'
    elif benford['log10_span'] is None or benford['log10_span'] < 2:
        b['reason'] = 'Positive counts span less than two orders of magnitude.'
    elif len(chart) != 9 or min(expected) < 5:
        b['reason'] = 'At least one expected digit count is below five; asymptotic test withheld.'
    else:
        b['statistic'] = math.fsum((r['count'] - r['expected_count']) ** 2 / r['expected_count'] for r in chart)
        b['p_value'] = chi_square_8_sf(b['statistic'])
        b['status'] = 'computed_exploratory'
    tests.append(b)
    for name in RATE_NAMES:
        f = features[name]
        ref = config.reference_rates.get(name)
        test = {'id': name, 'method': 'One-sided exact binomial test', 'n_valid': f['n_valid'],
                'n_flagged': f['n_flagged'], 'observed_rate': f['value'], 'reference': ref,
                'null_hypothesis': 'The feature probability is at most the prespecified reference rate.' if ref else None,
                'alternative_hypothesis': 'The feature probability exceeds the prespecified reference rate.' if ref else None,
                'effect_size_percentage_points': 100 * (f['value'] - ref['rate']) if ref and f['value'] is not None else None,
                'p_value': None, 'status': 'not_tested', 'reason': None,
                'assumptions': ['Independent Bernoulli observations with a common feature probability.',
                                'Reference fixed before examining this audience and applicable to the same feature definition.',
                                'Reference-rate uncertainty is not modeled; an estimated reference needs a two-sample design.',
                                'Missing observations and API-order selection may be informative.']}
        if ref is None:
            test['reason'] = 'No prespecified reference rate supplied; an authenticity baseline is not invented.'
        elif f['n_valid'] < 30:
            test['reason'] = 'Fewer than 30 valid observations (conservative application gate).'
        else:
            test['p_value'] = binomial_upper_tail(f['n_flagged'], f['n_valid'], ref['rate'])
            test['status'] = 'computed_exploratory'
        tests.append(test)
    computed = [t for t in tests if t['p_value'] is not None]
    family_size = 1 + len(config.reference_rates)
    for test, adjusted in zip(computed, holm_adjust([t['p_value'] for t in computed], family_size)):
        test.update(p_value_holm=adjusted, reject_reference_at_alpha=adjusted <= config.alpha,
                    decision='reject_reference_under_assumptions' if adjusted <= config.alpha else 'do_not_reject_reference_under_assumptions',
                    p_value_at_numerical_floor=test['p_value'] <= P_FLOOR)
    for test in tests:
        test.setdefault('p_value_holm', None)
        test.setdefault('reject_reference_at_alpha', None)
        test.setdefault('decision', 'not_tested')
    return {'version': 'hypotheses-v1', 'config': config.to_dict(), 'alpha': config.alpha,
            'scope': 'Exploratory conditional comparisons within this saved sample; not population authenticity tests.',
            'multiple_testing': {'method': 'Holm', 'planned_family_size': family_size,
                                'computed_tests': len(computed), 'scope': 'This report only; no correction across repeated collections or accounts.'},
            'tests': tests, 'limitations': [
                'A p-value is a tail probability under a specified null model, not the probability the null is true or that followers are fake.',
                'Failure to reject a reference does not establish conformity or authenticity.',
                'Sampling, independence and reference-model assumptions are unverified for these data.',
                'Holm adjustment handles multiple tests within this report; it does not repair invalid individual p-values.',
                'Bootstrap ranges describe within-sample sensitivity and are separate from hypothesis tests.',
                'No significance test for audience overlap is supplied without a defensible population/reference model.',
                'P-values smaller than 1e-300 are conservatively reported as 1e-300, not zero.',
                'Tests do not change the existing heuristic score or calibrate its weights.']}
