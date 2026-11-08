#!/usr/bin/env python3
"""Collect/import -> deduplicate -> analyze -> save history -> update comparison."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from analysis.config import Config
from analysis.hypotheses import HypothesisConfig
from llm.env_file import load_env_file
from llm.explain import explain_report, load_saved_report, GeminiError
from collect_mastodon import CollectionError, MastodonAdapter, export, parse_target
from history import HistoryStore, canonical_account
from data_io import parse_datetime
from analysis.validation_lab import run_validation
from analysis.audience_network import load_latest_snapshots, analyze_network
from research_reports import export_validation, export_network


def collect_to_store(account, store, limit, adapter_factory=None):
    if limit < 1:
        raise ValueError('--limit must be positive')
    instance, username = parse_target(account)
    started = datetime.now(timezone.utc)
    adapter = (adapter_factory or MastodonAdapter)(instance, os.getenv('MASTODON_ACCESS_TOKEN'))
    target, followers, reason, error = adapter.collect(username, limit)
    # Public lookup redirects may resolve an alias to the actual API host.
    # Keep the user's target key, but scope normalized follower records to the
    # host that supplied them. The standard CLI adapter does not redirect.
    resolved_instance = getattr(adapter, 'instance', instance)
    if isinstance(resolved_instance, str):
        instance = resolved_instance
    directory = store.directory / 'collections' / (started.strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid4().hex[:8])
    metadata = {'target_input': account, 'instance': instance, 'collected_at': started.isoformat(),
                'finished_at': datetime.now(timezone.utc).isoformat(), 'sample_size': len(followers),
                'requested_sample_size': limit, 'reported_followers_count': target.get('followers_count'),
                'stop_reason': reason, 'error': error, 'requests': adapter.requests,
                'sampling_method': 'First unique accessible followers in API order; not random.',
                'limitations': ['Remote account values may be stale or incomplete.',
                                'Avatar presence is a URL heuristic with unknown values preserved.']}
    export(directory, target, followers, metadata)
    if error or reason in ('partial_error', 'hidden_network'):
        raise ValueError(f'Collection could not complete; existing history unchanged. Saved available data: {directory}. Reason: {error or reason}')
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest='command', required=True)
    for name in ('collect', 'import', 'compare'):
        sub = subparsers.add_parser(name)
        sub.add_argument('--store', type=Path, default=Path(__file__).resolve().parent / 'history',
                         help='Persistent history directory; reuse it across runs')
        if name != 'compare':
            sub.add_argument('--config', type=Path)
            sub.add_argument('--hypotheses', type=Path, help='Prespecified reference rates and alpha')
            sub.add_argument('--explain', action='store_true', help='Ask Gemini to explain the saved snapshot')
            sub.add_argument('--model', help='Gemini model override; otherwise GEMINI_MODEL or default')
            sub.add_argument('--bootstrap', type=int, default=1000)
            sub.add_argument('--seed', type=int, default=42)
        if name == 'collect':
            sub.add_argument('account', help='@username@server or profile URL')
            sub.add_argument('--limit', type=int, default=300)
        if name == 'import':
            sub.add_argument('source', type=Path, help='Existing normalized JSON, CSV, or collector directory')
            sub.add_argument('--account', help='Required only when collection metadata lacks the target account')
            sub.add_argument('--collected-at', help='Reference timestamp when metadata is absent; may explicitly override it')
    explain = subparsers.add_parser('explain', help='Explain a saved audience using Gemini; does not rescrape or change history')
    explain.add_argument('account', help='Saved @username@server or Mastodon profile URL')
    explain.add_argument('--store', type=Path, default=Path(__file__).resolve().parent / 'history')
    explain.add_argument('--view', choices=('snapshot', 'cumulative'), default='snapshot')
    explain.add_argument('--out', type=Path, help='New explanation directory')
    explain.add_argument('--hypotheses', type=Path, help='Prespecified reference rates and alpha')
    explain.add_argument('--model', help='Gemini model override')
    explain.add_argument('--prepare-only', action='store_true', help='Write the exact prompt and input without making an API call')
    lab = subparsers.add_parser('validate', help='Run synthetic stress tests without touching real account history')
    lab.add_argument('--out', type=Path, help='New report directory')
    lab.add_argument('--config', type=Path)
    lab.add_argument('--sample-sizes', type=lambda s: [int(v) for v in s.split(',')], default=[50, 100, 300])
    lab.add_argument('--injection-rates', type=lambda s: [float(v) for v in s.split(',')], default=[0, .1, .25, .5, .75, 1])
    lab.add_argument('--missing-rates', type=lambda s: [float(v) for v in s.split(',')], default=[0, .1, .3])
    lab.add_argument('--replicates', type=int, default=20)
    lab.add_argument('--seed', type=int, default=42)
    lab.add_argument('--as-of', default='2026-01-01T00:00:00Z', help='Fixed reference date for simulated ages')
    network = subparsers.add_parser('network', help='Analyze overlap among latest saved audience snapshots')
    network.add_argument('--store', type=Path, default=Path(__file__).resolve().parent / 'history')
    network.add_argument('--out', type=Path, help='New report directory')
    network.add_argument('--account', action='append', help='Optional target filter; repeat for multiple accounts')
    network.add_argument('--config', type=Path)
    network.add_argument('--min-shared', type=int, default=2)
    network.add_argument('--min-jaccard', type=float, default=.01)
    args = parser.parse_args(argv)
    try:
        load_env_file(Path(__file__).resolve().parent.parent / '.env')
        if getattr(args, 'bootstrap', 0) < 0:
            raise ValueError('--bootstrap must be nonnegative')
        config = Config(**json.loads(args.config.read_text())) if getattr(args, 'config', None) else Config()
        hypothesis_config = HypothesisConfig(**json.loads(args.hypotheses.read_text())) if getattr(args, 'hypotheses', None) else None
        if args.command == 'explain':
            report = load_saved_report(args.store, args.account, args.view)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            out = args.out or args.store / 'explanations' / ('explain_' + stamp + '_' + uuid4().hex[:8])
            result = explain_report(report, out, hypothesis_config, args.model, args.prepare_only)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.command in ('validate', 'network'):
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            parent = Path(__file__).resolve().parent / 'experiments' if args.command == 'validate' else args.store / 'network'
            out = args.out or parent / (args.command + '_' + stamp)
            if out.exists():
                raise ValueError('Output directory already exists; choose a new --out')
            if args.command == 'validate':
                as_of = parse_datetime(args.as_of)
                if as_of is None:
                    raise ValueError('--as-of must be an ISO date/time')
                report, trials = run_validation(as_of, config, args.sample_sizes, args.injection_rates,
                                                args.missing_rates, args.replicates, args.seed)
                export_validation(out, report, trials)
                result = {'dataset_kind': report['dataset_kind'], 'trial_count': report['trial_count']}
            else:
                accounts = [canonical_account(a) for a in args.account] if args.account else None
                snapshots = load_latest_snapshots(args.store, accounts)
                report, graph = analyze_network(snapshots, config, args.min_shared, args.min_jaccard)
                export_network(out, report, graph)
                result = {k: report[k] for k in ('status', 'target_count', 'unique_follower_count', 'shared_follower_count')}
            result['report'] = str((out / 'report.html').resolve())
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        store = HistoryStore(args.store)
        if args.command == 'compare':
            result = store.refresh_exports()
        else:
            source = collect_to_store(args.account, store, args.limit) if args.command == 'collect' else args.source
            result = store.ingest(source, account=args.account,
                                  collected_at=getattr(args, 'collected_at', None), config=config,
                                  replicates=args.bootstrap, seed=args.seed, hypothesis_config=hypothesis_config)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        print('Comparison: ' + str(store.directory / 'comparison.md'))
        if getattr(args, 'explain', False):
            # Explain the exact imported run, including a historical backfill, not an unrelated newer snapshot.
            run_directory = store.directory / 'runs' / result['run_id']
            report = json.loads((run_directory / 'snapshot' / 'statistics.json').read_text())
            out = run_directory / 'snapshot' / 'explanations' / ('gemini_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
            try:
                print(json.dumps(explain_report(report, out, hypothesis_config, args.model), indent=2))
            except GeminiError as exc:
                print('Statistics saved successfully; explanation failed: ' + str(exc), file=sys.stderr)
                return 3
        return 0
    except (OSError, ValueError, TypeError, sqlite3.Error, CollectionError, GeminiError) as exc:
        print(f'Pipeline failed: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
