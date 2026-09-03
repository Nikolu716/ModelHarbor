"""运行时状态注册表。

维护每个中转站的健康状态、延迟 EMA、调用统计、并发信号量与 HTTP 客户端。
路由、探测、统计、仪表盘都从这里读写状态。
"""
from __future__ import annotations

import asyncio
import dataclasses
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from .config import GatewayConfig, StationConfig
from . import juice
from .errors import UpstreamError

# EMA 平滑系数：越新延迟权重越高
EMA_ALPHA = 0.3

# 成功率滑动窗口大小
SCORE_WINDOW = 100

# 探针热力图：1 分钟 1 格，保留最近 60 分钟
HISTORY_SLOTS = 60
HISTORY_SLOT_SECONDS = 60

STATUS_UP = "up"
STATUS_DOWN = "down"
STATUS_COOLING = "cooling"  # 冷却中，不路由


@dataclass
class StationState:
    cfg: StationConfig
    enabled: bool = True

    # 健康状态
    status: str = STATUS_UP
    consecutive_failures: int = 0
    down_since: float = 0.0
    ema_latency: float | None = None  # 秒
    ttft_ema: float | None = None  # 首字时间 EMA（秒）

    last_used_at: float = 0.0  # 最近成功使用时间（活跃判定）

    # ~~Juice~~ 模型匹配度（被动统计：上游头家族 + model 回显比对）
    upstream_families: dict = field(default_factory=dict)
    model_mismatch: int = 0
    last_probe_ok: bool | None = None
    last_probe_latency: float | None = None  # 秒
    last_probe_error: str = ""
    last_probe_at: float = 0.0
    probe_chat_at: float = 0.0  # 真实链路探测时间戳

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

    # 模型（静态配置 ∪ 探测结果）
    probed_models: set[str] = field(default_factory=set)

    # 调用统计
    calls: int = 0
    failures: int = 0
    switch_in: int = 0   # 因故障切换而落到此站
    switch_out: int = 0  # 此站失败导致切换到别的站
    last_error: str = ""
    last_error_at: float = 0.0

    # 动态健康评分（0-100，路由排序依据）
    health_score: float = 60.0
    window_results: deque = field(
        default_factory=lambda: deque(maxlen=SCORE_WINDOW))

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

    @property
    def effective_models(self) -> set[str]:
        return set(self.cfg.models) | self.probed_models

    @property
    def accepts_any_model(self) -> bool:
        """未声明模型列表且未开启探测时，接受所有模型。"""
        return not self.cfg.models and not self.cfg.probe_models

    @property
    def success_rate(self) -> float:
        if self.calls == 0:
            return 1.0
        return (self.calls - self.failures) / self.calls

    @property
    def window_success_rate(self) -> float:
        """最近 SCORE_WINDOW 次调用的成功率（动态评分用，新站默认 0.8 保守）。"""
        if not self.window_results:
            return 0.8
        return sum(self.window_results) / len(self.window_results)

    def _update_score(self) -> None:
        """动态健康评分（0-100）：延迟 + 滑动成功率 + 状态惩罚 + 手动偏好。"""
        score = 0.0
        # 延迟分量（0-40 分）：1s 内接近满分，线性衰减，10s 以上为 0
        if self.ema_latency is not None:
            score += max(0.0, 40.0 - self.ema_latency * 4.0)
        else:
            score += 20.0  # 未测量过延迟，给中等分
        # 成功率分量（0-30 分）
        score += self.window_success_rate * 30.0
        # 状态惩罚
        if self.status == STATUS_DOWN:
            score -= 35.0
        if self.quota_exhausted:
            score -= 60.0
        # 手动优先级偏好（1→+15，5→+3）：仍是"偏好"而非硬排序
        score += max(0, 6 - self.cfg.priority) * 3.0
        self.health_score = max(0.0, min(100.0, score))

    def record_success(self, latency_s: float, switched: bool = False) -> None:
        self.calls += 1
        if switched:
            self.switch_in += 1
        # 成功即恢复健康
        self.consecutive_failures = 0
        if self.status == STATUS_DOWN:
            self.status = STATUS_UP
            self.down_since = 0.0
        # EMA 平滑延迟
        if latency_s is not None:
            self.ema_latency = (
                latency_s
                if self.ema_latency is None
                else EMA_ALPHA * latency_s + (1 - EMA_ALPHA) * self.ema_latency
            )
        self.last_used_at = time.time()
        self.window_results.append(True)
        self._record_history(True)
        self._update_score()

    def record_ttft(self, sec: float | None) -> None:
        """记录首字时间（TTFT）EMA：响应头到达 → 首个数据块。"""
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
        """探测成功：更新延迟/评分，但不计入业务调用统计。"""
        if latency_s is not None:
            self.ema_latency = (
                latency_s
                if self.ema_latency is None
                else EMA_ALPHA * latency_s + (1 - EMA_ALPHA) * self.ema_latency
            )
        self.window_results.append(True)
        self._record_history(True)
        self._update_score()

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

    def record_failure(self, err: UpstreamError | Exception, switched: bool = True) -> None:
        self.calls += 1
        self.failures += 1
        if switched:
            self.switch_out += 1
        self.last_error = str(err)
        self.last_error_at = time.time()
        self.window_results.append(False)
        self._record_history(False)
        self._update_score()

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

    def mark_rate_limited(self, cooldown_s: float) -> None:
        """429 限流：短冷却期内不路由该站。"""
        self.rate_limit_until = time.time() + max(cooldown_s, 0)

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
        self.consecutive_failures = 0
        self.status = STATUS_UP
        self.down_since = 0.0
        self._update_score()


