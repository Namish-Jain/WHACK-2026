"""Descriptive overlap of latest sampled audiences; no coordination classifier."""
import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import fields
from itertools import combinations
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from data_io import parse_datetime
from models import FollowerProfile
from .config import Config
from .features import profile_features, rate


def load_latest_snapshots(store, accounts=None):
    database = Path(store).expanduser().resolve() / 'history.sqlite3'
    if not database.is_file():
        raise ValueError('No history.sqlite3 found. Import or collect at least two accounts first.')
    db = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        runs = db.execute('''SELECT r.account,r.run_id,r.collected_at,r.records_json,r.metadata_json
            FROM runs r WHERE NOT EXISTS (
                SELECT 1 FROM runs newer WHERE newer.account=r.account AND
                (newer.collected_at>r.collected_at OR
                 (newer.collected_at=r.collected_at AND newer.sequence>r.sequence)))
            ORDER BY r.account''').fetchall()
    finally:
        db.close()
    available = {r['account'] for r in runs}
    if accounts is not None and set(accounts) - available:
        raise ValueError('Accounts absent from history: ' + ', '.join(sorted(set(accounts) - available)))
    return [{'account': r['account'], 'run_id': r['run_id'], 'collected_at': r['collected_at'],
             'profiles': json.loads(r['records_json']), 'metadata': json.loads(r['metadata_json'])}
            for r in runs if accounts is None or r['account'] in accounts]


def qualified_handle(value):
    if not isinstance(value, str):
        return None
    value = value.strip().lstrip('@')
    if value.count('@') != 1 or any(c.isspace() for c in value):
        return None
    username, host = value.split('@')
    return value.lower() if username and '.' in host else None


def strong_identity(value):
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme in ('https', 'http') and parsed.hostname and '|id:' not in value:
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, parsed.fragment))
    return None


def node_id(kind, value):
    return kind + ':' + hashlib.sha256(value.encode()).hexdigest()


def as_profile(record, key):
    allowed = {field.name for field in fields(FollowerProfile)}
    record = {k: v for k, v in record.items() if k in allowed}
    record.update(profile_key=key, username=record.get('username') or key)
    for field in ('created_at', 'last_status_at'):
        record[field] = parse_datetime(record.get(field))
    return FollowerProfile(**record)


def combined_flag(a, b):
    # Complete-case joint rate; unknown is not interpreted as false.
    return a and b if a is not None and b is not None else None


