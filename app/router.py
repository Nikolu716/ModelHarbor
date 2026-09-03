"""路由与故障转移。

根据模型名选出候选站（启用 + 健康 + 支持该模型），按**动态健康评分**
排序（延迟 EMA + 滑动窗口成功率 + 状态惩罚，自动托管无需手工配置），
评分相近的站做加权随机避免频繁抖动。
"""
from __future__ import annotations

import asyncio
import random

from .config import GatewayConfig
from .state import Registry, StationState

# 评分相差不超过该值时视为"同等健康"，组内加权随机
SCORE_BAND = 5.0


class Router:
    def __init__(self, registry: Registry, gateway: GatewayConfig):
        self.registry = registry
        self.gateway = gateway

    def candidates(self, model: str) -> list[StationState]:
        """返回可路由候选站列表（已过滤不健康/不支持模型的站）。"""
        return self.registry.candidates_for(model)

    def pick(self, model: str) -> list[StationState]:
        """按动态健康评分排序，评分相近的站按权重+延迟加权随机。

        无需手工配置优先级：系统根据实时健康自动排列（托管模式）。
        """
        cands = self.candidates(model)
        if len(cands) <= 1:
            return cands

        cands.sort(key=lambda st: st.health_score, reverse=True)
        ordered: list[StationState] = []
        i = 0
        while i < len(cands):
            j = i
            while j < len(cands) and cands[j].health_score >= cands[i].health_score - SCORE_BAND:
                j += 1
            group = cands[i:j]
            if len(group) == 1:
                ordered.append(group[0])
            else:
                # 有效权重 = 配置权重 / (1 + 延迟秒数)：同健康度下更快、更重的站靠前
                lat = {st.cfg.name: (st.ema_latency or 999.0) for st in group}
                w = [st.cfg.weight / (1.0 + lat[st.cfg.name]) for st in group]
                total = sum(w)
                ordered.append(_weighted_choice(group, [x / total for x in w]))
                ordered.extend(st for st in group if st not in ordered)
            i = j
        return ordered

    async def acquire(self, st: StationState, timeout: float = 0.0) -> bool:
        """尝试获取站点并发额度；timeout=0 时立即失败不等待。"""
        sem = self.registry.ensure_semaphore(st)
        if timeout > 0:
            try:
                await asyncio.wait_for(sem.acquire(), timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        if sem.locked():
            return False
        await sem.acquire()
        return True

    def release(self, st: StationState) -> None:
        if st.semaphore is not None:
            st.semaphore.release()


def _weighted_choice(items: list, probs: list[float]):
    r = random.random()
    acc = 0.0
    for item, p in zip(items, probs):
        acc += p
        if r <= acc:
            return item
    return items[-1]