class Registry:
    def __init__(self, gateway: GatewayConfig, stations: list[StationConfig]):
        self.gateway = gateway
        self.states: dict[str, StationState] = {
            s.name: StationState(cfg=s, enabled=s.enabled) for s in stations
        }
        self._lock = asyncio.Lock()
        # 路由活动由同步方法维护，FastAPI 的异步任务和测试线程都可安全读取。
        # last_routed 保留原有结构与字段名，仅表示最近一次成功路由，不代表当前使用。
        self._activity_lock = threading.RLock()
        self._next_route_id = 1
        self._activity_revision = 0
        self._active_routes: dict[int, dict] = {}
        self._last_attempt: dict | None = None
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

    def begin_route(self, station: str, model: str) -> int:
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

    # ---------- 动态站点管理（运行时增删改，无需重启） ----------
    def add_station(self, cfg: StationConfig) -> StationState:
        """新增站点（运行时立即生效，下次探测/请求即纳入调度）。"""
        st = StationState(cfg=cfg, enabled=cfg.enabled)
        self.ensure_client(st)
        self.ensure_semaphore(st)
        self.states[cfg.name] = st
        return st

    def update_station(self, name: str, changes: dict) -> StationState:
        """更新站点配置；base_url/key/并发等变化时重建客户端与信号量。"""
        st = self.states[name]
        st.cfg = dataclasses.replace(st.cfg, **changes)
        if "enabled" in changes:
            st.enabled = st.cfg.enabled
        if any(k in changes for k in (
                "base_url", "api_key", "models", "probe_models", "probe_path")):
            st.probed_models.clear()
        if any(k in changes for k in ("base_url", "api_key", "headers",
                                      "timeout", "max_concurrency")):
            if st.client is not None:
                asyncio.create_task(st.client.aclose())
                st.client = None  # 懒重建：ensure_client 会新建
            st.semaphore = asyncio.Semaphore(st.cfg.max_concurrency)
        st._update_score()
        return st

    def remove_station(self, name: str) -> None:
        """删除站点并异步关闭其连接池。"""
        st = self.states.pop(name, None)
        if st is not None and st.client is not None:
            asyncio.create_task(st.client.aclose())
            st.client = None

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
                limits=httpx.Limits(
                    max_connections=max(state.cfg.max_concurrency * 2, 10),
                    max_keepalive_connections=state.cfg.max_concurrency,
                ),
                follow_redirects=True,
            )
        return state.client

    def ensure_semaphore(self, state: StationState) -> asyncio.Semaphore:
        if state.semaphore is None:
            state.semaphore = asyncio.Semaphore(state.cfg.max_concurrency)
        return state.semaphore

    async def close_clients(self) -> None:
        for st in self.states.values():
            if st.client is not None:
                await st.client.aclose()
                st.client = None

    # ---------- 健康/路由 ----------
    def is_routeable(self, state: StationState, now: float | None = None) -> bool:
        """是否可作为路由候选：启用 + 无资金问题 + 未熔断（冷却中除外）。"""
        if not state.enabled:
            return False
        if state.quota_exhausted:
            return False  # 余额不足/订阅到期：等额度恢复探测确认后再加入路由
        now = now or time.time()
        if now < state.rate_limit_until:
            return False  # 429 短冷却中
        if state.status == STATUS_DOWN:
            # 已过冷却期 → 值得再试（被动探测，成功即恢复）
            if now - state.down_since < self.gateway.cool_down:
                return False
        return True

    def candidates_for(self, model: str) -> list[StationState]:
        """按 (优先级, 延迟加权) 排序的可路由候选站。"""
        now = time.time()
        cands = []
        for st in self.states.values():
            if not self.is_routeable(st, now):
                continue
            if not st.accepts_any_model and model not in st.effective_models:
                continue
            cands.append(st)

        cands.sort(key=lambda st: (st.cfg.priority, st.ema_latency or 999.0))
        return cands

    # ---------- 统计快照（仪表盘） ----------
    def snapshot(self) -> dict:
        now = time.time()
        route_activity = self.route_activity_snapshot(now)
        stations = []
        for st in self.states.values():
            status = st.status
            if status == STATUS_DOWN and now - st.down_since >= self.gateway.cool_down:
                status = STATUS_COOLING
            stations.append(
                {
                    "name": st.cfg.name,
                    "base_url": st.cfg.base_url,
                    "enabled": st.enabled,
                    "status": status,
                    "billing": st.cfg.billing,
                    "quota_exhausted": st.quota_exhausted,
                    "quota_error": st.quota_error,
                    "quota_at": st.quota_at,
                    "quota_source": st.quota_source,
                    "quota_probe_at": st.quota_probe_at,
                    "quota_probe_error": st.quota_probe_error,
                    "quota_probe_backoff": st.quota_probe_backoff,
                    "rate_limit_until": st.rate_limit_until,
                    "health_score": round(st.health_score, 1),
                    "ttft_ema": st.ttft_ema,
                    "juice_score": juice.juice_score(st),
                    "window_success_rate": round(st.window_success_rate, 4),
                    "balance": st.balance,
                    "balance_at": st.balance_at,
                    "history_hour": st.history_hour(),
                    "priority": st.cfg.priority,
                    "weight": st.cfg.weight,
                    "consecutive_failures": st.consecutive_failures,
                    "down_since": st.down_since,
                    "ema_latency": st.ema_latency,
                    "last_probe_ok": st.last_probe_ok,
                    "last_probe_latency": st.last_probe_latency,
                    "last_probe_error": st.last_probe_error,
                    "last_probe_at": st.last_probe_at,
                    "models": sorted(st.effective_models),
                    "accepts_any_model": st.accepts_any_model,
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
        # 动态排名：按健康评分降序（托管模式下的实际路由顺序）
        ordered = sorted(stations,
                         key=lambda s: (s["health_score"], -s["priority"]),
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
                             "probe_interval": self.gateway.probe_interval,
                             "balance_alert_threshold": self.gateway.balance_alert_threshold,
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
