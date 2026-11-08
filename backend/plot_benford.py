#!/usr/bin/env python3
"""Optional Matplotlib chart: python plot_benford.py statistics.json --out chart.png"""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('statistics', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        from matplotlib.ticker import PercentFormatter
    except ImportError:
        parser.error('Charting requires matplotlib; install the project-root requirements.txt. The analysis itself uses only the standard library.')
    if args.out.exists():
        parser.error('Output already exists; choose a new filename')
    report = json.loads(args.statistics.read_text())
    b = report['benford']
    if not b['n_valid']:
        parser.error('No positive follower counts to plot')
    rows = b['chart_data']
    fig, ax = plt.subplots(figsize=(10, 6), layout='constrained')
    fig.set_facecolor('#f8fafc')
    ax.set_facecolor('#f8fafc')
    ax.bar([r['digit'] - .2 for r in rows], [r['expected_proportion'] for r in rows], width=.38, label='Benford reference', color='#a6b7c8')
    ax.bar([r['digit'] + .2 for r in rows], [r['observed_proportion'] for r in rows], width=.38, label='Observed sample', color='#087f8c')
    ax.set_xticks(range(1, 10))
    ax.set_xlabel('First significant digit of follower count')
    ax.set_ylabel('Share of positive counts')
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.spines[['top', 'right']].set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(axis='y', alpha=.2)
    ax.legend(frameon=False)
    ax.set_title(f"Audience first-digit distribution\n{b['n_valid']} positive counts · {b['n_zero_excluded']} zeros excluded · MAD {b['mad']:.4f}", loc='left', pad=18)
    fig.supxlabel('Descriptive comparison only. Benford deviation does not establish inauthenticity.', fontsize=10)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, dpi=180)
    plt.close(fig)


if __name__ == '__main__':
    main()
