"""路由与故障转移。

根据模型名选出候选站（启用 + 健康 + 支持该模型），按**动态健康评分**
排序。可靠性为基础，速度占比由账号配置；同档位按配置权重和手动偏好分流。
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass

from .config import GatewayConfig
from .state import Registry, StationState, STATUS_DOWN

# 评分相差不超过该值时视为"同等健康"，组内加权随机
SCORE_BAND = 5.0


@dataclass
class RoutePermit:
    semaphore: asyncio.Semaphore
    recovery: bool = False
    released: bool = False
    quota_revision: int = 0
    native_recovery: bool = False
    signature: str = ""
    recovery_at: float = 0.0
    origin_limiter: object | None = None
    origin_recovery_lock: asyncio.Lock | None = None
    origin_recovery: bool = False


class Router:
    def __init__(self, registry: Registry, gateway: GatewayConfig):
        self.registry = registry
        self.gateway = gateway

    def candidates(self, model: str, path: str | None = None) -> list[StationState]:
        """返回可路由候选站列表（已过滤不健康/不支持模型的站）。"""
        return self.registry.candidates_for(model, path)

    def pick(self, model: str, path: str | None = None, *, stream: bool | None = None,
             targets: dict[str, str] | None = None) -> list[StationState]:
        """Composite score bands first; never count the speed penalty twice."""
        if targets is None:
            cands = self.candidates(model, path)
        else:
            from .model_mappings import supports_mapping, mapping_path
            cands = [st for name, st in self.registry.states.items() if name in targets
                     and self.registry.is_routeable(st) and supports_mapping(st, targets[name], path)]
        if len(cands) <= 1:
            return cands

        evidence = {st.cfg.name: st.routing_evidence(
            targets[st.cfg.name] if targets is not None else model,
            mapping_path(st, targets[st.cfg.name], path) if targets is not None else path, stream) for st in cands}
        scores = {name: view['score'] if view['score'] is not None else 95.0
                  for name, view in evidence.items()}
        cands.sort(key=lambda st: scores[st.cfg.name], reverse=True)
        ordered: list[StationState] = []
        i = 0
        while i < len(cands):
            j = i
            while j < len(cands) and scores[cands[j].cfg.name] >= scores[cands[i].cfg.name] - SCORE_BAND:
                j += 1
            group = cands[i:j]
            if len(group) == 1:
                ordered.append(group[0])
            else:
                w = [st.cfg.weight * (1 + st.priority_bias / 15.0) for st in group]
                total = sum(w)
                ordered.append(_weighted_choice(group, [x / total for x in w]))
                ordered.extend(st for st in group if st not in ordered)
            i = j
        # One real request can validate an expired breaker, even with a low
        # score. No extra generation, and no concurrent recovery stampede.
        now = time.time()
        recovering = [st for st in ordered if (st.status == STATUS_DOWN or st.native_usage_limited)
                      and not st.recovery_inflight
                      and now >= st.recovery_attempt_at + max(1.0, self.gateway.cool_down)]
        if recovering:
            trial = min(recovering, key=lambda st: (st.recovery_attempt_at, st.down_since))
            ordered.remove(trial)
            ordered.insert(0, trial)
        return ordered

    def unavailable_reason(self, st: StationState, *, check_concurrency: bool = True) -> str | None:
        """Explain a local routing refusal without treating it as an upstream fault.

        Recheck after taking a semaphore: the candidate snapshot can become stale
        while credentials are refreshed or another request starts recovery.
        """
        now = time.time()
        if not st.enabled:
            return "站点已停用"
        if st.cfg.provider == "chatgpt" and self.registry.subscription_recovery_busy(st):
            return "账号正在恢复验证"
        recovery = st.status == STATUS_DOWN or st.native_usage_limited
        origin = self.registry.ensure_origin_limiter(st)
        if now < origin.cooldown_until:
            return "共享上游正在冷却"
        if origin.recovery_required and origin.recovery_lock.locked():
            return "共享上游正在恢复验证"
        if recovery and (st.recovery_inflight or st.generation_probe_lock.locked()):
            return "站点正在恢复验证"
        if not self.registry.is_routeable(st, now):
            if st.quota_exhausted:
                return "站点额度暂不可用"
            if now < st.rate_limit_until:
                return "站点正在限流冷却"
            if st.status == STATUS_DOWN:
                return "站点正在熔断冷却"
            return "站点暂不可路由"
        if recovery and origin.recovery_lock.locked():
            return "共享上游正在恢复验证"
        if recovery and now < st.recovery_attempt_at + max(1.0, self.gateway.cool_down):
            return "站点正在等待下一次恢复验证"
        if check_concurrency and origin.semaphore.locked():
            return "共享上游并发已满"
        if check_concurrency and self.registry.ensure_semaphore(st).locked():
            return "并发已满"
        return None

    async def acquire(self, st: StationState, timeout: float = 0.0) -> RoutePermit | None:
        """尝试获取站点并发额度；timeout=0 时立即失败不等待。"""
        if self.unavailable_reason(st, check_concurrency=False):
            return None
        sem = self.registry.ensure_semaphore(st)
        origin = self.registry.ensure_origin_limiter(st)
        origin_sem = origin.semaphore
        station_acquired = False
        origin_acquired = False
        try:
            # Check both before acquiring either one.  The normal zero-wait
            # path therefore never consumes a station slot for a full origin.
            if sem.locked() or origin_sem.locked():
                return None
            if timeout > 0:
                deadline = asyncio.get_running_loop().time() + timeout
                await asyncio.wait_for(origin_sem.acquire(), timeout)
                origin_acquired = True
                remaining = max(0.0, deadline - asyncio.get_running_loop().time())
                try:
                    await asyncio.wait_for(sem.acquire(), remaining)
                except asyncio.TimeoutError:
                    origin_sem.release()
                    origin_acquired = False
                    return None
                station_acquired = True
            else:
                await origin_sem.acquire()
                origin_acquired = True
                await sem.acquire()
                station_acquired = True
        except BaseException:
            if station_acquired:
                sem.release()
            if origin_acquired:
                origin_sem.release()
            raise
        if not station_acquired or not origin_acquired:
            if station_acquired:
                sem.release()
            if origin_acquired:
                origin_sem.release()
            return None
        now = time.time()
        native_recovery = st.native_usage_limited
        recovery = st.status == STATUS_DOWN or native_recovery
        origin_recovery = origin.recovery_required
        if self.unavailable_reason(st, check_concurrency=False):
            sem.release()
            origin_sem.release()
            return None
        recovery_lock = None
        if recovery or origin_recovery:
            # Only one channel may validate a shared origin after a breaker.
            if origin.recovery_lock.locked():
                sem.release()
                origin_sem.release()
                return None
            try:
                await origin.recovery_lock.acquire()
            except BaseException:
                sem.release()
                origin_sem.release()
                raise
            recovery_lock = origin.recovery_lock
            if recovery:
                st.recovery_inflight = True
                st.recovery_attempt_at = now
                if native_recovery:
                    st.begin_quota_recovery(now)
        origin.active += 1
        return RoutePermit(sem, recovery, quota_revision=st.quota_revision,
                           native_recovery=native_recovery, signature=st.connection_signature(), recovery_at=now,
                           origin_limiter=origin, origin_recovery_lock=recovery_lock,
                           origin_recovery=origin_recovery)

    def release(self, st: StationState, permit: RoutePermit, outcome: str = "cancelled",
                error: str = "request_aborted") -> None:
        if not permit.released:
            permit.released = True
            permit.semaphore.release()
            origin = permit.origin_limiter
            if origin is not None:
                origin.active = max(0, origin.active - 1)
                if outcome == "success":
                    self.registry.note_origin_success(st, limiter=origin)
                if permit.origin_recovery_lock is not None and permit.origin_recovery_lock.locked():
                    permit.origin_recovery_lock.release()
                origin.semaphore.release()
            if permit.recovery:
                st.recovery_inflight = False
            if permit.native_recovery:
                st.finish_quota_recovery(outcome, error, revision=permit.quota_revision,
                                         signature=permit.signature, attempt_at=permit.recovery_at)


def _weighted_choice(items: list, probs: list[float]):
    r = random.random()
    acc = 0.0
    for item, p in zip(items, probs):
        acc += p
        if r <= acc:
            return item
    return items[-1]
