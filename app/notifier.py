"""挂机告警：站点状态变化通过 Webhook 推送（默认关闭）。

配置 gateway.webhook_url 后启用，事件：
- station_down      站点熔断
- station_up        站点恢复
- station_quota     配额耗尽（余额不足/订阅到期）
- station_quota_recovered  自动探测确认额度恢复
- station_reset     管理员立即探测后确认恢复
- station_rate_limited  429 限流进入冷却

同类事件 5 分钟去重，避免刷屏。推送失败静默，不影响网关运行。
"""
from __future__ import annotations

import time

import httpx


class Notifier:
    def __init__(self, webhook_url: str = ""):
        self.url = webhook_url
        self._client: httpx.AsyncClient | None = None
        self._last_sent: dict[str, float] = {}
        self._dedupe_seconds = 300.0

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def send(self, event: str, station: str, message: str) -> None:
        if not self.url:
            return
        key = f"{event}:{station}"
        now = time.time()
        if now - self._last_sent.get(key, 0.0) < self._dedupe_seconds:
            return
        self._last_sent[key] = now
        payload = {
            "event": event, "station": station, "message": message,
            "ts": now, "source": "zgateway",
        }
        try:
            if self._client is None:
                self._client = httpx.AsyncClient(timeout=httpx.Timeout(
                    connect=5.0, read=5.0, write=5.0, pool=5.0))
            r = await self._client.post(self.url, json=payload)
            if r.status_code >= 400:
                print(f"[notify] webhook 返回 {r.status_code}")
        except Exception as e:  # noqa: BLE001 通知失败不影响网关
            print(f"[notify] 发送失败: {type(e).__name__}")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
