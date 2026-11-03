"""调用统计与事件日志。

- 内存环形缓冲记录最近调用/切换事件
- SQLite 事务保存；旧 JSON 只读迁移，重启恢复历史
- 今日按站聚合统计（占比/时间线，供仪表盘"今日统计"tab）
"""
from __future__ import annotations

import json
import ipaddress
import time
import uuid
import sqlite3
import logging
import math
from collections import deque
from datetime import date, datetime, timedelta
from pathlib import Path
from .stats_store import StatsStore, StaleStatsWriter

EVENT_MAXLEN = 10_000
EVENT_PAGE_MAXLEN = 500
EVENT_DEFAULT_MAXLEN = 500
EVENT_DETAIL_MAXLEN = 2048
EVENT_NAME_MAXLEN = 256
STATS_RETENTION_DAYS = 365
MODEL_BUCKET_MAXLEN = 128
MODEL_CANDIDATE_MAXLEN = 256
OTHER_MODEL_KEY = "__zg_other_models__"
STATS_SCHEMA_VERSION = 2
DAY_STORES = ('daily', 'daily_models', 'daily_totals', 'daily_hours', 'hourly_cache')
logger = logging.getLogger(__name__)


class EventLog:
    def __init__(self, data_dir: str, maxlen: int = EVENT_DEFAULT_MAXLEN,
                 retention_days: int = STATS_RETENTION_DAYS, *, durable: bool = True):
        self.path = Path(data_dir) / "stats.json"
        self.store = StatsStore(data_dir)
        self.durable = durable
        self._loaded = False
        self._read_only = False
        self._storage_error = ''
        self._last_saved = 0.0
        self._retry_at = 0.0
        self._runtime = {}
        self._pending = {}
        self._reported_usage = {}
        self._boot_id = uuid.uuid4().hex
        self._maintenance_day = ''
        self.retention_days = retention_days
        # Keep the request ring bounded while allowing operators to choose a
        # useful retention window for both browsing and log analytics.
        self.maxlen = max(1, min(int(maxlen), EVENT_MAXLEN))
        self.events: deque[dict] = deque(maxlen=self.maxlen)
        self.station_counters: dict[str, dict] = {}
        # 今日聚合: {date: {station: {"calls": n, "failures": n, "ms": n, "tokens": n}}}
        self.daily: dict[str, dict[str, dict]] = {}
        # 今日按站×模型用量。旧版持久化值是整数 token，读取后按需兼容迁移。
        # {date: {station: {model: {calls, failures, tokens, usage_reports}}}}
        self.daily_models: dict[str, dict[str, dict[str, dict | int]]] = {}
        # 请求级总数单独保存，确保全部上游失败（station="-"）仍计入成功率。
        self.daily_totals: dict[str, dict[str, int]] = {}
        # 小时聚合独立于 500 条事件环，避免高流量时从事件回放造成低估。
        self.daily_hours: dict[str, dict[str, list[int]]] = {}
        # 按小时的缓存命中：{date: {station: [{cache_read, cache_input, cache_reports} × 24]}}
        self.hourly_cache: dict[str, dict[str, list[dict]]] = {}

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.durable:
            return
        try:
            data = self.store.read()
            imported = data is None
            if imported:
                data = (json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else
                        dict(self._summary(), **{name: getattr(self, name) for name in DAY_STORES}, events=list(self.events)))
            if not isinstance(data, dict):
                raise ValueError('Statistics root must be an object')
            stored_events = data.get("events", [])
            if not isinstance(stored_events, list) or any(not isinstance(e, dict) for e in stored_events):
                raise ValueError('Invalid request history')
            for name in ('stations', *DAY_STORES, 'runtime', 'pending'):
                if not isinstance(data.get(name, {}), dict):
                    raise ValueError('Invalid statistics section: ' + name)
            self._validate_data(data)
            # Persisted order is oldest -> newest.  Keep the newest records;
            # the previous incremental load stopped at maxlen and therefore
            # retained the oldest records from an oversized legacy file.
            self.events.clear()
            self.events.extend(
                self._bounded_event(event)
                for event in stored_events[-self.maxlen:]
                if isinstance(event, dict)
            )
            for event in self.events:
                event.setdefault('event_id', uuid.uuid4().hex)
            self.station_counters = data.get("stations", {})
            self.daily = data.get("daily", {})
            self.daily_models = data.get("daily_models", {})
            self.daily_totals = data.get("daily_totals", {})
            self.daily_hours = data.get("daily_hours", {})
            self.hourly_cache = data.get("hourly_cache", {})
            for name in (
                "station_counters", "daily", "daily_models",
                "daily_totals", "daily_hours", "hourly_cache",
            ):
                if not isinstance(getattr(self, name), dict):
                    setattr(self, name, {})
            try:
                schema_version = int(data.get("stats_schema", 0) or 0)
            except (TypeError, ValueError):
                schema_version = 0
            if schema_version < STATS_SCHEMA_VERSION:
                self._rebuild_legacy_totals()
            self._prune_history()
            self._prune_daily(retention_days=self.retention_days)
            self._runtime = data.get('runtime', {})
            self._pending = data.get('pending', {})
            self._last_saved = data.get('saved_at', 0.0)
            if imported:
                self.store.initialise(self._summary(), self._days(), list(self.events), self.maxlen)
                self._last_saved = time.time()
            # Startup reconciliation is one transaction; a crash cannot duplicate recovery rows.
            for pending in list(self._pending.values()):
                event = next((e for e in self.events if e.get('event_id') == pending['event_id']), None)
                if event:
                    self._mark_interrupted(event, '服务重启中断，最终用量可能未上报')
                elif not pending.get('logged'):
                    old_durable, self.durable = self.durable, False
                    try:
                        self.add(pending['station'], pending['model'], False, 0,
                                 '服务重启中断，最终用量未上报', client_ip=pending.get('client_ip'),
                                 occurred_at=pending['started_at'], outcome='interrupted',
                                 event_id=pending['event_id'], requested_model=pending.get('requested_model'),
                                 mapping_id=pending.get('mapping_id'))
                    finally:
                        self.durable = old_durable
            if self._pending:
                self._pending.clear()
                self.persist()
            self.maintenance()
        except (ValueError, TypeError, KeyError, OverflowError, OSError, sqlite3.Error, StaleStatsWriter) as exc:
            self._read_only = True
            self._storage_error = '历史统计读取失败，已保护原文件；新记录暂存内存。请检查磁盘或从备份恢复后重启。'
            logger.error('Statistics loading failed; original files protected: %s', type(exc).__name__)

    def _summary(self):
        return {'stats_schema': STATS_SCHEMA_VERSION, 'stations': self.station_counters}

    @staticmethod
    def _validate_data(data):
        version = data.get('stats_schema', 0)
        if int(version or 0) > STATS_SCHEMA_VERSION:
            raise ValueError('Unsupported statistics content version')
        numeric = {'calls', 'failures', 'ms', 'tokens', 'usage_reports', 'input_tokens',
                   'cache_read', 'cache_write', 'cache_reports', 'cache_input', 'ttft_sum', 'ttft_n'}

        def bucket(value):
            if not isinstance(value, dict):
                raise ValueError('Invalid statistics bucket')
            for key in numeric.intersection(value):
                number = value[key]
                if not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                    raise ValueError('Invalid statistics counter')

        for name in DAY_STORES:
            table = data.get(name, {})
            if not isinstance(table, dict):
                raise ValueError('Invalid statistics section')
            for day, values in table.items():
                datetime.strptime(day, '%Y-%m-%d')
                if not isinstance(values, dict):
                    raise ValueError('Invalid daily statistics')
                if name == 'daily_totals':
                    bucket(values)
                elif name in ('daily', 'daily_models'):
                    for value in values.values():
                        if name == 'daily':
                            bucket(value)
                        else:
                            if not isinstance(value, dict):
                                raise ValueError('Invalid model statistics')
                            for model in value.values():
                                if isinstance(model, dict):
                                    bucket(model)
                                elif not isinstance(model, (int, float)) or not math.isfinite(model) or model < 0:
                                    raise ValueError('Invalid legacy model usage')
        for event in data.get('events', []):
            if not isinstance(event, dict):
                raise ValueError('Invalid request history')
            timestamp = event.get('ts')
            if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                raise ValueError('Invalid request time')
            datetime.fromtimestamp(timestamp)
        for pending in data.get('pending', {}).values():
            if not isinstance(pending, dict) or not all(isinstance(pending.get(k), str) for k in ('event_id', 'station', 'model')):
                raise ValueError('Invalid unfinished request')
            datetime.fromtimestamp(pending['started_at'])

    def _days(self, only=None):
        dates = {only} if only is not None else {day for name in DAY_STORES for day in getattr(self, name)}
        return {day: {name: getattr(self, name)[day] for name in DAY_STORES if day in getattr(self, name)} for day in dates}

    def storage_view(self):
        return {'backend': 'sqlite', 'ok': not bool(self._storage_error), 'read_only': self._read_only,
                'last_saved_at': self._last_saved, 'message': self._storage_error or '本地 SQLite · 请求完成即保存 · 重启保留',
                'scope': 'account', 'legacy_retained': self.path.exists()}

    def _commit(self, **changes):
        if not self.durable or self._read_only:
            return False
        if self._storage_error:
            if time.time() < self._retry_at:
                return False
            return self.persist()
        try:
            self._last_saved = self.store.write(maxlen=self.maxlen, **changes)
            return True
        except (OSError, ValueError, sqlite3.Error, StaleStatsWriter) as exc:
            if isinstance(exc, StaleStatsWriter):
                self._read_only = True
            self._storage_error = ('检测到另一网关进程写入统计，已停止覆盖；请只保留一个服务进程。'
                                   if self._read_only else '统计保存失败，新记录暂存内存；正在重试，请检查磁盘空间和目录权限。')
            self._retry_at = time.time() + 5
            logger.error('Statistics commit failed: %s', type(exc).__name__)
            return False

    def maintenance(self):
        """Bounded daily backup, not a full file rewrite every polling interval."""
        if not self.durable or self._read_only:
            return
        if self._storage_error and not self.persist():
            return
        day = datetime.now().strftime('%Y-%m-%d')
        if day == self._maintenance_day:
            return
        self._prune_daily(self.retention_days)
        if not self.persist():
            return
        target = self.store.path.parent / 'backups' / f'stats-{day}.sqlite3'
        try:
            if not target.exists():
                self.store.backup(target)
            backups = sorted(target.parent.glob('stats-????-??-??.sqlite3'))
            for old in backups[:-3]:
                old.unlink()
            self._maintenance_day = day
        except (OSError, sqlite3.Error) as exc:
            logger.error('Statistics backup failed: %s', type(exc).__name__)

    def save_runtime(self, name, state):
        self.load()
        previous = self._runtime.get(name, {})
        # Dashboard polling only recomputes scores; it must not write to disk
        # or renew the age of historical performance evidence.
        if any(previous.get(key) != state.get(key) for key in (
                'signature', 'values', 'window_results', 'score_version', 'health_samples', 'legacy_score_samples')):
            self._runtime[name] = state
            self._commit(runtime={name: state})

    def start_route(self, route_id, station, model, client_ip=None, **mapping):
        self.load()
        key = f'{self._boot_id}:{route_id}'
        pending = {'event_id': uuid.uuid4().hex, 'station': station, 'model': model,
                   'client_ip': client_ip, 'started_at': time.time(), 'logged': False, **mapping}
        self._pending[key] = pending
        self._commit(pending={key: pending})

    def _mark_interrupted(self, event, detail):
        if event.get('ok'):
            day = datetime.fromtimestamp(event['ts']).strftime('%Y-%m-%d')
            for bucket in (self.daily_totals.get(day), self.daily.get(day, {}).get(event['station']),
                           self.daily_models.get(day, {}).get(event['station'], {}).get(event['model'])):
                if isinstance(bucket, dict):
                    bucket['failures'] = int(bucket.get('failures', 0)) + 1
        event.update(ok=False, outcome='interrupted', detail=detail)

    def finish_route(self, route_id, outcome, error=''):
        key = f'{self._boot_id}:{route_id}'
        pending = self._pending.pop(key, None)
        if not pending:
            return
        event = next((e for e in self.events if e.get('event_id') == pending['event_id']), None)
        if event:
            if outcome not in ('ok', 'success'):
                self._mark_interrupted(event, '请求中断：' + (error or outcome))
            else:
                event['outcome'] = 'success'
            day = datetime.fromtimestamp(event['ts']).strftime('%Y-%m-%d')
            self._commit(events=[event], days=self._days(day), remove_pending=[key])
        else:
            self._commit(remove_pending=[key])

    @staticmethod
    def _safe_text(value: object, limit: int) -> str:
        return str(value or "")[:limit]

    @classmethod
    def _bounded_event(cls, event: dict) -> dict:
        result = dict(event)
        result["station"] = cls._safe_text(result.get("station"), EVENT_NAME_MAXLEN) or "-"
        result["model"] = cls._safe_text(result.get("model"), EVENT_NAME_MAXLEN) or "-"
        for key in ('requested_model', 'mapping_id'):
            if key in result:
                result[key] = cls._safe_text(result[key], EVENT_NAME_MAXLEN)
        result["detail"] = cls._safe_text(result.get("detail"), EVENT_DETAIL_MAXLEN)
        client_ip = result.get("client_ip")
        try:
            result["client_ip"] = str(ipaddress.ip_address(client_ip)) if client_ip else None
        except ValueError:
            result["client_ip"] = None
        return result

    def set_limit(self, maxlen: int) -> None:
        self.load()
        self.maxlen = max(1, min(int(maxlen), EVENT_MAXLEN))
        self.events = deque(self.events, maxlen=self.maxlen)

    @classmethod
    def _cap_model_map(cls, models: dict) -> None:
        """Keep the highest-token models and merge overflow into one bucket."""
        if not isinstance(models, dict):
            return
        named = [item for item in models.items() if item[0] != OTHER_MODEL_KEY]
        named.sort(key=lambda item: cls._model_tokens(item[1]), reverse=True)
        if len(named) <= MODEL_BUCKET_MAXLEN - 1:
            return
        kept = dict(named[:MODEL_BUCKET_MAXLEN - 1])
        overflow = named[MODEL_BUCKET_MAXLEN - 1:]
        if OTHER_MODEL_KEY in models:
            overflow.append((OTHER_MODEL_KEY, models[OTHER_MODEL_KEY]))
        merged = {
            "calls": 0, "failures": 0, "tokens": 0, "usage_reports": 0,
            "input_tokens": 0, "cache_read": 0, "cache_write": 0,
            "cache_reports": 0, "cache_input": 0,
        }
        for _, value in overflow:
            if isinstance(value, dict):
                for field in merged:
                    try:
                        merged[field] += max(0, int(value.get(field, 0) or 0))
                    except (TypeError, ValueError):
                        pass
            else:
                merged["tokens"] += cls._model_tokens(value)
        kept[OTHER_MODEL_KEY] = merged
        models.clear()
        models.update(kept)

    @classmethod
    def _model_bucket(cls, models: dict, model: str) -> dict:
        """Return a mutable model bucket while accepting the legacy token-only shape."""
        model = cls._safe_text(model, EVENT_NAME_MAXLEN) or "-"
        if model not in models and len(models) >= MODEL_CANDIDATE_MAXLEN:
            cls._cap_model_map(models)
        current = models.get(model)
        if isinstance(current, dict):
            for field in (
                "calls", "failures", "tokens", "usage_reports",
                "input_tokens", "cache_read", "cache_write",
                "cache_reports", "cache_input",
            ):
                current.setdefault(field, 0)
            return current
        try:
            legacy_tokens = max(0, int(current or 0))
        except (TypeError, ValueError):
            legacy_tokens = 0
        bucket = {
            "calls": 0,
            "failures": 0,
            "tokens": legacy_tokens,
            "usage_reports": 0,
            "input_tokens": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cache_reports": 0,
            "cache_input": 0,
        }
        models[model] = bucket
        return bucket

    @staticmethod
    def _optional_count(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _hit_rate(cache_read: int, cache_input: int) -> float | None:
        if cache_input <= 0:
            return None
        return round(min(1.0, max(0.0, cache_read / cache_input)), 4)

    @staticmethod
    def _empty_cache_hour() -> dict:
        return {"cache_read": 0, "cache_input": 0, "cache_reports": 0}

    @classmethod
    def _normalise_cache_hours(cls, value: object) -> list[dict]:
        source = value if isinstance(value, list) else []
        result = []
        for item in source[:24]:
            if not isinstance(item, dict):
                result.append(cls._empty_cache_hour())
                continue
            hour = cls._empty_cache_hour()
            for field in hour:
                try:
                    hour[field] = max(0, int(item.get(field, 0) or 0))
                except (TypeError, ValueError):
                    hour[field] = 0
            result.append(hour)
        while len(result) < 24:
            result.append(cls._empty_cache_hour())
        return result

    def _record_hour_cache(self, date: str, hour: int, station: str,
                           cache_read: int | None,
                           input_tokens: int | None) -> None:
        cache_read_count = self._optional_count(cache_read)
        if cache_read_count is None or station == "-":
            return
        hour = max(0, min(23, int(hour)))
        series = self._normalise_cache_hours(
            self.hourly_cache.setdefault(date, {}).get(station))
        bucket = series[hour]
        bucket["cache_read"] += cache_read_count
        bucket["cache_reports"] += 1
        input_count = self._optional_count(input_tokens)
        if input_count is not None:
            bucket["cache_input"] += input_count
        self.hourly_cache[date][station] = series

    def _hour_cache_bucket(self, date: str, station: str, hour: int) -> dict:
        day = self.hourly_cache.get(date)
        if not isinstance(day, dict):
            return self._empty_cache_hour()
        series = self._normalise_cache_hours(day.get(station))
        return series[max(0, min(23, int(hour)))]

    def _day_cache_bucket(self, date: str, station: str) -> dict:
        day = self.daily.get(date)
        if not isinstance(day, dict):
            return {}
        bucket = day.get(station)
        return bucket if isinstance(bucket, dict) else {}

    def _cache_station_names(self) -> list[str]:
        names: set[str] = set()
        for store in (self.daily, self.hourly_cache):
            if not isinstance(store, dict):
                continue
            for day in store.values():
                if isinstance(day, dict):
                    names.update(
                        key for key in day
                        if isinstance(key, str) and key != "-")
        return sorted(names)

    @classmethod
    def _sum_cache_buckets(cls, buckets: list[dict]) -> dict:
        merged = {
            "cache_read": 0, "cache_input": 0, "cache_reports": 0,
            "input_tokens": 0, "cache_write": 0,
        }
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            for field in merged:
                try:
                    merged[field] += max(0, int(bucket.get(field, 0) or 0))
                except (TypeError, ValueError):
                    pass
        return merged

    @classmethod
    def _cache_row(cls, name: str, bucket: dict, extra: dict | None = None) -> dict:
        row = {"name": name, **cls._cache_view(bucket or {})}
        if extra:
            row.update(extra)
        return row

    @staticmethod
    def _rank_cache_rows(rows: list[dict]) -> list[dict]:
        rows.sort(key=lambda row: (
            not row.get("cache_known"),
            -(row["cache_hit_rate"] if row.get("cache_hit_rate") is not None else -1.0),
            -int(row.get("cache_input", 0) or 0),
            str(row.get("name", "")),
        ))
        for index, row in enumerate(rows):
            row["rank"] = index + 1
            row["is_best"] = bool(row.get("cache_known") and index == 0)
        return rows

    def cache_compare(self, now: datetime | None = None) -> dict:
        """Per-station cache hit rates for hour / best hour / recent days."""
        moment = now or datetime.fromtimestamp(time.time())
        today = moment.strftime("%Y-%m-%d")
        hour = moment.hour
        names = self._cache_station_names()
        dates_7 = [
            (moment - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(7)
        ]
        dates_31 = [
            (moment - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(31)
        ]

        hour_rows = []
        best_rows = []
        today_rows = []
        d7_rows = []
        d31_rows = []
        for name in names:
            hour_rows.append(self._cache_row(
                name, self._hour_cache_bucket(today, name, hour),
                {"window_label": f"{hour:02d}:00"}))

            best: dict | None = None
            for offset in range(24):
                slot = moment - timedelta(hours=offset)
                view = self._cache_view(self._hour_cache_bucket(
                    slot.strftime("%Y-%m-%d"), name, slot.hour))
                if not view["cache_known"]:
                    continue
                candidate = {
                    **view,
                    "window_label": slot.strftime("%m-%d %H:00"),
                }
                if best is None or (
                    view["cache_hit_rate"], view["cache_input"]
                ) > (best["cache_hit_rate"], best["cache_input"]):
                    best = candidate
            best_rows.append(self._cache_row(
                name, best or {},
                {"window_label": (best or {}).get("window_label", "")}))

            today_rows.append(self._cache_row(
                name, self._day_cache_bucket(today, name)))
            d7_rows.append(self._cache_row(
                name, self._sum_cache_buckets(
                    [self._day_cache_bucket(date, name) for date in dates_7])))
            d31_rows.append(self._cache_row(
                name, self._sum_cache_buckets(
                    [self._day_cache_bucket(date, name) for date in dates_31])))

        windows = {
            "hour": self._rank_cache_rows(hour_rows),
            "best_hour": self._rank_cache_rows(best_rows),
            "today": self._rank_cache_rows(today_rows),
            "d7": self._rank_cache_rows(d7_rows),
            "d31": self._rank_cache_rows(d31_rows),
        }
        return {
            "date": today,
            "current_hour": hour,
            "windows": windows,
            "best": {
                key: next((row["name"] for row in rows if row.get("is_best")), "")
                for key, rows in windows.items()
            },
        }

    @classmethod
    def _cache_view(cls, bucket: dict) -> dict:
        def count(name: str) -> int:
            try:
                return max(0, int(bucket.get(name, 0) or 0))
            except (TypeError, ValueError):
                return 0

        cache_read = count("cache_read")
        cache_input = count("cache_input")
        cache_reports = count("cache_reports")
        return {
            "input_tokens": count("input_tokens"),
            "cache_read": cache_read,
            "cache_write": count("cache_write"),
            "cache_reports": cache_reports,
            "cache_input": cache_input,
            "cache_hit_rate": cls._hit_rate(cache_read, cache_input),
            "cache_known": cache_reports > 0,
        }

    @classmethod
    def _ensure_usage_fields(cls, bucket: dict) -> dict:
        for field in (
            "tokens", "usage_reports", "input_tokens",
            "cache_read", "cache_write", "cache_reports", "cache_input",
        ):
            bucket.setdefault(field, 0)
        if "usage_reports_complete" not in bucket and "usage_reports" in bucket:
            bucket["usage_reports_complete"] = True
        return bucket

    @classmethod
    def _apply_usage(cls, *buckets: dict, tokens: int | None = None,
                     input_tokens: int | None = None,
                     cache_read: int | None = None,
                     cache_write: int | None = None) -> None:
        token_count = cls._optional_count(tokens)
        input_count = cls._optional_count(input_tokens)
        cache_read_count = cls._optional_count(cache_read)
        cache_write_count = cls._optional_count(cache_write)
        if token_count is None and input_count is None and cache_read_count is None:
            return
        for bucket in buckets:
            cls._ensure_usage_fields(bucket)
            if token_count is not None:
                bucket["usage_reports"] = max(
                    0, int(bucket.get("usage_reports", 0) or 0)) + 1
                bucket["tokens"] = max(0, int(bucket.get("tokens", 0) or 0)) + token_count
            if input_count is not None:
                bucket["input_tokens"] = max(
                    0, int(bucket.get("input_tokens", 0) or 0)) + input_count
            if cache_read_count is not None:
                bucket["cache_read"] = max(
                    0, int(bucket.get("cache_read", 0) or 0)) + cache_read_count
                bucket["cache_reports"] = max(
                    0, int(bucket.get("cache_reports", 0) or 0)) + 1
                if input_count is not None:
                    bucket["cache_input"] = max(
                        0, int(bucket.get("cache_input", 0) or 0)) + input_count
            if cache_write_count is not None:
                bucket["cache_write"] = max(
                    0, int(bucket.get("cache_write", 0) or 0)) + cache_write_count

    @staticmethod
    def _model_tokens(value: dict | int) -> int:
        if isinstance(value, dict):
            value = value.get("tokens", 0)
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _normalise_hours(value: object) -> list[int]:
        source = value if isinstance(value, list) else []
        result = []
        for item in source[:24]:
            try:
                result.append(max(0, int(item or 0)))
            except (TypeError, ValueError):
                result.append(0)
        return result + [0] * (24 - len(result))

    def _timeline_from_events(self, date: str) -> dict[str, list[int]]:
        timeline: dict[str, list[int]] = {}
        for event in self.events:
            station = str(event.get("station", "-") or "-")
            if station == "-":
                continue
            try:
                timestamp = float(event.get("ts", 0))
                moment = datetime.fromtimestamp(timestamp)
            except (TypeError, ValueError, OSError):
                continue
            if moment.strftime("%Y-%m-%d") != date:
                continue
            timeline.setdefault(station, [0] * 24)[moment.hour] += 1
        return timeline

    def _rebuild_legacy_totals(self) -> None:
        """Migrate totals that previously omitted station="-" failures."""
        rebuilt: dict[str, dict[str, int]] = {}
        for date, stations in self.daily.items():
            if not isinstance(date, str) or not isinstance(stations, dict):
                continue
            calls = 0
            failures = 0
            for value in stations.values():
                if not isinstance(value, dict):
                    continue
                try:
                    calls += max(0, int(value.get("calls", 0) or 0))
                    failures += max(0, int(value.get("failures", 0) or 0))
                except (TypeError, ValueError):
                    continue
            rebuilt[date] = {"calls": calls, "failures": failures}

        for event in self.events:
            if str(event.get("station", "-") or "-") != "-":
                continue
            try:
                date = datetime.fromtimestamp(float(event.get("ts", 0))).strftime("%Y-%m-%d")
            except (TypeError, ValueError, OSError):
                continue
            totals = rebuilt.setdefault(date, {"calls": 0, "failures": 0})
            totals["calls"] += 1
            totals["failures"] += not bool(event.get("ok", False))
        # A legacy file may already contain a more complete aggregate than
        # the bounded event ring can reconstruct. Never downgrade it.
        for date, value in self.daily_totals.items():
            if not isinstance(value, dict):
                continue
            target = rebuilt.setdefault(date, {"calls": 0, "failures": 0})
            for field in ("calls", "failures"):
                try:
                    target[field] = max(target[field], int(value.get(field, 0) or 0))
                except (TypeError, ValueError):
                    pass
        self.daily_totals = rebuilt

    def _prune_history(self) -> None:
        """Bound persisted aggregates by date and per-station model cardinality."""
        stores = (self.daily, self.daily_models, self.daily_totals,
                  self.daily_hours, self.hourly_cache)
        dates = sorted({key for store in stores for key in store if isinstance(key, str)})
        keep = set(dates[-STATS_RETENTION_DAYS:])
        for store in stores:
            for date in list(store):
                if date not in keep:
                    del store[date]
        for stations in self.daily_models.values():
            if not isinstance(stations, dict):
                continue
            for models in stations.values():
                self._cap_model_map(models)

    def add(self, station: str, model: str, ok: bool, latency_ms: float,
            detail: str = "", attempts: int = 1,
            tokens: int | None = None,
            input_tokens: int | None = None,
            cache_read: int | None = None,
            cache_write: int | None = None,
            ttft_ms: float | None = None,
            client_ip: str | None = None,
            route_id: int | None = None, occurred_at: float | None = None,
            outcome: str | None = None, event_id: str | None = None,
            requested_model: str | None = None, mapping_id: str | None = None) -> float:
        self.load()
        now = time.time() if occurred_at is None else occurred_at
        station = self._safe_text(station, EVENT_NAME_MAXLEN) or "-"
        model = self._safe_text(model, EVENT_NAME_MAXLEN) or "-"
        detail = self._safe_text(detail, EVENT_DETAIL_MAXLEN)
        try:
            client_ip = str(ipaddress.ip_address(client_ip)) if client_ip else None
        except ValueError:
            client_ip = None
        date = datetime.fromtimestamp(now).strftime("%Y-%m-%d")

        totals = self.daily_totals.get(date)
        if not isinstance(totals, dict):
            existing_day = self.daily.get(date, {})
            totals = {
                "calls": sum(max(0, int(item.get("calls", 0) or 0))
                             for item in existing_day.values()
                             if isinstance(item, dict)),
                "failures": sum(max(0, int(item.get("failures", 0) or 0))
                                for item in existing_day.values()
                                if isinstance(item, dict)),
            }
            self.daily_totals[date] = totals
        totals["calls"] = max(0, int(totals.get("calls", 0) or 0)) + 1
        totals["failures"] = max(0, int(totals.get("failures", 0) or 0)) + (not ok)

        if station != "-" and date not in self.daily_hours:
            self.daily_hours[date] = self._timeline_from_events(date)
        pending_key = f'{self._boot_id}:{route_id}'
        pending = self._pending.get(pending_key)
        self.events.append({
            'event_id': event_id or (pending['event_id'] if pending else uuid.uuid4().hex),
            'outcome': outcome or ('in_progress' if pending else 'success' if ok else 'failed'),
            "ts": now,
            "station": station,
            "model": model,
            "ok": ok,
            "latency_ms": round(latency_ms, 1),
            "detail": detail,
            "attempts": attempts,
            "tokens": tokens,
            "client_ip": client_ip,
            **({'requested_model': self._safe_text(requested_model, EVENT_NAME_MAXLEN),
                'mapping_id': self._safe_text(mapping_id, EVENT_NAME_MAXLEN)} if requested_model else {}),
            **({"route_id": route_id} if route_id is not None else {}),
        })
        # 今日按站聚合（跨天自动切换日期键；"-" 为全站失败记录，不计入单站）
        if station != "-":
            existing_station = self.daily.setdefault(date, {}).get(station)
            if existing_station is None:
                st = {"calls": 0, "failures": 0, "ms": 0.0,
                      "tokens": 0, "usage_reports": 0,
                      "input_tokens": 0, "cache_read": 0, "cache_write": 0,
                      "cache_reports": 0, "cache_input": 0,
                      "usage_reports_complete": True}
                self.daily[date][station] = st
            else:
                st = existing_station
                if "usage_reports_complete" not in st:
                    st["usage_reports_complete"] = "usage_reports" in st
            self._ensure_usage_fields(st)
            st["calls"] += 1
            if not ok:
                st["failures"] += 1
            st["ms"] += latency_ms
            if ttft_ms:
                st["ttft_sum"] = st.get("ttft_sum", 0.0) + ttft_ms
                st["ttft_n"] = st.get("ttft_n", 0) + 1
            model_stats = self.daily_models.setdefault(date, {}).setdefault(
                station, {})
            model_bucket = self._model_bucket(model_stats, model)
            model_bucket["calls"] += 1
            if not ok:
                model_bucket["failures"] += 1
            self._apply_usage(
                st, model_bucket, tokens=tokens, input_tokens=input_tokens,
                cache_read=cache_read, cache_write=cache_write)
            self._record_hour_cache(
                date, datetime.fromtimestamp(now).hour, station,
                cache_read, input_tokens)
            station_hours = self.daily_hours.setdefault(date, {}).setdefault(
                station, [0] * 24)
            station_hours = self._normalise_hours(station_hours)
            station_hours[datetime.fromtimestamp(now).hour] += 1
            self.daily_hours[date][station] = station_hours
        if pending:
            pending['logged'] = True
        self._commit(events=[self.events[-1]], days=self._days(date),
                     pending={pending_key: pending} if pending else None)
        return now

    def add_tokens(self, station: str, model: str, tokens: int,
                   occurred_at: float | None = None,
                   input_tokens: int | None = None,
                   cache_read: int | None = None,
                   cache_write: int | None = None,
                   route_id: int | None = None) -> None:
        """流式请求结束后补记 token 用量（add 时可能未知）。"""
        self.load()
        matched_event = None
        station = self._safe_text(station, EVENT_NAME_MAXLEN) or "-"
        model = self._safe_text(model, EVENT_NAME_MAXLEN) or "-"
        if station == "-" or tokens is None:
            return
        token_count = self._optional_count(tokens)
        if token_count is None:
            return
        usage_key = (station, model, occurred_at, route_id)
        pending_key = f'{self._boot_id}:{route_id}'
        pending = self._pending.get(pending_key)
        if occurred_at is not None and (usage_key in self._reported_usage or pending and pending.get('usage_reported')):
            return
        # Streaming usage arrives after the log row is created. Correlate by
        # request start and route, so concurrent calls never share IP usage.
        # An evicted row must not be recreated; daily totals still receive it.
        if occurred_at is not None:
            matches = [event for event in self.events
                       if event.get("ts") == occurred_at
                       and event.get("station") == station
                       and event.get("model") == model
                       and (route_id is None or event.get("route_id") == route_id)]
            if len(matches) == 1:
                if matches[0].get("tokens") is not None:
                    return  # A final usage report must only count once.
                matches[0]["tokens"] = token_count
                matched_event = matches[0]
        timestamp = time.time() if occurred_at is None else occurred_at
        date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        existing_station = self.daily.setdefault(date, {}).get(station)
        if existing_station is None:
            st = {"calls": 0, "failures": 0, "ms": 0.0,
                  "tokens": 0, "usage_reports": 0,
                  "input_tokens": 0, "cache_read": 0, "cache_write": 0,
                  "cache_reports": 0, "cache_input": 0,
                  "usage_reports_complete": True}
            self.daily[date][station] = st
        else:
            st = existing_station
            if "usage_reports_complete" not in st:
                st["usage_reports_complete"] = "usage_reports" in st
        model_stats = self.daily_models.setdefault(date, {}).setdefault(
            station, {})
        model_bucket = self._model_bucket(model_stats, model)
        self._apply_usage(
            st, model_bucket, tokens=tokens, input_tokens=input_tokens,
            cache_read=cache_read, cache_write=cache_write)
        self._record_hour_cache(
            date, datetime.fromtimestamp(timestamp).hour, station,
            cache_read, input_tokens)
        if occurred_at is not None:
            self._reported_usage[usage_key] = True
            if len(self._reported_usage) > EVENT_MAXLEN:
                del self._reported_usage[next(iter(self._reported_usage))]
        if pending:
            pending['usage_reported'] = True
        self._commit(events=[matched_event] if matched_event else [], days=self._days(date),
                     pending={pending_key: pending} if pending else None)

    def daily_stats(self) -> dict:
        """今日统计：各站 token/调用占比 + 站×模型明细 + 按小时时间线。"""
        now = time.time()
        today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        day = self.daily.get(today, {})
        day_models = self.daily_models.get(today, {})

        stations = []
        station_calls = 0
        station_failures = 0
        total_tokens = 0
        model_views: dict[str, dict] = {}
        station_token_totals: dict[str, int] = {}
        for name, s in day.items():
            station_calls += s["calls"]
            station_failures += s["failures"]
            raw_models = day_models.get(name, {})
            model_view = dict(raw_models) if isinstance(raw_models, dict) else {}
            self._cap_model_map(model_view)
            model_views[name] = model_view
            model_tokens = sum(self._model_tokens(value) for value in model_view.values())
            station_tokens = max(0, int(s.get("tokens", 0) or 0), model_tokens)
            station_token_totals[name] = station_tokens
            total_tokens += station_tokens
        totals = self.daily_totals.get(today, {})
        total_calls = max(station_calls, int(totals.get("calls", 0) or 0))
        total_failures = min(total_calls, max(
            station_failures, int(totals.get("failures", 0) or 0)))
        for name, s in day.items():
            # 站内模型明细（token 维度）
            models = []
            station_tokens = station_token_totals[name]
            for m, usage in sorted(
                    model_views[name].items(),
                    key=lambda kv: -self._model_tokens(kv[1])):
                model_bucket = self._model_bucket(model_views[name], m)
                tok = self._model_tokens(model_bucket)
                cache = self._cache_view(model_bucket)
                models.append({
                    "model": "其他模型" if m == OTHER_MODEL_KEY else m,
                    "calls": max(0, int(model_bucket.get("calls", 0) or 0)),
                    "failures": max(0, int(model_bucket.get("failures", 0) or 0)),
                    "tokens": tok,
                    "usage_reports": max(
                        0, int(model_bucket.get("usage_reports", 0) or 0)),
                    **cache,
                    # share 保持旧接口语义：占全站 Token；station_share 为站内占比。
                    "share": round(tok / total_tokens, 4)
                    if total_tokens else 0.0,
                    "station_share": round(tok / station_tokens, 4)
                    if station_tokens else 0.0,
                })
            usage_reports = max(0, int(s.get("usage_reports", 0) or 0))
            coverage_complete = bool(s.get("usage_reports_complete", False))
            stations.append({
                "name": name,
                "calls": s["calls"],
                "failures": s["failures"],
                "ms": round(s["ms"], 1),
                "tokens": station_tokens,
                "usage_reports": usage_reports,
                "token_coverage": round(usage_reports / s["calls"], 4)
                if coverage_complete and s["calls"] else
                (0.0 if coverage_complete else None),
                "token_coverage_known": coverage_complete,
                **self._cache_view(s),
                "share": round(s["calls"] / total_calls, 4) if total_calls else 0.0,
                "token_share": round(station_tokens / total_tokens, 4)
                if total_tokens else 0.0,
                "avg_ttft_ms": round(s.get('ttft_sum', 0.0) / s['ttft_n'], 1)
                if s.get('ttft_n') else None,
                "models": models,
            })
        stations.sort(key=lambda s: -s["tokens"])

        # 时间线：优先使用独立小时聚合；旧数据回退到事件回放。
        timeline: dict[str, list[int]] = {}
        hour = int(datetime.fromtimestamp(now).strftime("%H"))
        stored_hours = self.daily_hours.get(today, {})
        fallback_hours = self._timeline_from_events(today)
        for name in day:
            value = stored_hours.get(name) if isinstance(stored_hours, dict) else None
            timeline[name] = self._normalise_hours(
                value if isinstance(value, list) else fallback_hours.get(name))

        coverage_known = all(
            s["token_coverage_known"] for s in stations) if stations else True
        usage_reports = sum(s["usage_reports"] for s in stations)
        total_cache = self._cache_view({
            "input_tokens": sum(s["input_tokens"] for s in stations),
            "cache_read": sum(s["cache_read"] for s in stations),
            "cache_write": sum(s["cache_write"] for s in stations),
            "cache_reports": sum(s["cache_reports"] for s in stations),
            "cache_input": sum(s["cache_input"] for s in stations),
        })
        return {
            "date": today,
            "total_calls": total_calls,
            "total_failures": total_failures,
            "total_tokens": total_tokens,
            "usage_reports": usage_reports,
            "token_coverage": round(
                usage_reports / total_calls, 4)
            if coverage_known and total_calls else
            (0.0 if coverage_known else None),
            "token_coverage_known": coverage_known,
            **total_cache,
            "stations": stations,
            "timeline": timeline,
            "current_hour": hour,
            "cache_compare": self.cache_compare(datetime.fromtimestamp(now)),
        }

    def recent(self, n: int = 50) -> list[dict]:
        return list(self.events)[-n:]

    @staticmethod
    def _date_value(value: date | str | None) -> date | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))

    @staticmethod
    def _event_date(event: dict) -> date | None:
        try:
            return datetime.fromtimestamp(float(event.get("ts", 0))).date()
        except (TypeError, ValueError, OSError, OverflowError):
            return None

    @classmethod
    def _matches_log_filters(cls, event: dict, *, ip: str | None = None,
                             model: str | None = None,
                             date_from: date | str | None = None,
                             date_to: date | str | None = None) -> bool:
        event_ip = event.get("client_ip")
        if ip:
            if ip == "unknown":
                if event_ip:
                    return False
            elif event_ip != ip:
                return False
        if model and event.get("model", "-") != model:
            return False
        day = cls._event_date(event)
        start, end = cls._date_value(date_from), cls._date_value(date_to)
        if (start or end) and day is None:
            return False
        if start and day < start:
            return False
        if end and day > end:
            return False
        return True

    def paginated(self, page: int = 1, page_size: int = 50, *,
                  ip: str | None = None, model: str | None = None,
                  date_from: date | str | None = None,
                  date_to: date | str | None = None) -> dict:
        """Return filtered retained events, newest first, with page metadata."""
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), EVENT_PAGE_MAXLEN))
        newest_first = [event for event in reversed(self.events)
                        if self._matches_log_filters(
                            event, ip=ip, model=model,
                            date_from=date_from, date_to=date_to)]
        total = len(newest_first)
        pages = (total + page_size - 1) // page_size
        # If retention or a refresh removed a formerly valid last page, give
        # the client the current last page instead of an unexplained empty UI.
        page = min(page, pages or 1)
        start = (page - 1) * page_size
        return {
            "events": newest_first[start:start + page_size],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "max_events": self.maxlen,
            "retained_events": len(self.events),
        }

    def log_analytics(self, date_from: date | str | None = None,
                      date_to: date | str | None = None,
                      ip: str | None = None, model: str | None = None,
                      group_by: str = "ip") -> dict:
        """Aggregate charts and filter options from the retained event ring only."""
        today = datetime.now().date()
        end = self._date_value(date_to) or today
        start = self._date_value(date_from) or (end - timedelta(days=6))
        if start > end:
            raise ValueError("date_from must not be after date_to")
        days = (end - start).days + 1
        if days > 366:
            raise ValueError("log chart range cannot exceed 366 days")
        if group_by not in ("ip", "model"):
            raise ValueError("group_by must be ip or model")

        retained = list(self.events)
        ips = {}
        models = {}
        for event in retained:
            event_ip = event.get("client_ip")
            ip_key = event_ip or "unknown"
            model_key = str(event.get("model", "-") or "-")
            ips[ip_key] = ips.get(ip_key, 0) + 1
            models[model_key] = models.get(model_key, 0) + 1
        ip_options = [{"value": value, "label": "未记录 IP" if value == "unknown" else value}
                      for value, _ in sorted(ips.items(), key=lambda item: (-item[1], item[0]))[:100]]
        model_options = [{"value": value, "label": value}
                         for value, _ in sorted(models.items(), key=lambda item: (-item[1], item[0]))[:100]]

        matched = [event for event in retained
                   if self._matches_log_filters(
                       event, ip=ip, model=model,
                       date_from=start, date_to=end)]
        metrics = ("calls", "failures", "tokens", "usage_reports")
        def empty_bucket():
            return dict.fromkeys(metrics, 0)

        def accumulate(bucket, event):
            bucket["calls"] += 1
            bucket["failures"] += not bool(event.get("ok", False))
            tokens = event.get("tokens")
            if isinstance(tokens, (int, float)) and not isinstance(tokens, bool) and tokens >= 0:
                bucket["tokens"] += int(tokens)
                bucket["usage_reports"] += 1

        daily = {}
        for offset in range(days):
            current = start + timedelta(days=offset)
            daily[current.isoformat()] = {"date": current.isoformat(), **empty_bucket()}
        breakdown: dict[str, dict[str, int]] = {}
        ip_models = {}
        totals = empty_bucket()
        for event in matched:
            day = self._event_date(event)
            if day is None:
                continue
            bucket = daily[day.isoformat()]
            accumulate(bucket, event)
            accumulate(totals, event)
            key = ((event.get("client_ip") or "unknown") if group_by == "ip"
                   else str(event.get("model", "-") or "-"))
            item = breakdown.setdefault(key, empty_bucket())
            accumulate(item, event)
            pair = (event.get("client_ip") or "unknown", str(event.get("model") or "-"))
            accumulate(ip_models.setdefault(pair, empty_bucket()), event)
        ranked = sorted(breakdown.items(), key=lambda item: (-item[1]["calls"], item[0]))
        visible = ranked[:12]
        if len(ranked) > 12:
            overflow = empty_bucket()
            for _, item in ranked[12:]:
                for metric in metrics:
                    overflow[metric] += item[metric]
            visible.append(("__other__", overflow))
        items = [{"value": key,
                  "label": ("未记录 IP" if key == "unknown" else
                            "其他" if key == "__other__" else key),
                  **value} for key, value in visible]
        timestamps = [self._event_date(event) for event in retained]
        timestamps = [value for value in timestamps if value is not None]
        return {
            "date_from": start.isoformat(), "date_to": end.isoformat(),
            "group_by": group_by, "calls": len(matched),
            "failures": sum(not bool(event.get("ok", False)) for event in matched),
            "tokens": totals["tokens"], "usage_reports": totals["usage_reports"],
            "ip_reports": sum(bool(event.get("client_ip")) for event in matched),
            "ip_models": [{"ip": ip, "model": model, **bucket}
                          for (ip, model), bucket in sorted(ip_models.items(),
                              key=lambda item: (-item[1]["calls"], item[0]))],
            "retained_events": len(retained), "max_events": self.maxlen,
            "available_from": min(timestamps).isoformat() if timestamps else None,
            "available_to": max(timestamps).isoformat() if timestamps else None,
            "ips": ip_options, "models": model_options,
            "daily": list(daily.values()), "breakdown": items,
        }

    def daily_history(self, days: int = STATS_RETENTION_DAYS) -> dict:
        """Return bounded daily totals and station/model series for analytics."""
        days = max(1, min(int(days), STATS_RETENTION_DAYS))
        today = datetime.now().date()
        first_day = today - timedelta(days=days - 1)
        daily = []
        models = []
        retained_dates = [date for store in (self.daily, self.daily_totals)
                          for date in store
                          if isinstance(date, str) and date >= first_day.strftime("%Y-%m-%d")]
        available_from = min(retained_dates) if retained_dates else None
        for offset in range(days):
            date = (first_day + timedelta(days=offset)).strftime("%Y-%m-%d")
            stations = self.daily.get(date, {})
            day_models = self.daily_models.get(date, {})
            calls = failures = tokens = usage_reports = 0
            cache_read = cache_input = cache_reports = 0
            coverage_known = True
            for name, bucket in stations.items():
                if not isinstance(bucket, dict):
                    continue
                calls += max(0, int(bucket.get("calls", 0) or 0))
                failures += max(0, int(bucket.get("failures", 0) or 0))
                usage_reports += max(0, int(bucket.get("usage_reports", 0) or 0))
                coverage_known = coverage_known and bool(bucket.get("usage_reports_complete", "usage_reports" in bucket))
                cache_read += max(0, int(bucket.get("cache_read", 0) or 0))
                cache_input += max(0, int(bucket.get("cache_input", 0) or 0))
                cache_reports += max(0, int(bucket.get("cache_reports", 0) or 0))
                model_buckets = day_models.get(name, {})
                if not isinstance(model_buckets, dict):
                    model_buckets = {}
                model_tokens = 0
                model_calls = model_failures = 0
                for model, value in model_buckets.items():
                    model_bucket = value if isinstance(value, dict) else {}
                    model_tokens += self._model_tokens(value)
                    model_calls += max(0, int(model_bucket.get("calls", 0) or 0))
                    model_failures += max(0, int(model_bucket.get("failures", 0) or 0))
                    models.append({
                        "date": date,
                        "station": name,
                        "model": "其他模型" if model == OTHER_MODEL_KEY else str(model),
                        "calls": max(0, int(model_bucket.get("calls", 0) or 0)),
                        "failures": max(0, int(model_bucket.get("failures", 0) or 0)),
                        "tokens": self._model_tokens(value),
                        "usage_reports": max(0, int(model_bucket.get("usage_reports", 0) or 0)),
                    })
                station_tokens = max(max(0, int(bucket.get("tokens", 0) or 0)), model_tokens)
                tokens += station_tokens
                remainder = {"date": date, "station": name, "model": "未归类模型",
                             "tokens": station_tokens - model_tokens,
                             "calls": max(0, int(bucket.get("calls", 0) or 0) - model_calls),
                             "failures": max(0, int(bucket.get("failures", 0) or 0) - model_failures),
                             "usage_reports": 0}
                if any(remainder[key] for key in ("tokens", "calls", "failures")):
                    models.append(remainder)
            totals = self.daily_totals.get(date, {})
            daily.append({
                "date": date,
                "calls": max(calls, int(totals.get("calls", 0) or 0)),
                "failures": min(max(calls, int(totals.get("calls", 0) or 0)),
                                 max(failures, int(totals.get("failures", 0) or 0))),
                "tokens": tokens,
                "usage_reports": usage_reports,
                "token_coverage_known": coverage_known,
                "cache_read": cache_read,
                "cache_input": cache_input,
                "cache_reports": cache_reports,
            })
        return {"from": first_day.strftime("%Y-%m-%d"),
                "to": today.strftime("%Y-%m-%d"), "days": days,
                "available_from": available_from, "daily": daily, "models": models}

    def _prune_daily(self, retention_days: int = STATS_RETENTION_DAYS) -> None:
        """裁剪所有按天字典：只保留最近 retention_days 天（防无限增长）。"""
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
        for attr in ("daily", "daily_models", "daily_totals",
                     "daily_hours", "hourly_cache"):
            table = getattr(self, attr, None)
            if isinstance(table, dict):
                stale = [k for k in table if k < cutoff]
                for k in stale:
                    del table[k]
                if stale:
                    print(f"[stats] 已裁剪 {len(stale)} 天历史数据（{attr}，保留 {retention_days} 天）")

    def persist(self) -> bool:
        if not self.durable:
            return False
        if not self._loaded:
            if not self.store.path.exists() and not self.path.exists():
                self._loaded = True
                for event in self.events:
                    event.setdefault('event_id', uuid.uuid4().hex)
                try:
                    self.store.initialise(self._summary(), self._days(), list(self.events), self.maxlen)
                except (OSError, ValueError, sqlite3.Error, StaleStatsWriter) as exc:
                    self._read_only = True
                    self._storage_error = '统计初始化失败，原文件已保护；请检查磁盘或是否重复启动服务。'
                    logger.error('Statistics initialisation failed: %s', type(exc).__name__)
                    return False
            else:
                self.load()
        if self._read_only:
            return False
        try:
            self._prune_history()
            for event in self.events:
                event.setdefault('event_id', uuid.uuid4().hex)
            self._last_saved = self.store.write(events=list(self.events), days=self._days(),
                summary=self._summary(), maxlen=self.maxlen, replace=True,
                runtime=self._runtime, pending=self._pending)
            self._storage_error = ''
            self._retry_at = 0.0
            return True
        except (OSError, ValueError, sqlite3.Error, StaleStatsWriter) as exc:
            self._read_only |= isinstance(exc, StaleStatsWriter)
            self._storage_error = '统计保存失败，原文件未覆盖；新记录暂存内存，请检查磁盘或是否重复启动服务。'
            self._retry_at = time.time() + 5
            logger.error('Statistics persistence failed: %s', type(exc).__name__)
            return False


async def persist_loop(eventlog: EventLog, interval: float = 30.0) -> None:
    """后台定期落盘。"""
    while True:
        try:
            await __import__("asyncio").sleep(interval)
            eventlog.maintenance()
        except __import__("asyncio").CancelledError:
            break
        except Exception as e:  # noqa: BLE001
            print(f"[stats] 落盘任务异常: {e}")
