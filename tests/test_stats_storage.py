"""Crash, migration and recovery checks; no real upstream requests."""
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch

from app.config import GatewayConfig, StationConfig
from app.state import Registry, StationState
from app.stats import EventLog


def rows(path):
    with sqlite3.connect(path) as db:
        return [json.loads(row[0]) for row in db.execute('SELECT payload FROM events ORDER BY sequence')]


def test_completed_request_survives_abrupt_exit(tmp_path):
    script = '''
import os, sys
from app.stats import EventLog
log = EventLog(sys.argv[1])
log.add('alpha', 'model-a', True, 10, tokens=42, client_ip='192.0.2.7')
os._exit(0)
'''
    subprocess.run([sys.executable, '-c', script, str(tmp_path)], check=True,
                   cwd=Path(__file__).resolve().parents[1])
    restored = EventLog(tmp_path)
    restored.load()
    assert len(restored.events) == 1
    assert restored.events[0]['client_ip'] == '192.0.2.7'
    assert restored.daily_stats()['total_tokens'] == 42
    assert not (tmp_path / 'stats.json').exists()


def test_legacy_migration_is_once_and_preserves_original(tmp_path):
    legacy = tmp_path / 'stats.json'
    original = json.dumps({'events': [dict(ts=time.time(), station='alpha', model='m', ok=True,
                                          tokens=None, latency_ms=1, detail='', attempts=1)],
                           'daily_totals': {}, 'stats_schema': 2})
    legacy.write_text(original, encoding='utf-8')
    log = EventLog(tmp_path)
    log.load()
    assert legacy.read_text(encoding='utf-8') == original
    log.add('alpha', 'm', True, 1, tokens=9)
    legacy.write_text('invalid JSON that must no longer be consulted', encoding='utf-8')
    restored = EventLog(tmp_path)
    restored.load()
    assert len(restored.events) == 2
    assert restored.events[-1]['tokens'] == 9
    assert restored.storage_view()['ok']


def test_corrupt_files_are_not_overwritten(tmp_path):
    for name in ('stats.json', 'stats.sqlite3'):
        directory = tmp_path / name.replace('.', '-')
        directory.mkdir()
        target = directory / name
        original = b'corrupt-file-do-not-replace'
        target.write_bytes(original)
        log = EventLog(directory)
        log.load()
        log.add('alpha', 'm', True, 1)
        assert not log.persist()
        assert log.storage_view()['read_only']
        assert target.read_bytes() == original
        assert len(log.events) == 1


def test_future_schema_is_protected(tmp_path):
    log = EventLog(tmp_path)
    log.add('alpha', 'm', True, 1)
    with sqlite3.connect(log.store.path) as db:
        db.execute('PRAGMA user_version=999')
    original = log.store.path.read_bytes()
    restored = EventLog(tmp_path)
    restored.load()
    assert restored.storage_view()['read_only']
    restored.add('beta', 'm', True, 1)
    assert not restored.persist()
    assert log.store.path.read_bytes() == original


def test_invalid_json_structure_is_protected_before_loading(tmp_path):
    for i, payload in enumerate(({'daily': {'2026-01-01': []}},
                                 {'stats_schema': 999},
                                 {'events': [{'ts': 'invalid'}]},
                                 {'pending': {'one': None}},
                                 {'daily': {'2026-01-01': {'alpha': {'calls': -10}}}})):
        directory = tmp_path / str(i)
        directory.mkdir()
        path = directory / 'stats.json'
        original = json.dumps(payload)
        path.write_text(original, encoding='utf-8')
        log = EventLog(directory)
        log.load()
        assert log.storage_view()['read_only']
        assert not log.persist()
        assert path.read_text(encoding='utf-8') == original
        assert not log.store.path.exists()


def test_write_error_retries_entire_uncommitted_history(tmp_path):
    log = EventLog(tmp_path)
    log.load()
    with patch.object(log.store, 'write', side_effect=sqlite3.OperationalError('disk unavailable')):
        log.add('alpha', 'm', True, 1, tokens=8)
    assert not log.storage_view()['ok']
    log.add('beta', 'm', False, 1)
    log._retry_at = 0
    log.maintenance()
    assert log.storage_view()['ok']
    assert len(rows(log.store.path)) == 2
    restored = EventLog(tmp_path)
    restored.load()
    assert restored.daily_stats()['total_calls'] == 2
    assert restored.daily_stats()['total_tokens'] == 8


def test_locked_database_is_retryable(tmp_path):
    log = EventLog(tmp_path)
    log.load()
    with sqlite3.connect(log.store.path) as db:
        db.execute('BEGIN IMMEDIATE')
        log.add('alpha', 'm', True, 1)
        assert not log.storage_view()['ok']
        assert not log.storage_view()['read_only']
    log._retry_at = 0
    log.maintenance()
    assert log.storage_view()['ok']
    assert len(rows(log.store.path)) == 1


def test_stale_writer_cannot_erase_newer_history(tmp_path):
    first = EventLog(tmp_path)
    first.load()
    second = EventLog(tmp_path)
    second.load()
    second.add('newer', 'm', True, 1)
    first.add('stale', 'm', True, 1)
    assert first.storage_view()['read_only']
    assert [row['station'] for row in rows(first.store.path)] == ['newer']