def analyze_network(snapshots, config=None, min_shared=2, min_jaccard=0.01):
    config = config or Config()
    if type(min_shared) is not int or min_shared < 1 or not math.isfinite(min_jaccard) or not 0 <= min_jaccard <= 1:
        raise ValueError('Use positive min_shared and a Jaccard threshold in [0, 1]')
    if len({s['account'] for s in snapshots}) != len(snapshots):
        raise ValueError('Supply exactly one latest snapshot per target account')
    snapshots = sorted(snapshots, key=lambda s: s['account'])
    timestamps = [parse_datetime(s['collected_at']) for s in snapshots]
    if any(t is None for t in timestamps):
        raise ValueError('Snapshot collection timestamps are required')
    as_of = max(timestamps) if timestamps else None
    handles_to_strong = defaultdict(set)
    for s in snapshots:
        for record in s['profiles']:
            strong, handle = strong_identity(record.get('profile_key')), qualified_handle(record.get('username'))
            if strong and handle:
                handles_to_strong[handle].add(strong)
    ambiguous = {h for h, identities in handles_to_strong.items() if len(identities) > 1}
    audiences, observations, members = {}, defaultdict(list), defaultdict(set)
    skipped, identity_modes, targets, duplicate_rows = 0, defaultdict(int), [], 0
    for snapshot, timestamp in zip(snapshots, timestamps):
        keys = set()
        for record in snapshot['profiles']:
            strong = strong_identity(record.get('profile_key'))
            handle = qualified_handle(record.get('username'))
            fallback = record.get('profile_key')
            if strong:
                key, mode = 'actor:' + strong, 'actor_or_profile_url'
            elif handle and len(handles_to_strong[handle]) == 1:
                key, mode = 'actor:' + next(iter(handles_to_strong[handle])), 'unique_handle_to_url_resolution'
            elif handle and handle not in ambiguous:
                key, mode = 'handle:' + handle, 'qualified_handle'
            elif isinstance(fallback, str) and '|id:' in fallback and fallback.split('|id:')[0]:
                key, mode = 'scoped:' + fallback, 'instance_scoped_id'
            else:
                skipped += 1
                continue
            identity_modes[mode] += 1
            if key in keys:
                duplicate_rows += 1
            keys.add(key)
            members[key].add(snapshot['account'])
            observations[key].append((timestamp, record))
        audiences[snapshot['account']] = keys
        reported = snapshot['metadata'].get('reported_followers_count')
        targets.append({'id': node_id('target', snapshot['account']), 'kind': 'target', 'account': snapshot['account'],
                        'run_id': snapshot['run_id'], 'collected_at': snapshot['collected_at'],
                        'input_rows': len(snapshot['profiles']), 'sampled_unique_followers': len(keys),
                        'reported_followers_count': reported,
                        'sample_fraction_of_reported_count': len(keys) / reported if type(reported) is int and reported > 0 and len(keys) <= reported else None})
    followers, attributes, conflicts = [], {}, 0
    for key in sorted(members):
        latest_time = max(t for t, _ in observations[key])
        latest = [r for t, r in observations[key] if t == latest_time]
        agreed = dict(latest[0])
        conflict_fields = []
        for field in ('followers_count', 'following_count', 'posts_count', 'created_at', 'is_bot', 'has_profile_picture', 'bio_length', 'display_name', 'last_status_at'):
            if any(r.get(field) != latest[0].get(field) for r in latest):
                agreed[field] = None
                conflict_fields.append(field)
        conflicts += bool(conflict_fields)
        profile = as_profile(agreed, key)
        attributes[key] = profile_features(profile, as_of, config)
        followers.append({'id': node_id('follower', key), 'kind': 'follower', 'identity': key,
                          'username': profile.username, 'target_count': len(members[key]),
                          'targets': sorted(members[key]), 'last_observed_at': latest_time.isoformat(),
                          'conflicting_fields_at_latest_timestamp': conflict_fields,
                          'new_account': attributes[key]['new_account'], 'low_activity': attributes[key]['low_activity'],
                          'new_and_low_activity': combined_flag(attributes[key]['new_account'], attributes[key]['low_activity'])})
    pairs, adjacency = [], {account: set() for account in audiences}
    for left, right in combinations(sorted(audiences), 2):
        a, b = audiences[left], audiences[right]
        shared, union = a & b, a | b
        jaccard = len(shared) / len(union) if union else None
        qualifies = len(shared) >= min_shared and jaccard is not None and jaccard >= min_jaccard
        if qualifies:
            adjacency[left].add(right)
            adjacency[right].add(left)
        measurements = {}
        for name in ('new_account', 'low_activity', 'new_and_low_activity'):
            values = [combined_flag(attributes[k]['new_account'], attributes[k]['low_activity'])
                      if name == 'new_and_low_activity' else attributes[k][name] for k in sorted(shared)]
            measurements[name + '_rate'] = rate(values)
        pairs.append({'left': left, 'right': right, 'left_sample_size': len(a), 'right_sample_size': len(b),
                      'shared_count': len(shared), 'union_count': len(union), 'jaccard': jaccard,
                      'overlap_coefficient': len(shared) / min(len(a), len(b)) if a and b else None,
                      'fraction_of_left_sample_shared': len(shared) / len(a) if a else None,
                      'fraction_of_right_sample_shared': len(shared) / len(b) if b else None,
                      'passes_display_thresholds': qualifies, 'shared_profile_metrics': measurements,
                      'shared_follower_node_ids': [node_id('follower', key) for key in sorted(shared)]})
    components, visited = [], set()
    for account in sorted(adjacency):
        if account in visited:
            continue
        stack, component = [account], []
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            stack.extend(sorted(adjacency[current] - visited, reverse=True))
        if len(component) > 1:
            components.append(sorted(component))
    edges = [{'source': node_id('target', account), 'target': node_id('follower', key), 'relationship': 'observed_follower'}
             for account in sorted(audiences) for key in sorted(audiences[account])]
    report = {
        'schema_version': '1.0', 'dataset_kind': 'latest_snapshot_audience_network',
        'status': 'computed' if len(snapshots) >= 2 else 'insufficient_accounts',
        'as_of': as_of.isoformat() if as_of else None,
        'snapshot_span_days': (max(timestamps) - min(timestamps)).total_seconds() / 86400 if timestamps else None,
        'config': config.to_dict(), 'display_thresholds': {'min_shared': min_shared, 'min_jaccard': min_jaccard},
        'target_count': len(targets), 'unique_follower_count': len(followers),
        'shared_follower_count': sum(f['target_count'] >= 2 for f in followers),
        'observed_edge_count': len(edges), 'targets': targets, 'pairs': pairs,
        'connected_target_groups': components,
        'shared_followers': sorted([f for f in followers if f['target_count'] >= 2], key=lambda f: (-f['target_count'], f['identity'])),
        'identity_quality': {'skipped_unresolvable_records': skipped, 'duplicate_memberships_removed': duplicate_rows,
                             'identity_mode_counts': dict(identity_modes), 'ambiguous_handle_count': len(ambiguous),
                             'followers_with_conflicting_latest_fields': conflicts},
        'limitations': [
            'This graph describes overlap of observed samples, not full follower networks. Zero observed overlap does not imply disjoint audiences.',
            'Shared interests can produce shared audiences. No coordination, bot classification, p-value, or manipulation probability is inferred.',
            'Only each target account\'s latest dated snapshot is used; cumulative historical unions are excluded.',
            'Snapshots may be collected at different times. Follower characteristics use the newest selected observation, with age measured at the latest selected collection time.',
            'Conflicting same-time profile fields become unknown. Ambiguous handles do not collapse distinct actor URLs; unresolved identities are excluded and counted.',
            'Connected groups depend on display thresholds. They are not validated communities or evidence of coordination.',
            'Account creation dates describe account age, not follow time. This analysis does not change existing anomaly scores.',
        ],
    }
    graph = {'directed': True, 'graph_type': 'bipartite_target_to_observed_follower',
             'nodes': targets + followers, 'edges': edges}
    return report, graph
