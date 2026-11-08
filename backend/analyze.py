#!/usr/bin/env python3
"""Analyze a Mastodon collector directory or normalized JSON/CSV file offline."""
import argparse
import json
import sys
from pathlib import Path
from analysis.config import Config
from analysis.hypotheses import HypothesisConfig
from analysis.pipeline import analyze
from data_io import load_profiles, parse_datetime, write_json


def render_report(report):
    def number(value, digits=4):
        return 'unavailable' if value is None else f'{value:.{digits}f}'
    scoring, features, benford = report['scoring'], report['features'], report['benford']
    lines = ['# Audience anomaly analysis', '',
             f"Target: {report['collection'].get('target_input') or 'unspecified'}",
             f"As of: {report['as_of']}", f"Unique followers analyzed: {report['sample_size']}", '',
             f"**Heuristic anomaly score: {number(scoring['anomaly_score'], 2)} / 100 ({scoring['severity']})**", '',
             'This score is not a fake-follower probability. Its categories are uncalibrated.', '',
             '| Feature | Value | Valid denominator |', '|---|---:|---:|',
             f"| Benford MAD | {number(benford['mad'], 6)} | {benford['n_valid']} positive counts |"]
    for key in ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate'):
        value = features[key]
        lines.append(f"| {key} | {number(100 * value['value'], 2) + '%' if value['value'] is not None else 'unavailable'} | {value['n_valid']} |")
    lines += ['', f"Benford excludes {benford['n_zero_excluded']} zero counts and {benford['n_missing_or_invalid']} missing/invalid counts.", '',
              '| Score component | Effective weight | Contribution (points) | Exclusion |', '|---|---:|---:|---|']
    for name, c in scoring['components'].items():
        lines.append(f"| {name} | {c['effective_weight']:.3f} | {c['contribution_points']:.3f} | {c['exclusion_reason'] or '—'} |")
    dist = features['follower_count_distribution']
    lines += ['', f"Median follower count: {number(dist['median'], 2)}.",
              f"IQR of ln(1 + followers): {number(dist['iqr_log1p'])}; sample standard deviation: {number(dist['sample_std_log1p'])}.",
              f"Median log imbalance: {number(features['median_imbalance'])}.",
              f"Median account age: {number(features['median_account_age_days'], 2)} days.",
              f"Mean completeness among fully observed profiles: {number(features['average_profile_completeness'])}.", '']
    if report['bootstrap']['status'] == 'computed':
        lines += ['## Bootstrap sensitivity', '',
                  report['bootstrap']['interpretation'], '',
                  '| Metric | 2.5th percentile | 97.5th percentile | Valid replicates |', '|---|---:|---:|---:|']
        for name, interval in report['bootstrap']['intervals'].items():
            lines.append(f"| {name} | {number(interval['lower'], 6)} | {number(interval['upper'], 6)} | {interval['valid_replicates']} |")
    tests = report.get('hypothesis_testing')
    if tests:
        lines += ['', '## Exploratory hypothesis tests', '', tests['scope'], '',
                  f"Alpha: {tests['alpha']}; correction: Holm within this report.", '',
                  '| Test | Raw p | Holm-adjusted p | Decision / reason |', '|---|---:|---:|---|']
        for test in tests['tests']:
            p = lambda v: 'unavailable' if v is None else f'{v:.6g}'
            lines.append(f"| {test['id']} | {p(test['p_value'])} | {p(test['p_value_holm'])} | {test['reason'] or test['decision']} |")
        lines += [''] + ['- ' + warning for warning in tests['limitations']]
    lines += ['', '## Limitations', ''] + ['- ' + warning for warning in report['limitations']]
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--out', type=Path, required=True, help='New output directory (will not overwrite)')
    parser.add_argument('--as-of', help='ISO-8601 reference date/time; defaults to collection_metadata.json collected_at')
    parser.add_argument('--hypotheses', type=Path, help='Prespecified reference rates and alpha as JSON')
    parser.add_argument('--config', type=Path, help='JSON overrides of Config defaults')
    parser.add_argument('--bootstrap', type=int, default=1000, help='Replicates; use 0 to disable')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args(argv)
    try:
        if args.out.exists():
            raise ValueError('Output directory already exists; choose a new --out')
        if args.bootstrap < 0:
            raise ValueError('--bootstrap must be nonnegative')
        config = Config(**json.loads(args.config.read_text())) if args.config else Config()
        hypothesis_config = HypothesisConfig(**json.loads(args.hypotheses.read_text())) if args.hypotheses else HypothesisConfig()
        profiles, metadata, quality = load_profiles(args.input)
        as_of = parse_datetime(args.as_of or metadata.get('collected_at'))
        if as_of is None:
            raise ValueError('No collection timestamp found; supply --as-of YYYY-MM-DD for reproducible account ages')
        report, summary, rows = analyze(profiles, as_of, config, metadata, quality, args.bootstrap, args.seed, hypothesis_config)
        args.out.mkdir(parents=True, exist_ok=False)
        write_json(args.out / 'statistics.json', report)
        write_json(args.out / 'llm_input.json', summary)
        write_json(args.out / 'profile_features.json', rows)
        write_json(args.out / 'benford_chart_data.json', report['benford']['chart_data'])
        (args.out / 'report.md').write_text(render_report(report), encoding='utf-8')
        print(json.dumps({'sample_size': len(profiles), 'anomaly_score': summary['anomaly_score'],
                          'severity': summary['severity'], 'out': str(args.out.resolve())}, indent=2))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f'Analysis failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