def test_retention_and_ip_analytics_survive_restart(tmp_path):
    log = EventLog(tmp_path, maxlen=3)
    for i in range(7):
        log.add('alpha', f'm-{i}', True, 1, tokens=i, client_ip='192.0.2.1')
    restored = EventLog(tmp_path, maxlen=3)
    restored.load()
    assert [row['model'] for row in restored.events] == ['m-4', 'm-5', 'm-6']
    assert restored.log_analytics(ip='192.0.2.1')['calls'] == 3
    assert restored.log_analytics()['tokens'] == 15
    assert restored.daily_stats()['total_calls'] == 7
    assert restored.daily_stats()['total_tokens'] == 21


def test_accounts_store_separate_history(tmp_path):
    for account, ip in (('one', '192.0.2.1'), ('two', '192.0.2.2')):
        EventLog(tmp_path / account).add(account, 'm', True, 1, client_ip=ip)
    first = EventLog(tmp_path / 'one')
    second = EventLog(tmp_path / 'two')
    first.load()
    second.load()
    assert first.log_analytics(ip='192.0.2.2')['calls'] == 0
    assert second.log_analytics(ip='192.0.2.1')['calls'] == 0


def test_stream_interrupted_recovery_is_once_and_usage_unknown(tmp_path):
    log = EventLog(tmp_path)
    log.start_route(1, 'alpha', 'm', '192.0.2.3')
    log.add('alpha', 'm', True, 1, route_id=1, client_ip='192.0.2.3')
    log.start_route(2, 'alpha', 'm', '192.0.2.4')
    restored = EventLog(tmp_path)
    restored.load()
    assert len(restored.events) == 2
    assert all(row['outcome'] == 'interrupted' and row['tokens'] is None for row in restored.events)
    assert restored.daily_stats()['total_failures'] == 2
    again = EventLog(tmp_path)
    again.load()
    assert len(again.events) == 2
    assert again.daily_stats()['total_calls'] == 2
    assert again.daily_stats()['total_failures'] == 2


def test_finished_stream_usage_is_idempotent_and_no_pending(tmp_path):
    log = EventLog(tmp_path)
    log.start_route(1, 'alpha', 'm', '192.0.2.3')
    started = log.add('alpha', 'm', True, 1, route_id=1, client_ip='192.0.2.3')
    log.add_tokens('alpha', 'm', 12, occurred_at=started, route_id=1)
    log.add_tokens('alpha', 'm', 12, occurred_at=started, route_id=1)
    log.finish_route(1, 'success')
    restored = EventLog(tmp_path)
    restored.load()
    assert restored.events[0]['outcome'] == 'success'
    assert restored.daily_stats()['total_tokens'] == 12
    assert not restored._pending


def test_evicted_stream_is_not_recreated_or_counted_twice(tmp_path):
    log = EventLog(tmp_path, maxlen=1)
    log.start_route(1, 'alpha', 'm')
    started = log.add('alpha', 'm', True, 1, route_id=1)
    log.add('beta', 'm', True, 1)
    log.add_tokens('alpha', 'm', 7, occurred_at=started, route_id=1)
    log.add_tokens('alpha', 'm', 7, occurred_at=started, route_id=1)
    restored = EventLog(tmp_path, maxlen=1)
    restored.load()
    assert len(restored.events) == 1
    assert restored.events[0]['station'] == 'beta'
    assert restored.daily_stats()['total_tokens'] == 7


def test_health_restored_without_active_routes_or_polling_writes(tmp_path):
    cfg = StationConfig(name='alpha', base_url='https://example.invalid', api_key='test-key', models=['m'])
    log = EventLog(tmp_path)
    log.load()
    registry = Registry(GatewayConfig(), [cfg])
    registry.bind_statistics(log)
    st = registry.get('alpha')
    st.record_success(1.5)
    st.mark_rate_limited(300, reason='429', wait_source='upstream')
    before = log.store.revision
    for _ in range(10):
        registry.snapshot()
    assert log.store.revision == before
    restored = EventLog(tmp_path)
    restored.load()
    new = Registry(GatewayConfig(), [cfg])
    new.bind_statistics(restored)
    current = new.get('alpha')
    assert current.calls == 1
    assert current.rate_limit_until == st.rate_limit_until
    assert current.ema_latency == 1.5
    assert new.route_activity_snapshot()['inflight'] == 0
    different = StationState(cfg=dataclasses.replace(cfg, api_key='changed'))
    different.restore_durable(restored._runtime['alpha'])
    assert different.rate_limit_until == 0


def test_backups_are_bounded_and_readable(tmp_path):
    log = EventLog(tmp_path)
    log.add('alpha', 'm', True, 1)
    directory = tmp_path / 'backups'
    for day in ('2001-01-01', '2001-01-02', '2001-01-03', '2001-01-04'):
        log.store.backup(directory / f'stats-{day}.sqlite3')
    log._maintenance_day = ''
    log.maintenance()
    backups = list(directory.glob('stats-*.sqlite3'))
    assert len(backups) == 3
    for backup in backups:
        with sqlite3.connect(backup) as db:
            assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert not list(directory.glob('.stats-backup-*'))


def test_missing_live_database_is_not_silently_recreated(tmp_path):
    log = EventLog(tmp_path)
    log.load()
    log.store.path.rename(tmp_path / 'saved.sqlite3')
    log.add('alpha', 'm', True, 1)
    assert not log.storage_view()['ok']
    assert not log.store.path.exists()
