"""健康探测与熔断。

后台循环周期探测每个中转站：
- /models 接口（免费，默认）：可用性 + 延迟 EMA
- 真实生成链路（可选 probe_chat）：极小的 chat 请求验证"能真正出活"，
  /models 通不代表生成链路通（部分中转站 /models 正常但生成 503）
失败累计：连续失败达到 fail_threshold 熔断为 DOWN 并进入冷却；
冷却结束自动重新探测，成功即恢复 UP（全自动托管）。
"""
from __future__ import annotations

import asyncio
import json
import time

import httpx

from .config import GatewayConfig
from .errors import UpstreamError, classify_error
from .notifier import Notifier
from .state import STATUS_DOWN, Registry, StationState

# 真实链路探测的最小间隔（秒）：/models 探测之外额外限制
PROBE_CHAT_MIN_INTERVAL = 60

# 余额探测路径（OpenAI/one-api/new-api 兼容）
BALANCE_PATH = "/dashboard/billing/credit_grants"


class HealthMonitor:
    def __init__(self, registry: Registry, gateway: GatewayConfig,
                 notifier: Notifier | None = None):
        self.registry = registry
        self.gateway = gateway
        self.notifier = notifier or Notifier()
        self._prev_status: dict[str, str] = {}
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """主循环：按各站探测间隔循环探测。"""
        while not self._stop.is_set():
            try:
                await self._probe_all()
            except asyncio.CancelledError:
                break
            except Exception as e:  # 单个探测失败不影响整体循环
                print(f"[health] 探测循环异常: {e}")
            # 每轮之间等待最小间隔，整体以最短探测间隔驱动
            try:
                await asyncio.sleep(self._tick())
            except asyncio.CancelledError:
                break
            except Exception as e:  # 间隔计算异常也不允许杀死循环
                print(f"[health] 间隔计算异常: {e}")
                await asyncio.sleep(5.0)

    def _tick(self) -> float:
        """下一轮最快需要等待的秒数。"""
        intervals = [s.cfg.probe_interval for s in self.registry.states.values()
                     if s.cfg.probe_interval]
        return min(intervals) if intervals else self.gateway.probe_interval

    async def _probe_all(self) -> None:
        now = time.time()
        tasks = []
        for st in self.registry.states.values():
            if not st.enabled:
                continue
            interval = st.cfg.probe_interval or self.gateway.probe_interval
            cooling = (st.status == STATUS_DOWN
                       and now - st.down_since < self.gateway.cool_down)
            due = now - st.last_probe_at >= interval
            quota_due = self._quota_recovery_due(st, now)
            if due and not cooling and not (
                    quota_due and (st.quota_source or "request") == "models"):
                tasks.append(self.probe(st))
                # 真实生成链路探测（可选，至少 60s 一次；配额耗尽时改走恢复探测）
                if (st.cfg.probe_chat and not st.quota_exhausted
                        and now - st.probe_chat_at >= PROBE_CHAT_MIN_INTERVAL):
                    tasks.append(self._probe_chat(st))
                # 余额探测（默认开，GET 免费；不支持则静默降级为 None）
                if (st.cfg.probe_balance
                        and now - st.balance_at >= self.gateway.balance_probe_interval):
                    tasks.append(self._probe_balance(st))
            if quota_due:
                tasks.append(self.probe_quota_recovery(st))
        if tasks:
            await asyncio.gather(*tasks)
        await self._notify_changes()

    async def _probe_balance(self, st: StationState) -> None:
        """探测站点余额（credit_grants 接口）。

        OpenAI/one-api/new-api 兼容格式：{"total_available": ...}
        站点不支持（404/401/超时）时静默降级 balance=None，不影响其他功能。
        """
        client = self.registry.ensure_client(st)
        url = st.cfg.api_base + BALANCE_PATH
        headers = {"Authorization": f"Bearer {st.cfg.api_key}", **st.cfg.headers}
        st.balance_at = time.time()
        try:
            resp = await client.get(url, headers=headers,
                                    timeout=httpx.Timeout(
                                        connect=self.gateway.connect_timeout,
                                        read=10.0, write=10.0, pool=10.0))
            if resp.status_code < 400:
                try:
                    data = json.loads(resp.content.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    return
                available = (data.get("total_available")
                             if isinstance(data, dict) else None)
                if isinstance(available, (int, float)):
                    st.balance = float(available)
                    # 配额耗尽后自动恢复：余额探测到有钱（权威信号）即重新启用，
                    # 无需人工点重置（不支持余额接口的站仍保持人工 reset 模式）
                    if st.quota_exhausted and st.balance > 0:
                        st.reset_quota()
                        await self.notifier.send(
                            "station_quota_restored", st.cfg.name,
                            f"探测到余额 ${st.balance:.2f}，已自动恢复路由")
                        print(f"[health] {st.cfg.name} 余额恢复 ${st.balance:.2f}，自动启用")
                    threshold = self.gateway.balance_alert_threshold
                    if st.balance < threshold:
                        await self.notifier.send(
                            "balance_low", st.cfg.name,
                            f"余额不足预警: ${st.balance:.2f}（低于 ${threshold:.2f}，请及时充值）")
            # 非 2xx（404 等）：不支持，保持 balance=None 静默降级
        except (httpx.TimeoutException, httpx.TransportError):
            pass  # 网络异常静默降级
        except Exception as e:  # noqa: BLE001
            print(f"[health] {st.cfg.name} 余额探测异常: {e}")

    async def _notify_changes(self) -> None:
        """每轮探测后统一刷新评分，并检测状态变化推送 webhook（挂机托管）。"""
        for st in self.registry.states.values():
            st._update_score()  # 探测结果（熔断/恢复/配额）实时反映到评分
            cur = ("quota" if st.quota_exhausted
                   else ("down" if st.status == STATUS_DOWN else "up"))
            prev = self._prev_status.get(st.cfg.name)
            if prev is not None and prev != cur:
                if cur == "down":
                    await self.notifier.send(
                        "station_down", st.cfg.name,
                        f"连续失败 {st.consecutive_failures} 次熔断: {st.last_error[:120]}")
                elif cur == "up" and prev == "down":
                    await self.notifier.send(
                        "station_up", st.cfg.name,
                        f"探测恢复，已重新加入路由（延迟 {st.ema_latency and round(st.ema_latency*1000, 1)}ms）")
                elif cur == "quota":
                    await self.notifier.send(
                        "station_quota", st.cfg.name,
                        f"配额问题: {st.quota_error[:120]}（将自动探测直到额度恢复）")
                elif cur == "up" and prev == "quota":
                    await self.notifier.send(
                        "station_quota_recovered", st.cfg.name,
                        "自动探测确认额度已恢复，已重新加入路由")
            self._prev_status[st.cfg.name] = cur

    def _quota_recovery_due(self, st: StationState, now: float) -> bool:
        if not self.gateway.quota_auto_recover:
            return False
        if not st.enabled or not st.quota_exhausted:
            return False
        interval = st.quota_probe_backoff or self.gateway.quota_probe_interval
        last = st.quota_probe_at or st.quota_at
        return now - last >= interval

    def _advance_quota_backoff(self, st: StationState) -> None:
        base = self.gateway.quota_probe_interval
        cap = self.gateway.quota_probe_max_interval
        current = st.quota_probe_backoff or base
        st.quota_probe_backoff = min(cap, current * 2)

    async def probe_quota_recovery(self, st: StationState) -> bool:
        """探测配额耗尽站点是否已恢复额度；仍无额度则继续退避探测。

        /models 成功不代表有额度。按首次耗尽来源对症探测：
        - models：再打 /models，不再报配额即恢复
        - chat/request：打 1 token 生成探测，成功或非配额 4xx 即恢复
        """
        st.quota_probe_at = time.time()
        source = st.quota_source or "request"
        recovered = False
        detail = ""

        if source == "models":
            await self.probe(st)
            if st.last_probe_ok:
                recovered = True
                detail = "模型探测已不再报配额"
            elif st.last_probe_quota:
                detail = st.last_probe_error or "模型探测仍无额度"
            else:
                detail = st.last_probe_error or "模型探测失败"
        else:
            status, latency, err = await self._generation_probe(st)
            if status == "ok":
                recovered = True
                detail = "生成探测已恢复"
                if latency is not None:
                    st.record_probe(latency)
            elif status == "client_error":
                recovered = True
                detail = f"生成探测不再报配额: {err.message if err else ''}"
            elif status == "quota":
                detail = (err.message if err else "") or "生成探测仍无额度"
            elif status == "skip":
                if (st.cfg.billing == "balance" and st.cfg.probe_balance):
                    await self._probe_balance(st)
                    if st.balance is not None and st.balance > 0:
                        recovered = True
                        detail = f"余额已恢复 ${st.balance:.2f}"
                    elif st.balance is not None:
                        detail = f"余额仍不足 ${st.balance:.2f}"
                    else:
                        detail = "无可用模型且余额不可查，无法确认额度"
                else:
                    detail = "无可用模型，无法确认额度"
            else:
                detail = (err.message if err else "") or "生成探测失败"

        if recovered:
            st.reset_quota()
            st.quota_probe_error = ""
            print(f"[health] {st.cfg.name} 额度探测恢复: {detail}")
            return True
        st.quota_probe_error = detail
        self._advance_quota_backoff(st)
        print(f"[health] {st.cfg.name} 额度仍不可用: {detail[:120]}，"
              f"{st.quota_probe_backoff:.0f}s 后再探")
        return False

    async def _generation_probe(
            self, st: StationState,
    ) -> tuple[str, float | None, UpstreamError | None]:
        """极小生成请求。返回 (ok|quota|client_error|server_error|skip, latency, err)。"""
        models = st.effective_models
        if not models:
            return "skip", None, None
        st.probe_chat_at = time.time()
        client = self.registry.ensure_client(st)
        # 优先用配置声明的首个模型（真实用户模型），避免字母序选到冷门模型
        model = (st.cfg.models[0] if st.cfg.models else sorted(models)[0])
        if st.cfg.probe_chat_path == "/responses":
            url = st.cfg.api_base + "/responses"
            body = {"model": model, "input": "ping", "max_output_tokens": 1,
                    "store": False}
        else:
            url = st.cfg.api_base + "/chat/completions"
            body = {"model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1}
        headers = {"Authorization": f"Bearer {st.cfg.api_key}",
                   "Content-Type": "application/json", **st.cfg.headers}
        t0 = time.perf_counter()
        try:
            resp = await client.post(url, json=body, headers=headers,
                                     timeout=httpx.Timeout(
                                         connect=self.gateway.connect_timeout,
                                         read=30.0, write=10.0, pool=10.0))
            latency = time.perf_counter() - t0
            if resp.status_code < 400:
                st.last_probe_ok = True
                st.last_probe_latency = latency
                st.last_probe_error = ""
                st.last_probe_quota = False
                return "ok", latency, None
            err = classify_error(st.cfg.name, resp.status_code, resp.content)
            st.last_probe_ok = False
            st.last_probe_error = str(err)
            st.last_probe_quota = bool(err.quota)
            if err.quota:
                return "quota", latency, err
            if resp.status_code >= 500:
                return "server_error", latency, err
            return "client_error", latency, err
        except (httpx.TimeoutException, httpx.TransportError) as e:
            err = UpstreamError(
                st.cfg.name, 0, f"真实链路探测失败: {type(e).__name__}")
            st.last_probe_ok = False
            st.last_probe_error = str(err)
            st.last_probe_quota = False
            return "server_error", None, err
        except Exception as e:  # noqa: BLE001
            err = UpstreamError(st.cfg.name, 0, f"真实链路探测异常: {e}")
            st.last_probe_ok = False
            st.last_probe_error = str(err)
            st.last_probe_quota = False
            return "server_error", None, err

    async def _probe_chat(self, st: StationState) -> None:
        """真实生成链路探测：极小请求验证能真正出活（需站声明模型）。

        端点按 probe_chat_path 区分：
        - /responses       → responses 格式 body（responses-only 站，如 codexzh）
        - 其他（默认 chat/completions）→ chat 格式 body
        4xx 为配置类错误（协议/模型不匹配），只记录不熔断站点；
        5xx/超时/连接失败才累计熔断。
        """
        status, latency, err = await self._generation_probe(st)
        if status == "skip":
            return
        if status == "ok":
            if latency is not None:
                st.record_probe(latency)
            return
        if status == "quota" and err is not None:
            st.mark_quota_exhausted(err, source="chat")
            await self.notifier.send(
                "station_quota", st.cfg.name,
                f"探测发现配额问题: {err.message[:120]}（将自动探测直到额度恢复）")
            return
        if status == "server_error":
            self._mark_failure(st)

    async def probe(self, st: StationState) -> None:
        """立即探测单个站点（供后台循环与手动触发共用）。"""
        client = self.registry.ensure_client(st)
        sem = self.registry.ensure_semaphore(st)
        if sem.locked():
            return  # 并发已满，跳过探测
        path = st.cfg.probe_path if st.cfg.probe_path.startswith("/") else "/" + st.cfg.probe_path
        url = st.cfg.api_base + path
        headers = {"Authorization": f"Bearer {st.cfg.api_key}", **st.cfg.headers}
        t0 = time.perf_counter()
        st.last_probe_at = time.time()
        try:
            resp = await client.get(
                url,
                headers=headers,
                timeout=httpx.Timeout(connect=self.gateway.connect_timeout,
                                      read=10.0, write=10.0, pool=10.0),
            )
            latency = time.perf_counter() - t0
            if resp.status_code < 400:
                st.last_probe_ok = True
                st.last_probe_latency = latency
                st.last_probe_error = ""
                st.last_probe_quota = False
                st.consecutive_failures = 0
                st._record_history(True)  # 探测成功 → 热力图绿格
                if st.status == STATUS_DOWN:
                    st.status = "up"
                    st.down_since = 0.0
                    st._update_score()  # 恢复立即反映到评分（排前）
                # 合并探测到的模型列表
                if st.cfg.probe_models:
                    self._merge_probed_models(st, resp.content)
            else:
                st.last_probe_ok = False
                st.last_probe_latency = None
                err = classify_error(st.cfg.name, resp.status_code, resp.content)
                st.last_probe_error = str(err)
                st.last_probe_quota = bool(err.quota)
                if err.quota:
                    # 余额不足/订阅到期：标记配额耗尽；/models 成功不会自动清除
                    st.mark_quota_exhausted(err, source="models")
                    await self.notifier.send(
                        "station_quota", st.cfg.name,
                        f"探测发现配额问题: {err.message[:120]}（将自动探测直到额度恢复）")
                self._mark_failure(st)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            st.last_probe_ok = False
            st.last_probe_latency = None
            st.last_probe_error = f"探测失败: {type(e).__name__}"
            st.last_probe_quota = False
            self._mark_failure(st)
        except Exception as e:  # noqa: BLE001 保证探测健壮
            st.last_probe_ok = False
            st.last_probe_latency = None
            st.last_probe_error = f"探测异常: {e}"
            st.last_probe_quota = False
            self._mark_failure(st)

    def _mark_failure(self, st: StationState) -> None:
        st.consecutive_failures += 1
        st._record_history(False)  # 探测失败 → 热力图红格
        if (
            st.consecutive_failures >= self.gateway.fail_threshold
            and st.status != STATUS_DOWN
        ):
            st.status = STATUS_DOWN
            st.down_since = time.time()
            st._update_score()  # 熔断立即反映到评分（排后）
            print(f"[health] {st.cfg.name} 连续失败 {st.consecutive_failures} 次，"
                  f"熔断 {self.gateway.cool_down}s")

    @staticmethod
    def _merge_probed_models(st: StationState, content: bytes) -> None:
        """解析 /models 响应并合并模型名。"""
        import json

        try:
            data = json.loads(content.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                st.probed_models.add(str(item["id"]))
            elif isinstance(item, str):
                st.probed_models.add(item)

    async def stop(self) -> None:
        self._stop.set()
