"""Juice 巡检：单 token 分布指纹（仅对"使用中"的站点采集）。

规则：
- 只测 enabled + up + 非配额耗尽 + 最近 USE_WINDOW 秒内有成功假量的站点
- 每站每个"使用中的模型"按 juice_probe_interval 周期采集
- 同模型多站互比 JSD；采集中的样本量 < MIN_SAMPLES 不计入判定
"""
from __future__ import annotations

import asyncio
import json
import random
import time

import httpx

from .config import GatewayConfig
from .juice import JuiceRegistry, PROMPTS, parse_single_token
from .state import Registry, StationState

# 站点活跃判定窗口（秒）：最近有成功流量才算"使用中"
USE_WINDOW = 1800
# 每次巡检的探针数（8 格 × samples 个）
SAMPLES = 6


class JuiceMonitor:
    def __init__(self, registry: Registry, gateway: GatewayConfig,
                 juice: JuiceRegistry):
        self.registry = registry
        self.gateway = gateway
        self.juice = juice
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """主循环：每分钟 tick，检查各站指纹是否到期且处于使用中。"""
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001
                print(f"[juice] 巡检循环异常: {e}")
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        now = time.time()
        interval = self.gateway.juice_probe_interval
        for st in self.registry.states.values():
            if not st.enabled or st.quota_exhausted:
                continue
            if st.status != "up":
                continue
            # 采集该站支持的所有模型（effective_models）。
            # 放宽为"启用即在采"（每站每模型约 $0.001/轮）：同模型多站
            # 必须有完整指纹，JSD 互比才有对象，Juice 才有意义。
            models = set(st.effective_models)
            if not models:
                models = set(st.cfg.models or [])
            for m in models:
                fp = self.juice.get(st.cfg.name, m)
                if now - fp.updated_at < interval:
                    continue
                await self._probe(st, m, fp)

    async def _probe(self, st: StationState, model: str, fp) -> None:
        """对 (站, 模型) 采集 SAMPLES 个单 token 样本。"""
        client = self.registry.ensure_client(st)
        headers = {
            "Authorization": f"Bearer {st.cfg.api_key}",
            "Content-Type": "application/json",
            **st.cfg.headers,
        }
        prompts = random.sample(PROMPTS, min(len(PROMPTS), SAMPLES * 2))
        prompts = prompts[:SAMPLES]
        for prompt in prompts:
            if st.cfg.probe_chat_path == "/responses":
                url = st.cfg.api_base + "/responses"
                body = {"model": model, "input": prompt,
                        "max_output_tokens": 16, "temperature": 1.0,
                        "store": False}
            else:
                url = st.cfg.api_base + "/chat/completions"
                body = {"model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 16, "temperature": 1.0}
            try:
                resp = await client.post(
                    url, json=body, headers=headers,
                    timeout=httpx.Timeout(connect=10, read=30, write=10, pool=10))
            except (httpx.TimeoutException, httpx.TransportError):
                continue
            if resp.status_code >= 400:
                continue
            text = self._extract_text(resp.content)
            fp.add(parse_single_token(text))
        fp.updated_at = time.time()
        self.juice.refresh_flags()
        print(f"[juice] {st.cfg.name}/{model} 指纹样本 {fp.samples}；"
              f"JSD={fp.jsd} flag={fp.flag or ''}")

    @staticmethod
    def _extract_text(content: bytes) -> str:
        try:
            obj = json.loads(content.decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return ""
        if not isinstance(obj, dict):
            return ""
        # chat / responses 兼容提取
        for choice in obj.get("choices", []) or []:
            if isinstance(choice, dict):
                msg = choice.get("message")
                if isinstance(msg, dict) and msg.get("content"):
                    return str(msg["content"])
                delta = choice.get("delta")
                if isinstance(delta, dict) and delta.get("content"):
                    return str(delta["content"])
        for item in obj.get("output", []) or []:
            if isinstance(item, dict) and item.get("content"):
                for c in item["content"]:
                    if isinstance(c, dict) and c.get("text"):
                        return str(c["text"])
        return ""

    def _last_used(self, st: StationState, model: str) -> float:
        """(站, 模型) 最近成功使用时间：优先专用字段，回退站级。"""
        return getattr(st, "last_used_at", 0.0) or 0.0

    async def stop(self) -> None:
        self._stop.set()
