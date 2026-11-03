"""运行时状态注册表。

维护每个中转站的健康状态、延迟 EMA、调用统计、并发信号量与 HTTP 客户端。
路由、探测、统计、仪表盘都从这里读写状态。
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from .config import GatewayConfig, StationConfig
from . import juice
from .errors import UpstreamError
from .http_cleanup import close_resource, protect_response
from . import scoring

# EMA 平滑系数：越新延迟权重越高
EMA_ALPHA = 0.3

# 成功率滑动窗口大小
SCORE_WINDOW = 100

# 探针热力图：1 分钟 1 格，保留最近 60 分钟
HISTORY_SLOTS = 60
HISTORY_SLOT_SECONDS = 60

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_COOLING = "cooling"  # Compatibility value: cooldown elapsed, awaiting recovery validation.


@dataclass
class StationState:
    cfg: StationConfig
    enabled: bool | None = None

    # 健康状态
    status: str = STATUS_UP
    consecutive_failures: int = 0
    down_since: float = 0.0
    breaker_reason: str = ""
    breaker_source: str = ""
    breaker_status_code: int = 0
    recovery_inflight: bool = False
    recovery_attempt_at: float = 0.0
    ema_latency: float | None = None  # 秒
    ttft_ema: float | None = None  # 首字时间 EMA（秒）

    last_used_at: float = 0.0  # 最近成功使用时间（活跃判定）

    # ~~Juice~~ 模型匹配度（被动统计：上游头家族 + model 回显比对）
    upstream_families: dict = field(default_factory=dict)
    model_mismatch: int = 0
    last_probe_ok: bool | None = None
    last_probe_kind: str = ""
    last_probe_latency: float | None = None  # 秒
    last_probe_error: str = ""
    last_probe_at: float = 0.0
    probe_chat_at: float = 0.0  # 真实链路探测时间戳
    generation_probe_ok: bool | None = None
    generation_probe_error: str = ""
    generation_probe_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # 资金/配额状态（余额不足、订阅到期）
    # 默认自动探测恢复：/models 成功不视为有额度，需对症探测确认后才清除
    quota_exhausted: bool = False
    quota_error: str = ""
    quota_at: float = 0.0
    quota_source: str = ""  # models / chat / request
    quota_probe_at: float = 0.0
    quota_probe_error: str = ""
    quota_probe_backoff: float = 0.0
    last_probe_quota: bool = False

    # 429 限流短冷却（到点前不路由该站）
    rate_limit_until: float = 0.0
    rate_limit_reason: str = ""
    rate_limit_source: str = ""
    rate_limit_wait_source: str = ""

    # 模型（静态配置 ∪ 探测结果）
    probed_models: set[str] = field(default_factory=set)
    probed_model_order: list[str] = field(default_factory=list)
    model_api_paths: dict[str, list[str]] = field(default_factory=dict)
    model_unavailable: dict[str, str] = field(default_factory=dict)
    models_discovery_status: str = "pending"
    models_discovered_at: float = 0.0
    models_checked_at: float = 0.0
    models_next_refresh_at: float = 0.0
    models_retry_after_until: float = 0.0
    models_discovery_error: str = ""
    models_discovery_failures: int = 0
    models_discovery_revision: int = 0
    models_discovery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    go_usage: dict = field(default_factory=dict)
    go_usage_status: str = "pending"
    go_usage_error: str = ""
    go_usage_updated_at: float = 0.0
    go_usage_checked_at: float = 0.0
    go_usage_next_at: float = 0.0
    go_usage_retry_at: float = 0.0
    go_usage_failures: int = 0
    go_usage_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # 调用统计
    calls: int = 0
    failures: int = 0
    switch_in: int = 0   # 因故障切换而落到此站
    switch_out: int = 0  # 此站失败导致切换到别的站
    last_error: str = ""
    last_error_at: float = 0.0

    # Reliability-led composite with a bounded speed weight. None = no samples.
    health_score: float | None = None
    score_breakdown: dict = field(default_factory=dict)
    score_refreshed_at: float = 0.0
    window_results: deque = field(
        default_factory=lambda: deque(maxlen=SCORE_WINDOW))
    health_samples: deque = field(default_factory=lambda: deque(maxlen=scoring.STORED_SAMPLES))
    legacy_score_samples: int = 0

    # 探针历史：固定 60 个分钟槽（索引 0 = 当前分钟），事件按分钟累加，
    # 不因事件条数多而挤出历史（与事件队列不同，容量与分钟数解耦）
    history_minute: int = 0
    history_ok: list[int] = field(default_factory=lambda: [0] * HISTORY_SLOTS)
    history_fail: list[int] = field(default_factory=lambda: [0] * HISTORY_SLOTS)

    # 余额（credit_grants 探测，不支持时为 None）
    balance: float | None = None
    balance_at: float = 0.0

    # 运行时
    semaphore: asyncio.Semaphore | None = None
    client: httpx.AsyncClient | None = None
    persist_state: object = field(default=None, repr=False, compare=False)
    gateway: GatewayConfig | None = field(default=None, repr=False, compare=False)

    def connection_signature(self):
        cfg = self.cfg
        values = [cfg.api_base, cfg.api_key, cfg.headers, cfg.provider, cfg.opencode_plan, cfg.wire_api]
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    def durable_view(self):
        names = ('calls', 'failures', 'switch_in', 'switch_out', 'last_error', 'last_error_at',
                 'status', 'consecutive_failures', 'down_since', 'breaker_reason', 'breaker_source',
                 'breaker_status_code', 'ema_latency', 'ttft_ema', 'last_used_at',
                 'quota_exhausted', 'quota_error', 'quota_at', 'quota_source', 'quota_probe_at',
                 'quota_probe_error', 'quota_probe_backoff', 'rate_limit_until', 'rate_limit_reason',
                 'rate_limit_source', 'rate_limit_wait_source', 'go_usage_status', 'go_usage_error',
                 'go_usage_updated_at', 'go_usage_checked_at', 'go_usage_next_at', 'go_usage_retry_at', 'go_usage_failures')
        return {'signature': self.connection_signature(), 'saved_at': time.time(),
                'values': {name: getattr(self, name) for name in names},
                'window_results': list(self.window_results), 'go_usage': self.go_usage,
                'score_version': 2, 'health_samples': list(self.health_samples),
                'legacy_score_samples': self.legacy_score_samples}

    def restore_durable(self, data):
        if not isinstance(data, dict) or data.get('signature') != self.connection_signature():
            return
        values = data.get('values', {})
        if not isinstance(values, dict):
            return
        cached = data.get('go_usage')
        if isinstance(cached, dict) and cached:
            from .go_usage import normalize_usage
            from datetime import datetime, timezone
            try:
                self.go_usage = normalize_usage({'usage': {key: {
                    'percent': item.get('percent'), 'status': item.get('status'),
                    'resetsAt': datetime.fromtimestamp(item['resets_at'], timezone.utc).isoformat() if item.get('resets_at') is not None else None,
                } for key, item in cached.items()}})
            except (ValueError, TypeError, AttributeError, OverflowError, OSError):
                self.go_usage = {}
        for name in self.durable_view()['values']:
            value = values.get(name)
            current = getattr(self, name)
            if isinstance(current, bool):
                if isinstance(value, bool): setattr(self, name, value)
            elif isinstance(current, str):
                if isinstance(value, str): setattr(self, name, value[:2048])
            elif isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                setattr(self, name, value)
        if self.status not in (STATUS_UP, STATUS_DOWN, STATUS_COOLING):
            self.status = STATUS_UP
        self.calls = max(int(self.calls), int(self.failures))
        # Historical totals remain, but stale performance samples should not bias routing forever.
        saved_at = data.get('saved_at', 0)
        age = time.time() - saved_at if isinstance(saved_at, (int, float)) and math.isfinite(saved_at) else float('inf')
        if data.get('score_version') == 2:
            self.health_samples.extend(scoring.valid_samples(data.get('health_samples'), time.time()))
            legacy_count = data.get('legacy_score_samples', 0)
            if isinstance(legacy_count, int) and not isinstance(legacy_count, bool):
                self.legacy_score_samples = max(0, min(100, legacy_count))
        else:
            # Old booleans have neither occurrence time nor error attribution.
            # Preserve accounting, but do not present them as new reliable evidence.
            old_samples = data.get('window_results', [])
            self.legacy_score_samples = len(old_samples) if isinstance(old_samples, list) else 0
        if not 0 <= age < 86400:
            self.ema_latency = self.ttft_ema = None
        self._update_score()

    def _persist_state(self):
        if self.persist_state:
            self.persist_state(self.cfg.name, self.durable_view())

    def __post_init__(self) -> None:
        if self.enabled is None:
            self.enabled = self.cfg.enabled
        self._update_score()

    @property
    def priority_bias(self) -> float:
        return 0.0 if self.cfg.priority_auto else max(0, 6 - self.cfg.priority) * 3.0

    def model_discovery_view(self) -> dict:
        return {
            "status": self.models_discovery_status if self.cfg.probe_models else "off",
            "updated_at": self.models_discovered_at,
            "checked_at": self.models_checked_at,
            "next_refresh_at": self.models_next_refresh_at,
            "retry_after_until": self.models_retry_after_until,
            "error": self.models_discovery_error,
            "count": len(self.probed_models),
        }

    def go_usage_view(self):
        from .go_usage import usage_view
        return usage_view(self)

    @property
    def effective_models(self) -> set[str]:
        return {model for model in self.known_models if self.supports_model(model)}

    @property
    def known_models(self) -> set[str]:
        return set(self.cfg.models) | self.probed_models

    def protocol_view(self) -> dict[str, list[str]]:
        if self.cfg.provider in ("anthropic", "gemini"):
            path = "/messages" if self.cfg.provider == "anthropic" else "/generateContent"
            return {model: [path] for model in self.known_models}
        return dict(self.model_api_paths)

    def model_allowed(self, model: str) -> bool:
        return not self.cfg.route_models or any(
            model.startswith(rule[:-1]) if rule.endswith("*") else model == rule
            for rule in self.cfg.route_models)

    def supports_model(self, model: str, path: str | None = None) -> bool:
        from .protocols import capability_path, provider_supports
        if not self.model_allowed(model):
            return False
        if self.cfg.provider == "opencode":
            paths = self.model_api_paths.get(model, [])
            return bool(paths) and (path is None or capability_path(path) in paths)
        if not provider_supports(self.cfg.provider, path):
            return False
        if path == "/chat/completions" and self.cfg.wire_api == "responses":
            return False  # There is no Chat-to-Responses request/stream adapter.
        return self.accepts_any_model or model in self.known_models

    @property
    def probe_model_candidates(self) -> list[str]:
        discovered = [m for m in self.probed_model_order if m in self.probed_models]
        discovered += sorted(self.probed_models - set(discovered))
        candidates = list(dict.fromkeys((discovered if self.cfg.probe_models else []) + self.cfg.models))
        return [m for m in candidates if m in self.model_api_paths] if self.cfg.provider == "opencode" else candidates

    def generation_path(self, model: str) -> str:
        if self.cfg.provider in ("anthropic", "gemini"):
            return "/messages" if self.cfg.provider == "anthropic" else "/generateContent"
        paths = self.model_api_paths.get(model, []) if self.cfg.provider == "opencode" else []
        return paths[0] if paths else self.cfg.probe_chat_path

    @property
    def selected_probe_model(self) -> str:
        configured = self.cfg.probe_chat_model.strip()
        return configured or next(iter(self.probe_model_candidates), "")

    def generation_probe_view(self) -> dict:
        return {
            "mode": "manual" if self.cfg.probe_chat_model.strip() else "auto",
            "model": self.selected_probe_model,
            "last_at": self.probe_chat_at,
            "ok": self.generation_probe_ok,
            "error": self.generation_probe_error,
        }

    def protection_view(self, gateway: GatewayConfig, now: float) -> dict:
        until = 0.0
        source = "gateway"
        if not self.enabled:
            kind, reason = "disabled", "管理员已停用，暂停参与路由"
        elif self.quota_exhausted:
            kind, reason = "quota", self.quota_error
            source = self.quota_source
        elif now < self.rate_limit_until:
            kind, reason = "rate_limited", self.rate_limit_reason or "上游返回 HTTP 429"
            source, until = self.rate_limit_source, self.rate_limit_until
        elif self.status == STATUS_DOWN:
            until = self.down_since + gateway.cool_down
            kind = "breaker" if now < until else "recovering"
            reason = self.breaker_reason or self.last_error or "连续请求失败"
            source = self.breaker_source
        else:
            kind, reason = "normal", "允许参与路由"
        return {
            "kind": kind, "reason": reason, "source": source,
            "until": until, "remaining_seconds": max(0, int(until - now + 0.999)),
            "failure_count": self.consecutive_failures,
            "threshold": gateway.fail_threshold,
            "status_code": self.breaker_status_code if kind in {"breaker", "recovering"} else (429 if kind == "rate_limited" else None),
            "wait_source": self.rate_limit_wait_source if kind == "rate_limited" else "",
        }

    def recover_health(self) -> None:
        self.consecutive_failures = 0
        self.status = STATUS_UP
        self.down_since = 0.0
        self.breaker_reason = self.breaker_source = ""
        self.breaker_status_code = 0

    def mark_transient_failure(self, err: UpstreamError, gateway: GatewayConfig,
                               *, source: str = "request") -> None:
        self.consecutive_failures += 1
        self.breaker_reason = str(err)
        self.breaker_source = source
        self.breaker_status_code = err.status_code
        now = time.time()
        if (self.consecutive_failures >= gateway.fail_threshold
                and (self.status != STATUS_DOWN or now - self.down_since >= gateway.cool_down)):
            self.status = STATUS_DOWN
            self.down_since = now
        self._update_score()
        self._persist_state()

    @property
    def accepts_any_model(self) -> bool:
        """Only explicitly configured passthrough stations accept unknown model IDs."""
        return self.cfg.provider != "opencode" and self.cfg.model_routing == "passthrough"

    @property
    def success_rate(self) -> float:
        if self.calls == 0:
            return 1.0
        return (self.calls - self.failures) / self.calls

    @property
    def window_success_rate(self) -> float | None:
        return self.score_breakdown.get('success_rate')

    def routing_evidence(self, model: str, path: str | None, stream: bool | None = None) -> dict:
        from .protocols import capability_path
        return scoring.summarize(self.health_samples, time.time(), model=model,
                                 path=capability_path(path) if path else None, stream=stream,
                                 speed_weight=self.gateway.scoring_speed_weight if self.gateway else scoring.SPEED_WEIGHT)

    def _health_result(self, ok: bool, *, model: str = '', path: str = '',
                       stream: bool = False, latency: float | None = None) -> None:
        from .protocols import capability_path
        self.health_samples.append(dict(at=time.time(), ok=ok, model=model,
                                        path=capability_path(path), stream=stream, latency=latency))

    def _update_score(self) -> None:
        """Recompute without persisting on dashboard reads or ageing ticks."""
        now = time.time()
        self.health_samples = deque((s for s in self.health_samples
                                     if 0 <= now - s['at'] < scoring.MAX_AGE),
                                    maxlen=scoring.STORED_SAMPLES)
        view = scoring.summarize(self.health_samples, now,
                                 speed_weight=self.gateway.scoring_speed_weight if self.gateway else scoring.SPEED_WEIGHT)
        self.window_results = deque((s['ok'] for s in list(self.health_samples)[-SCORE_WINDOW:]),
                                    maxlen=SCORE_WINDOW)
        self.health_score = view['score'] if self.enabled else 0.0
        self.score_breakdown = {**view, 'version': 2, 'disabled': not self.enabled,
                                'legacy_samples_ignored': self.legacy_score_samples}
        self.score_refreshed_at = now

    def record_success(self, latency_s: float, switched: bool = False, *, model: str = '',
                       path: str = '', stream: bool = False, counted: bool = False,
                       signature: str | None = None) -> None:
        if not counted:
            self.calls += 1
        if switched:
            self.switch_in += 1
        if signature is not None and signature != self.connection_signature():
            self._persist_state()
            return
        # 成功即恢复健康
        self.recover_health()
        # EMA 平滑延迟
        if latency_s is not None:
            self.ema_latency = (
                latency_s
                if self.ema_latency is None
                else EMA_ALPHA * latency_s + (1 - EMA_ALPHA) * self.ema_latency
            )
        self.last_used_at = time.time()
        self._health_result(True, model=model, path=path, stream=stream, latency=latency_s)
        self._record_history(True)
        self._update_score()
        self._persist_state()

    def record_ttft(self, sec: float | None) -> None:
        """End-to-end first-content latency; independent of the health score."""
        if sec is None:
            return
        self.ttft_ema = (sec if self.ttft_ema is None
                         else EMA_ALPHA * sec + (1 - EMA_ALPHA) * self.ttft_ema)

    def note_upstream(self, headers, response_model: str | None, requested_model: str) -> None:
        """被动统计：记录上游头家族与 model 回显比对（juice 信号源）。"""
        from .juice import header_family
        fam = header_family(headers or {})
        self.upstream_families[fam] = self.upstream_families.get(fam, 0) + 1
        if response_model and response_model != requested_model:
            self.model_mismatch += 1

    def record_probe(self, latency_s: float) -> None:
        """Generation success restores availability, without fabricating business samples."""
        self.recover_health()
        self._record_history(True)
        self._update_score()
        self._persist_state()

    def _record_history(self, ok: bool) -> None:
        """记录探针历史：事件累加到当前分钟槽，跨分钟自动滚动。

        固定 60 槽，事件再多也不会挤出历史（仅 60 分钟后自然过期）。
        """
        minute = int(time.time() // HISTORY_SLOT_SECONDS)
        if minute != self.history_minute:
            shift = minute - self.history_minute
            if 0 < shift < HISTORY_SLOTS:
                # 时间前进：旧数据向高索引平移，新槽补 0
                self.history_ok = [0] * shift + self.history_ok[:HISTORY_SLOTS - shift]
                self.history_fail = [0] * shift + self.history_fail[:HISTORY_SLOTS - shift]
            else:
                # 异常跳跃（休眠/重启时间差）：直接清空
                self.history_ok = [0] * HISTORY_SLOTS
                self.history_fail = [0] * HISTORY_SLOTS
            self.history_minute = minute
        if ok:
            self.history_ok[0] += 1
        else:
            self.history_fail[0] += 1

    def history_hour(self) -> dict:
        """最近 60 分钟聚合（1 格 = 1 分钟，索引 0 最旧，最新在右）。

        返回 {"ok": [n...], "fail": [n...], "minutes": 60}，前端渲染红绿格。
        """
        return {"ok": list(reversed(self.history_ok)),
                "fail": list(reversed(self.history_fail)),
                "minutes": HISTORY_SLOTS}

    def record_failure(self, err: UpstreamError | Exception, switched: bool = True, *,
                       model: str = '', path: str = '', stream: bool = False,
                       counted: bool = False, signature: str | None = None) -> None:
        if not counted:
            self.calls += 1
        self.failures += 1
        if switched:
            self.switch_out += 1
        if signature is not None and signature != self.connection_signature():
            self._persist_state()
            return
        self.last_error = str(err)
        self.last_error_at = time.time()
        if scoring.affects_health(err):
            self._health_result(False, model=model, path=path, stream=stream)
        self._record_history(False)
        self._update_score()
        self._persist_state()

    # ---------- 资金/配额与限流 ----------
    def mark_quota_exhausted(self, err: UpstreamError,
                             source: str = "request") -> None:
        """资金/配额问题：从路由排除，并由后台按来源自动探测恢复。"""
        first = not self.quota_exhausted
        self.quota_exhausted = True
        self.quota_error = str(err)
        self.quota_at = time.time()
        if first or not self.quota_source:
            self.quota_source = source if source in {"models", "chat", "request"} else "request"
        if first:
            self.quota_probe_at = self.quota_at
            self.quota_probe_backoff = 0.0
            self.quota_probe_error = ""
        self.last_error = str(err)
        self.last_error_at = self.quota_at
        self._update_score()
        self._persist_state()

    def mark_rate_limited(self, cooldown_s: float, *, reason: str = "",
                          source: str = "request", wait_source: str = "gateway") -> None:
        """429 限流：短冷却期内不路由该站。"""
        until = time.time() + max(cooldown_s, 0)
        if until >= self.rate_limit_until:
            self.rate_limit_until = until
            self.rate_limit_reason = reason or "上游返回 HTTP 429"
            self.rate_limit_source = source
            self.rate_limit_wait_source = wait_source
        self._update_score()
        self._persist_state()

    def reset_quota(self) -> None:
        """清除配额耗尽状态（探测确认有额度，或管理员强制重置）。"""
        self.quota_exhausted = False
        self.quota_error = ""
        self.quota_at = 0.0
        self.quota_source = ""
        self.quota_probe_at = 0.0
        self.quota_probe_error = ""
        self.quota_probe_backoff = 0.0
        self.last_probe_quota = False
        self.rate_limit_until = 0.0
        self.rate_limit_reason = self.rate_limit_source = self.rate_limit_wait_source = ""
        self.recover_health()
        self._update_score()
        self._persist_state()


class Registry:
    def __init__(self, gateway: GatewayConfig, stations: list[StationConfig]):
        self.gateway = gateway
        self.states: dict[str, StationState] = {
            s.name: StationState(cfg=s, enabled=s.enabled, gateway=gateway) for s in stations
        }
        self._lock = asyncio.Lock()
        self._cleanup_tasks: set[asyncio.Task] = set()
        self._retired_clients: dict[httpx.AsyncClient, asyncio.Task | None] = {}
        # 路由活动由同步方法维护，FastAPI 的异步任务和测试线程都可安全读取。
        # last_routed 保留原有结构与字段名，仅表示最近一次成功路由，不代表当前使用。
        self._activity_lock = threading.RLock()
        self._next_route_id = 1
        self._activity_revision = 0
        self._active_routes: dict[int, dict] = {}
        self._last_attempt: dict | None = None
        self.eventlog = None
        self.last_routed: dict = {"station": "", "model": "", "ts": 0.0,
                                  "latency_ms": 0.0}
        # SSE listeners receive coalesced snapshots. A one-item queue keeps a
        # slow dashboard from accumulating stale route transitions.
        self._next_activity_subscriber_id = 1
        self._activity_subscribers: dict[
            int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]
        ] = {}

    @staticmethod
    def _enqueue_activity(queue: asyncio.Queue, snapshot: dict) -> None:
        """Replace a queued stale snapshot with the newest activity state."""
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            queue.put_nowait(snapshot)
        except asyncio.QueueFull:
            # Another notification won the race; that notification is at
            # least as recent as the state the client already has.
            pass

    def _activity_changed(self) -> None:
        """Publish the latest route state without blocking proxy requests."""
        with self._activity_lock:
            self._activity_revision += 1
            subscribers = list(self._activity_subscribers.values())
        if not subscribers:
            return
        snapshot = self.route_activity_snapshot()
        for loop, queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._enqueue_activity, queue, snapshot)
            except RuntimeError:
                # The request loop can close just before its generator runs
                # the unsubscribe finally block.
                continue

    def subscribe_route_activity(self) -> tuple[int, asyncio.Queue]:
        """Subscribe to immediate, coalesced route-activity snapshots."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        with self._activity_lock:
            subscriber_id = self._next_activity_subscriber_id
            self._next_activity_subscriber_id += 1
            self._activity_subscribers[subscriber_id] = (loop, queue)
        queue.put_nowait(self.route_activity_snapshot())
        return subscriber_id, queue

    def unsubscribe_route_activity(self, subscriber_id: int) -> None:
        with self._activity_lock:
            self._activity_subscribers.pop(subscriber_id, None)

    def begin_route(self, station: str, model: str, client_ip: str | None = None) -> int:
        """登记一次真正发往上游的尝试，返回用于精确结束该尝试的 ID。"""
        now = time.time()
        with self._activity_lock:
            route_id = self._next_route_id
            self._next_route_id += 1
            route = {
                "id": route_id,
                "station": station,
                "model": model,
                "started_at": now,
                "phase": "connecting",
            }
            self._active_routes[route_id] = route
            self._last_attempt = {
                **route,
                "finished_at": 0.0,
                "outcome": "inflight",
                "duration_ms": None,
                "error": "",
            }
        if self.eventlog:
            self.eventlog.start_route(route_id, station, model, client_ip)
        self._activity_changed()
        return route_id

    def update_route_phase(self, route_id: int, phase: str) -> None:
        """更新活跃路由阶段；已结束的路由会被安全忽略。"""
        with self._activity_lock:
            route = self._active_routes.get(route_id)
            if route is not None:
                route["phase"] = phase
                if self._last_attempt and self._last_attempt["id"] == route_id:
                    self._last_attempt["phase"] = phase
            else:
                return
        self._activity_changed()

    def end_route(self, route_id: int, outcome: str,
                  error: str = "") -> None:
        """注销活跃路由，并完成最近尝试记录。

        最近尝试按开始顺序定义：较早的并发请求即使更晚结束，也不会覆盖
        较晚开始的请求。
        """
        finished_at = time.time()
        with self._activity_lock:
            route = self._active_routes.pop(route_id, None)
            if route is None:
                return
            if self._last_attempt and self._last_attempt["id"] == route_id:
                self._last_attempt.update({
                    "finished_at": finished_at,
                    "outcome": outcome,
                    "duration_ms": round(
                        max(0.0, finished_at - route["started_at"]) * 1000, 1),
                    "error": error,
                })
        if self.eventlog:
            self.eventlog.finish_route(route_id, outcome, error)
        self._activity_changed()

    def note_fingerprint(self, st, model: str, text: str) -> None:
        """顺路采集真实响应特征到指纹（无 juice_registry 时安全跳过）。"""
        juice_registry = getattr(self, "juice_registry", None)
        print(f"[juice-proxy] {getattr(st, 'cfg', None) and st.cfg.name} {model} "
              f"text={repr(text)[:20]} reg={'ok' if juice_registry else 'MISSING'} "
              f"body={self._last_body[:80] if hasattr(self, '_last_body') else '?'}")
        if juice_registry is None or not text:
            return
        fp = juice_registry.get(st.cfg.name, model)
        fp.add_feature(text)
        fp.updated_at = time.time()
        juice_registry.refresh_flags()  # JSD 成本低，每次采样后立即重算

    def mark_routed(self, station: str, model: str, latency_ms: float) -> None:
        """记录最近一次成功路由；不用于判断当前是否仍有请求在执行。"""
        with self._activity_lock:
            self.last_routed = {
                "station": station,
                "model": model,
                "ts": time.time(),
                "latency_ms": round(latency_ms, 1),
            }
        self._activity_changed()

    def route_activity_snapshot(self, now: float | None = None) -> dict:
        """返回逐请求与站点/模型聚合的实时路由活动快照。"""
        current = time.time() if now is None else now
        with self._activity_lock:
            routes = []
            grouped: dict[str, dict] = {}
            for route in sorted(self._active_routes.values(),
                                key=lambda item: (item["started_at"], item["id"])):
                item = dict(route)
                item["elapsed_ms"] = round(
                    max(0.0, current - item["started_at"]) * 1000, 1)
                routes.append(item)

                station = grouped.setdefault(item["station"], {
                    "station": item["station"],
                    "inflight": 0,
                    "oldest_started_at": item["started_at"],
                    "models": {},
                })
                station["inflight"] += 1
                station["oldest_started_at"] = min(
                    station["oldest_started_at"], item["started_at"])
                models = station["models"]
                models[item["model"]] = models.get(item["model"], 0) + 1

            active_stations = []
            for station in grouped.values():
                active_stations.append({
                    "station": station["station"],
                    "inflight": station["inflight"],
                    "oldest_started_at": station["oldest_started_at"],
                    "models": [
                        {"model": model, "inflight": count}
                        for model, count in sorted(station["models"].items())
                    ],
                })
            active_stations.sort(key=lambda item: item["station"])

            last_success = (dict(self.last_routed)
                            if self.last_routed["station"] else None)
            return {
                "revision": self._activity_revision,
                "server_ts": current,
                "active": bool(routes),
                "inflight": len(routes),
                "active_routes": routes,
                "active_stations": active_stations,
                "last_attempt": (dict(self._last_attempt)
                                 if self._last_attempt is not None else None),
                "last_success": last_success,
            }

    # ---------- 配置 ----------
    @property
    def stations(self) -> list[StationConfig]:
        return [s.cfg for s in self.states.values()]

    def get(self, name: str) -> StationState | None:
        return self.states.get(name)

    def bind_statistics(self, eventlog):
        self.eventlog = eventlog
        for st in self.states.values():
            st.restore_durable(eventlog._runtime.get(st.cfg.name))
            # Recover counters from old JSON when that version did not persist station runtime.
            if st.cfg.name not in eventlog._runtime:
                rows = [day.get(st.cfg.name, {}) for day in eventlog.daily.values() if isinstance(day, dict)]
                st.calls = sum(int(row.get('calls', 0)) for row in rows if isinstance(row, dict))
                st.failures = sum(int(row.get('failures', 0)) for row in rows if isinstance(row, dict))
            st.persist_state = eventlog.save_runtime
            st._update_score()

    # ---------- 动态站点管理（运行时增删改，无需重启） ----------
    def add_station(self, cfg: StationConfig) -> StationState:
        """新增站点（运行时立即生效，下次探测/请求即纳入调度）。"""
        st = StationState(cfg=cfg, enabled=cfg.enabled, gateway=self.gateway)
        if self.eventlog:
            st.persist_state = self.eventlog.save_runtime
        self.ensure_client(st)
        self.ensure_semaphore(st)
        self.states[cfg.name] = st
        return st

    def update_station(self, name: str, changes: dict) -> StationState:
        """更新站点配置；base_url/key/并发等变化时重建客户端与信号量。"""
        st = self.states[name]
        previous_signature = st.connection_signature()
        connection_changed = any(k in changes and changes[k] != getattr(st.cfg, k)
                                 for k in ("base_url", "api_key", "provider", "opencode_plan"))
        st.cfg = dataclasses.replace(st.cfg, **changes)
        if previous_signature != st.connection_signature():
            st.health_samples.clear()
            st.legacy_score_samples = 0
            st.ema_latency = st.ttft_ema = None
        if connection_changed or "headers" in changes:
            st.go_usage = {}
            st.go_usage_status, st.go_usage_error = "pending", ""
            st.go_usage_updated_at = st.go_usage_checked_at = st.go_usage_next_at = st.go_usage_retry_at = 0.0
            st.go_usage_failures = 0
        if connection_changed and st.quota_exhausted:
            # A rejection from the previous credentials/billing endpoint is not evidence
            # that the replacement connection has exhausted its quota.
            st.reset_quota()
        if "enabled" in changes:
            st.enabled = st.cfg.enabled
            st._update_score()
        if any(k in changes for k in (
                "base_url", "api_key", "headers", "probe_models", "provider", "opencode_plan")):
            st.probed_models.clear()
            st.probed_model_order.clear()
            st.model_api_paths.clear()
            st.model_unavailable.clear()
            st.models_discovery_revision += 1
            st.models_discovery_status = "pending"
            st.models_discovered_at = st.models_checked_at = st.models_next_refresh_at = 0.0
            st.models_retry_after_until = 0.0
            st.models_discovery_error = ""
            st.models_discovery_failures = 0
        if any(k in changes for k in ("base_url", "api_key", "headers",
                                      "timeout", "max_concurrency", "provider", "opencode_plan")):
            if st.client is not None:
                self.reset_client(st)
            if "max_concurrency" in changes:
                st.semaphore = asyncio.Semaphore(st.cfg.max_concurrency)
        st._update_score()
        st._persist_state()
        return st

    def remove_station(self, name: str) -> None:
        """删除站点并异步关闭其连接池。"""
        st = self.states.pop(name, None)
        if st is not None and st.client is not None:
            self.reset_client(st)

    # ---------- HTTP 客户端 ----------
    def ensure_client(self, state: StationState) -> httpx.AsyncClient:
        if state.client is None or state.client.is_closed:
            timeout = httpx.Timeout(
                connect=self.gateway.connect_timeout,
                read=state.cfg.timeout,
                write=30.0,
                pool=10.0,
            )
            state.client = httpx.AsyncClient(
                timeout=timeout,
                event_hooks={"response": [self._protect_response]},
                limits=httpx.Limits(
                    max_connections=max(state.cfg.max_concurrency * 2, 10),
                    max_keepalive_connections=state.cfg.max_concurrency,
                ),
                follow_redirects=True,
            )
        return state.client

    async def _protect_response(self, response: httpx.Response) -> None:
        protect_response(response, self.start_cleanup)

    def start_cleanup(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._cleanup_tasks.add(task)

        def done(task):
            self._cleanup_tasks.discard(task)
            if not task.cancelled():
                task.exception()

        task.add_done_callback(done)
        return task

    def reset_client(self, state: StationState, *, grace: float | None = None,
                     expected_client: httpx.AsyncClient | None = None) -> bool:
        """Rebuild a station client whose connection pool is exhausted.

        httpcore marks a response stream closed before releasing its socket.
        If that first close is interrupted by a cancelled ASGI task, the
        connection stays checked out forever and every later request ends in
        PoolTimeout. A fresh client restores routing immediately; the old one
        keeps serving in-flight streams and is closed after a grace period.
        """
        old = state.client
        if old is None or old.is_closed or (expected_client is not None and old is not expected_client):
            return False
        state.client = None
        self._retired_clients[old] = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return True
        if grace is None:
            grace = max(60.0, self.gateway.read_timeout, state.cfg.timeout)

        async def drain() -> None:
            await asyncio.sleep(grace)
            await asyncio.shield(self.start_cleanup(close_resource(old)))
            self._retired_clients.pop(old, None)

        task = loop.create_task(drain())
        self._retired_clients[old] = task
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        return True

    def ensure_semaphore(self, state: StationState) -> asyncio.Semaphore:
        if state.semaphore is None:
            state.semaphore = asyncio.Semaphore(state.cfg.max_concurrency)
        return state.semaphore

    async def close_clients(self) -> None:
        retired = list(self._retired_clients.items())
        drains = [task for _, task in retired if task is not None]
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
        clients = {client for client, _ in retired}
        for st in self.states.values():
            if st.client is not None:
                clients.add(st.client)
                st.client = None
        # Finish independent response cleanup before tearing down their pools.
        while self._cleanup_tasks:
            batch = list(self._cleanup_tasks)
            await asyncio.gather(*batch, return_exceptions=True)
            self._cleanup_tasks.difference_update(batch)
        await asyncio.gather(*(close_resource(client) for client in clients))
        self._retired_clients.clear()

    # ---------- 健康/路由 ----------
    def is_routeable(self, state: StationState, now: float | None = None) -> bool:
        """是否可作为路由候选：启用 + 无资金问题 + 未熔断（冷却中除外）。"""
        now = now or time.time()
        if state.score_refreshed_at and now - state.score_refreshed_at >= 1.0:
            state._update_score()
        if not state.enabled:
            return False
        if state.quota_exhausted:
            return False  # 余额不足/订阅到期：等额度恢复探测确认后再加入路由
        if now < state.rate_limit_until:
            return False  # 429 短冷却中
        if state.status == STATUS_DOWN:
            # 已过冷却期 → 值得再试（被动探测，成功即恢复）
            if now - state.down_since < self.gateway.cool_down:
                return False
        return True

    def candidates_for(self, model: str, path: str | None = None) -> list[StationState]:
        """按 (优先级, 延迟加权) 排序的可路由候选站。"""
        now = time.time()
        cands = []
        for st in self.states.values():
            if not self.is_routeable(st, now):
                continue
            if not st.supports_model(model, path):
                continue
            cands.append(st)

        cands.sort(key=lambda st: (6 if st.cfg.priority_auto else st.cfg.priority,
                                   st.ema_latency if st.ema_latency is not None else 999.0))
        return cands

    # ---------- 统计快照（仪表盘） ----------
    def snapshot(self) -> dict:
        now = time.time()
        route_activity = self.route_activity_snapshot(now)
        stations = []
        for st in self.states.values():
            if st.score_refreshed_at and now - st.score_refreshed_at >= 1.0:
                st._update_score()
            status = st.status
            if status == STATUS_DOWN and now - st.down_since >= self.gateway.cool_down:
                status = STATUS_COOLING
            stations.append(
                {
                    "name": st.cfg.name,
                    "base_url": st.cfg.base_url,
                    "enabled": st.enabled,
                    "status": status,
                    "protection": st.protection_view(self.gateway, now),
                    "billing": st.cfg.billing,
                    "quota_exhausted": st.quota_exhausted,
                    "quota_error": st.quota_error,
                    "quota_at": st.quota_at,
                    "quota_source": st.quota_source,
                    "quota_probe_at": st.quota_probe_at,
                    "quota_probe_error": st.quota_probe_error,
                    "quota_probe_backoff": st.quota_probe_backoff,
                    "rate_limit_until": st.rate_limit_until,
                    "health_score": round(st.health_score, 1) if st.health_score is not None else None,
                    "health_score_breakdown": dict(st.score_breakdown),
                    "ttft_ema": st.ttft_ema,
                    "juice_score": juice.juice_score(st),
                    "window_success_rate": round(st.window_success_rate, 4) if st.window_success_rate is not None else None,
                    "balance": st.balance,
                    "balance_at": st.balance_at,
                    "history_hour": st.history_hour(),
                    "priority": st.cfg.priority,
                    "priority_auto": st.cfg.priority_auto,
                    "priority_bias": st.priority_bias,
                    "weight": st.cfg.weight,
                    "consecutive_failures": st.consecutive_failures,
                    "down_since": st.down_since,
                    "ema_latency": st.ema_latency,
                    "last_probe_ok": st.last_probe_ok,
                    "last_probe_kind": st.last_probe_kind,
                    "last_check_at": st.probe_chat_at if st.last_probe_kind == "generation" else st.last_probe_at,
                    "last_probe_latency": st.last_probe_latency,
                    "last_probe_error": st.last_probe_error,
                    "last_probe_at": st.last_probe_at,
                    "models": sorted(st.effective_models),
                    "detected_models": sorted(st.probed_models),
                    "model_discovery": st.model_discovery_view(),
                    "generation_probe": st.generation_probe_view(),
                    "probe_chat": st.cfg.probe_chat,
                    "accepts_any_model": st.accepts_any_model,
                    "model_routing": st.cfg.model_routing,
                    "route_models": list(st.cfg.route_models),
                    "provider": st.cfg.provider,
                    "opencode_plan": st.cfg.opencode_plan,
                    "go_usage": st.go_usage_view(),
                    "model_api_paths": st.protocol_view(),
                    "probe_model_candidates": list(st.probe_model_candidates),
                    "model_unavailable": dict(st.model_unavailable),
                    "calls": st.calls,
                    "failures": st.failures,
                    "success_rate": round(st.success_rate, 4),
                    "switch_in": st.switch_in,
                    "switch_out": st.switch_out,
                    "last_error": st.last_error,
                    "last_error_at": st.last_error_at,
                    "max_concurrency": st.cfg.max_concurrency,
                }
            )
        up = sum(1 for s in stations if s["status"] == STATUS_UP and s["enabled"])
        # Overview only; actual routing uses this request's model and protocol.
        for s in stations:
            s["rank"] = None
        ordered = sorted((s for s in stations
                          if self.is_routeable(self.states[s["name"]], now)
                          and (s["models"] or s["accepts_any_model"])),
                         key=lambda s: (s["health_score"] if s["health_score"] is not None else 95.0,
                                        -(6 if s["priority_auto"] else s["priority"])),
                         reverse=True)
        for i, s in enumerate(ordered):
            s["rank"] = i + 1
        return {"gateway": {"max_attempts": self.gateway.max_attempts,
                             "retry_backoff": self.gateway.retry_backoff,
                             "max_rounds": self.gateway.max_rounds,
                             "round_backoff": self.gateway.round_backoff,
                             "max_round_backoff": self.gateway.max_round_backoff,
                             "max_retry_seconds": self.gateway.max_retry_seconds,
                             "cool_down": self.gateway.cool_down,
                             "scoring_speed_weight": self.gateway.scoring_speed_weight,
                             "fail_threshold": self.gateway.fail_threshold,
                             "rate_limit_cooldown": self.gateway.rate_limit_cooldown,
                             "probe_interval": self.gateway.probe_interval,
                             "balance_alert_threshold": self.gateway.balance_alert_threshold,
                             "public_url": self.gateway.public_url,
                             "generation_probe_enabled": self.gateway.generation_probe_enabled,
                             "generation_probe_interval": self.gateway.generation_probe_interval,
                             "quota_auto_recover": self.gateway.quota_auto_recover,
                             "quota_probe_interval": self.gateway.quota_probe_interval,
                             "quota_probe_max_interval": self.gateway.quota_probe_max_interval},
                "stations": stations,
                # last_routed 保持向后兼容；route_activity 是新代码的规范入口。
                "last_routed": (route_activity["last_success"]
                                or {"station": "", "model": "", "ts": 0.0,
                                    "latency_ms": 0.0}),
                "route_activity": route_activity,
                "active_routes": route_activity["active_routes"],
                "active_stations": route_activity["active_stations"],
                "last_attempt": route_activity["last_attempt"],
                "last_success": route_activity["last_success"],
                "summary": {"total": len(stations), "up": up}}
