"""Read normalized JSON/CSV through the same strict, platform-neutral parser."""
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from models import FollowerProfile


def missing(value):
    return value is None or isinstance(value, str) and value.strip().lower() in ('', 'null', 'none', 'nan')


def parse_count(value):
    if missing(value):
        return None
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'\d+', value.strip(), flags=re.ASCII):
        return int(value)
    raise ValueError('Expected a nonnegative integer')


def parse_bool(value):
    if missing(value):
        return None
    if type(value) is bool:
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    raise ValueError('Expected true or false')


def parse_datetime(value):
    if missing(value):
        return None
    dt = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    # Mastodon last_status_at is date-only. Naive timestamps are interpreted as UTC.
    return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)


def load_profiles(source):
    source = Path(source).expanduser().resolve()
    if source.is_dir():
        source = source / ('followers_normalized.json' if (source / 'followers_normalized.json').exists() else 'demo_followers.csv')
    blob = source.read_bytes()
    if source.suffix.lower() == '.json':
        records = json.loads(blob)
    elif source.suffix.lower() == '.csv':
        with source.open(newline='', encoding='utf-8-sig') as stream:
            records = list(csv.DictReader(stream))
    else:
        raise ValueError('Input must be normalized .json, .csv, or a collector directory')
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError('Expected an array of profile objects')
    if any(not any(k in row for k in ('followers_count', 'following_count', 'posts_count', 'statuses_count', 'created_at', 'bio_length', 'has_profile_picture')) for row in records):
        raise ValueError('Record has no recognized analysis fields')
    metadata_path = source.parent / 'collection_metadata.json'
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    if not isinstance(metadata, dict):
        raise ValueError('collection_metadata.json must be an object')
    invalid, missing_counts, seen = Counter(), Counter(), set()
    profiles, duplicate_count = [], 0
    for index, record in enumerate(records):
        identity = record.get('uri') or record.get('url')
        if not identity and record.get('id') is not None:
            identity = f"{record.get('source_instance') or metadata.get('instance', '')}|id:{record['id']}"
        if not identity:
            identity = record.get('username') or record.get('acct')
        if identity is not None:
            if not isinstance(identity, str):
                raise ValueError(f'Invalid identity on row {index + 1}')
            if identity in seen:
                duplicate_count += 1
                continue
            seen.add(identity)
        def parse(name, parser, alias=None):
            value = record.get(name)
            if missing(value) and alias:
                value = record.get(alias)
            try:
                result = parser(value)
            except (ValueError, TypeError, OverflowError):
                invalid[name] += 1
                result = None
            if result is None:
                missing_counts[name] += 1
            return result
        profiles.append(FollowerProfile(
            profile_key=identity,
            username=str(record.get('username') or record.get('acct') or f'row_{index + 1}'),
            followers_count=parse('followers_count', parse_count),
            following_count=parse('following_count', parse_count),
            posts_count=parse('posts_count', parse_count, 'statuses_count'),
            created_at=parse('created_at', parse_datetime),
            last_status_at=parse('last_status_at', parse_datetime),
            is_bot=parse('is_bot', parse_bool, 'bot'),
            has_profile_picture=parse('has_profile_picture', parse_bool),
            bio_length=parse('bio_length', parse_count),
            display_name=record.get('display_name') if isinstance(record.get('display_name'), str) else None,
        ))
    quality = {'input_rows': len(records), 'unique_rows': len(profiles), 'duplicates_removed': duplicate_count,
               'invalid_values_by_field': dict(invalid), 'missing_values_by_field': dict(missing_counts),
               'source_path': str(source), 'source_sha256': hashlib.sha256(blob).hexdigest()}
    return profiles, metadata, quality


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
