"""Transactional per-account snapshots, latest-observation unions, and comparisons."""
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from analysis.config import Config
from analysis.hypotheses import HypothesisConfig
from analysis.pipeline import analyze
from analyze import render_report
from collect_mastodon import parse_target
from data_io import load_profiles, parse_datetime
from models import FollowerProfile


def canonical_account(value):
    instance, username = parse_target(value)
    return username.lower() + '@' + urlsplit(instance).hostname.lower()


def pack_profile(profile):
    value = asdict(profile)
    for field in ('created_at', 'last_status_at'):
        value[field] = value[field].isoformat() if value[field] else None
    return value


def unpack_profile(value):
    value = dict(value)
    for field in ('created_at', 'last_status_at'):
        value[field] = parse_datetime(value[field])
    return FollowerProfile(**value)


def dumps(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            file.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n')


def documents(profiles, as_of, config, metadata, quality, replicates, seed, kind, hypothesis_config=None):
    report, llm, rows = analyze(profiles, as_of, config, metadata, quality, replicates, seed, hypothesis_config)
    report['dataset_kind'] = kind
    llm['dataset_kind'] = kind
    if kind == 'cumulative_observed_followers':
        warning = ('Historical union of observed followers, not a current follower census. '
                   'Each follower uses its newest observed record; unobserved older records are retained. '
                   'Absence from a later sample does not establish whether a follower still follows the account. '
                   'Counts and profile measurements can therefore have different observation dates.')
        report['limitations'].append(warning)
        # These lists share an object in analyze(); append only once.
    return {'statistics': report, 'llm_input': llm, 'profile_features': rows,
            'benford_chart_data': report['benford']['chart_data']}


class HistoryStore:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory / 'history.sqlite3'
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS runs (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL UNIQUE, account TEXT NOT NULL,
                collected_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
                records_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
                snapshot_json TEXT NOT NULL, cumulative_json TEXT NOT NULL,
                observations_json TEXT NOT NULL, merge_json TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS account_time ON runs(account, collected_at, sequence)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=60)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, source, account=None, collected_at=None, config=None, replicates=1000, seed=42, hypothesis_config=None):
        config = config or Config()
        hypothesis_config = hypothesis_config or HypothesisConfig()
        profiles, metadata, quality = load_profiles(source)
        if metadata.get('error') or metadata.get('stop_reason') in ('partial_error', 'hidden_network'):
            raise ValueError('Failed, partial, or hidden-network collections are not promoted into account history. Collection files remain available.')
        supplied = canonical_account(account) if account else None
        recorded = canonical_account(metadata['target_input']) if metadata.get('target_input') else None
        if supplied and recorded and supplied != recorded:
            raise ValueError('Explicit account conflicts with collection metadata; refusing to mix audiences.')
        account = supplied or recorded
        if not account:
            raise ValueError('Target account missing: supply --account @username@server')
        timestamp = parse_datetime(collected_at or metadata.get('collected_at'))
        if timestamp is None:
            raise ValueError('Collection timestamp missing: supply --collected-at ISO_DATE_TIME')
        timestamp_text = timestamp.isoformat(timespec='microseconds')
        if not profiles and metadata.get('reported_followers_count') != 0:
            raise ValueError('Unexplained empty collection cannot replace account history.')
        for profile in profiles:
            if not profile.profile_key:
                raise ValueError('Every follower needs a stable identity for merging (URI, URL, scoped ID, or handle).')
        records = sorted((pack_profile(p) for p in profiles), key=lambda p: p['profile_key'])
        metadata = dict(metadata, target_input='@' + account, collected_at=timestamp_text, sample_size=len(profiles))
        fingerprint = {'account': account, 'collected_at': timestamp_text, 'records': records,
                       'config': config.to_dict(), 'bootstrap': replicates, 'seed': seed,
                       'model_version': 'hackathon-heuristics-v1', 'analysis_version': 'hypotheses-v1',
                       'hypothesis_config': hypothesis_config.to_dict()}
        run_id = hashlib.sha256(dumps(fingerprint).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT run_id FROM runs WHERE run_id=?', (run_id,)).fetchone()
            if existing:
                result = {'run_id': run_id, 'account': account, 'status': 'already_imported'}
            else:
                previous = db.execute('SELECT * FROM runs WHERE account=? ORDER BY collected_at, sequence', (account,)).fetchall()
                for run in previous:
                    if run['collected_at'] == timestamp_text and json.loads(run['records_json']) != records:
                        raise ValueError('Conflicting records have the same account and collection timestamp. Use the correct distinct collection timestamps.')
                merged, observations = {}, {}
                captures = [(r['collected_at'], json.loads(r['records_json'])) for r in previous]
                captures.append((timestamp_text, records))
                old_keys = {p['profile_key'] for _, entries in captures[:-1] for p in entries}
                for observed_at, entries in sorted(captures, key=lambda item: item[0]):
                    for record in entries:
                        key = record['profile_key']
                        observations.setdefault(key, {'first_observed_at': observed_at})
                        observations[key]['last_observed_at'] = observed_at
                        merged[key] = record
                merged_records = [merged[k] for k in sorted(merged)]
                latest_timestamp = max(t for t, _ in captures)
                latest_metadata = metadata if timestamp_text == latest_timestamp else json.loads(previous[-1]['metadata_json'])
                union_metadata = dict(latest_metadata, sample_size=len(merged_records),
                                      sampling_method='Historical union of all imported samples; latest observation per follower; not random.')
                snapshot = documents(profiles, timestamp, config, metadata, quality, replicates, seed, 'collection_snapshot', hypothesis_config)
                cumulative = documents([unpack_profile(p) for p in merged_records], parse_datetime(latest_timestamp), config,
                                       union_metadata, None, replicates, seed, 'cumulative_observed_followers', hypothesis_config)
                incoming_keys = {p['profile_key'] for p in records}
                merge = {'incoming_unique_rows': len(records), 'previous_unique_rows': len(old_keys),
                         'new_identities': len(incoming_keys - old_keys), 'overlapping_identities': len(incoming_keys & old_keys),
                         'cumulative_unique_rows': len(merged_records), 'latest_collection_at': latest_timestamp,
                         'historical_backfill': timestamp_text < latest_timestamp,
                         'policy': 'Whole newest record wins; no averaging of statistics, no deletion inferred from sample absence.'}
                observation_export = [dict(profile_key=key, **observations[key]) for key in sorted(observations)]
                db.execute('''INSERT INTO runs(run_id,account,collected_at,ingested_at,records_json,metadata_json,
                           snapshot_json,cumulative_json,observations_json,merge_json) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                           (run_id, account, timestamp_text, datetime.now(timezone.utc).isoformat(), dumps(records),
                            dumps(metadata), dumps(snapshot), dumps(cumulative), dumps(observation_export), dumps(merge)))
                result = {'run_id': run_id, 'account': account, 'status': 'imported', **merge}
        # Database is authoritative. An export failure is repairable by rerunning import or compare.
        self.refresh_exports()
        result['run_directory'] = str(self.directory / 'runs' / run_id)
        return result

    def refresh_exports(self):
        with self.connect() as db:
            # Serialize readers that write derived files, preventing stale concurrent exports.
            db.execute('BEGIN IMMEDIATE')
            runs = db.execute('SELECT * FROM runs ORDER BY collected_at, sequence').fetchall()
            account_runs = {}
            for run in runs:
                account_runs.setdefault(run['account'], []).append(run)
                folder = self.directory / 'runs' / run['run_id']
                for kind in ('snapshot', 'cumulative'):
                    bundle = json.loads(run[kind + '_json'])
                    for name, value in bundle.items():
                        atomic_json(folder / kind / (name + '.json'), value)
                    atomic_text(folder / kind / 'report.md', render_report(bundle['statistics']))
                atomic_json(folder / 'merge.json', json.loads(run['merge_json']))
                atomic_json(folder / 'follower_observations.json', json.loads(run['observations_json']))
            comparison, histories = [], {}
            for account, group in sorted(account_runs.items()):
                latest = group[-1]
                latest_union = max(group, key=lambda run: run['sequence'])
                current = json.loads(latest['snapshot_json'])['statistics']
                cumulative = json.loads(latest_union['cumulative_json'])['statistics']
                prior_captures = [r for r in group if r['collected_at'] < latest['collected_at']]
                prior = json.loads(prior_captures[-1]['snapshot_json'])['statistics'] if prior_captures else None
                same_model = prior is not None and self.comparison_signature(prior) == self.comparison_signature(current)
                score = current['scoring']['anomaly_score']
                prior_score = prior['scoring']['anomaly_score'] if prior else None
                entry = {'account': account, 'latest_collected_at': latest['collected_at'],
                         'latest_snapshot_run_id': latest['run_id'], 'latest_cumulative_run_id': latest_union['run_id'],
                         'snapshot_sample_size': current['sample_size'], 'snapshot_anomaly_score': score,
                         'snapshot_severity': current['scoring']['severity'],
                         'cumulative_observed_followers': cumulative['sample_size'],
                         'cumulative_anomaly_score': cumulative['scoring']['anomaly_score'],
                         'score_change_from_previous_snapshot': score - prior_score if same_model and score is not None and prior_score is not None else None,
                         'previous_snapshot_comparable_settings': same_model,
                         'comparison_signature': self.comparison_signature(current),
                         'snapshot_feature_rates': {k: current['features'][k]['value'] for k in
                            ('extreme_ratio_rate', 'low_activity_rate', 'new_account_rate', 'incomplete_profile_rate')},
                         'snapshot_benford_mad': current['benford']['mad']}
                comparison.append(entry)
                histories[account] = [{'run_id': r['run_id'], 'collected_at': r['collected_at'],
                                       'ingested_at': r['ingested_at'], 'merge': json.loads(r['merge_json']),
                                       'snapshot_sample_size': json.loads(r['snapshot_json'])['statistics']['sample_size'],
                                       'snapshot_anomaly_score': json.loads(r['snapshot_json'])['statistics']['scoring']['anomaly_score']}
                                      for r in group]
            same_settings = len({dumps(e['comparison_signature']) for e in comparison}) <= 1
            view = {'accounts': comparison, 'same_scoring_settings_across_accounts': same_settings,
                    'interpretation': 'Accounts remain separate. Snapshot scores and historical-union scores are different views. '
                                      'Changes can reflect sample composition or age as well as account behavior. '
                                      'Matching settings do not establish representative sampling or statistical significance.'}
            atomic_json(self.directory / 'comparison.json', view)
            atomic_json(self.directory / 'account_history.json', histories)
            lines = ['# Account comparison', '', view['interpretation'], '',
                     '| Account | Latest collection | Snapshot rows | Snapshot score | Historical union rows | Historical union score |',
                     '|---|---|---:|---:|---:|---:|']
            display = lambda v: 'unavailable' if v is None else f'{v:.2f}'
            for e in comparison:
                lines.append(f"| @{e['account']} | {e['latest_collected_at']} | {e['snapshot_sample_size']} | {display(e['snapshot_anomaly_score'])} | {e['cumulative_observed_followers']} | {display(e['cumulative_anomaly_score'])} |")
            if not same_settings:
                lines += ['', 'Scoring settings or active components differ between accounts; scores are not directly comparable.']
            atomic_text(self.directory / 'comparison.md', '\n'.join(lines) + '\n')
            return view

    @staticmethod
    def comparison_signature(report):
        return {'model_version': report['model_version'], 'config': report['config'],
                'active_components': [k for k, v in report['scoring']['components'].items() if v['included']]}
