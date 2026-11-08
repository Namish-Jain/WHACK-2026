"""First-digit distribution for positive integer counts; zero has no first digit."""
import math


def first_digit(value):
    return int(str(value)[0]) if type(value) is int and value > 0 else None


def analyze_benford(values):
    values = list(values)
    positive = [x for x in values if first_digit(x) is not None]
    n = len(positive)
    counts = [0] * 9
    for value in positive:
        counts[first_digit(value) - 1] += 1
    expected = [math.log10(1 + 1 / digit) for digit in range(1, 10)]
    observed = [count / n for count in counts] if n else [None] * 9
    mad = sum(abs(a - b) for a, b in zip(observed, expected)) / 9 if n else None
    return {
        'n_valid': n,
        'n_zero_excluded': sum(type(v) is int and v == 0 for v in values),
        'n_missing_or_invalid': len(values) - n - sum(type(v) is int and v == 0 for v in values),
        'mad': mad,
        'log10_span': math.log10(max(positive)) - math.log10(min(positive)) if n else None,
        'occupied_orders_of_magnitude': len({len(str(v)) - 1 for v in positive}),
        'chart_data': [{'digit': d, 'count': counts[d-1], 'observed_proportion': observed[d-1],
                        'expected_proportion': expected[d-1], 'expected_count': n * expected[d-1]}
                       for d in range(1, 10)],
    }
