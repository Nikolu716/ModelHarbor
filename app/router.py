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

    async def acquire(self, st: StationState, timeout: float = 0.0) -> RoutePermit | None:
        """尝试获取站点并发额度；timeout=0 时立即失败不等待。"""
        sem = self.registry.ensure_semaphore(st)
        if timeout > 0:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=timeout)
            except asyncio.TimeoutError:
                return None
        else:
            if sem.locked():
                return None
            await sem.acquire()
        now = time.time()
        native_recovery = st.native_usage_limited
        recovery = st.status == STATUS_DOWN or native_recovery
        if not self.registry.is_routeable(st, now) or (recovery and (
                st.recovery_inflight or st.generation_probe_lock.locked()
                or now < st.recovery_attempt_at + max(1.0, self.gateway.cool_down))):
            sem.release()
            return None
        if recovery:
            st.recovery_inflight = True
            st.recovery_attempt_at = now
            if native_recovery:
                st.quota_resume_at = max(st.quota_resume_at, now + st.native_retry_interval())
                st.quota_wait_source = "gateway"
                st._persist_state()
        return RoutePermit(sem, recovery, quota_revision=st.quota_revision)

    def release(self, st: StationState, permit: RoutePermit) -> None:
        if not permit.released:
            permit.released = True
            permit.semaphore.release()
            if permit.recovery:
                st.recovery_inflight = False


def _weighted_choice(items: list, probs: list[float]):
    r = random.random()
    acc = 0.0
    for item, p in zip(items, probs):
        acc += p
        if r <= acc:
            return item
    return items[-1]
