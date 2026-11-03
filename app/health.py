"""健康探测与熔断。

后台循环周期探测每个中转站：
- /models 元数据：缓存模型目录，不作为生成成功或余额可用的证明
- 真实生成链路（可选 probe_chat）：极小的 chat 请求验证"能真正出活"，
  /models 通不代表生成链路通（部分中转站 /models 正常但生成 503）
失败累计：连续失败达到 fail_threshold 熔断为 DOWN 并进入冷却；
冷却结束允许业务请求验证恢复；后台生成检查需显式开启，
真实请求或生成测试成功恢复 UP，目录可达不代表生成已恢复。
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from .config import GatewayConfig
from .errors import UpstreamError, classify_error
from .notifier import Notifier
from .providers import opencode_capabilities, inference_headers
from .protocols import inference_url
from .go_usage import URL as GO_USAGE_URL, AUTO_INTERVAL as GO_USAGE_INTERVAL, MANUAL_INTERVAL as GO_USAGE_MANUAL_INTERVAL, MAX_RESPONSE_BYTES as GO_USAGE_MAX_BYTES, normalize_usage
from .state import STATUS_DOWN, Registry, StationState

# 真实生成探测的间隔由管理员按全局设置控制。
MODEL_REFRESH_INTERVAL = 900.0
MODEL_REFRESH_MIN_INTERVAL = 300.0
MODEL_MANUAL_MIN_INTERVAL = 60.0
MODEL_STOP_STATUSES = {"auth_error", "unsupported", "invalid_response"}

# 余额探测路径（OpenAI/one-api/new-api 兼容）
BALANCE_PATH = "/dashboard/billing/credit_grants"


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        seconds = float(value)
        return seconds if math.isfinite(seconds) and seconds > 0 else 0.0
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            return max(
                0.0,
                date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() - time.time(),
            )
        except (TypeError, ValueError, OverflowError):
            return 0.0


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
        intervals = [self._station_probe_interval(s)
                     for s in self.registry.states.values() if s.enabled]
        if any(s.enabled and s.cfg.provider == "opencode" and s.cfg.opencode_plan == "go" for s in self.registry.states.values()):
            intervals.append(GO_USAGE_INTERVAL)
        return min(intervals) if intervals else max(
            MODEL_REFRESH_MIN_INTERVAL, self.gateway.probe_interval)

    def _station_probe_interval(self, st: StationState) -> float:
        return max(
            MODEL_REFRESH_MIN_INTERVAL,
            st.cfg.probe_interval or self.gateway.probe_interval,
        )

    async def _probe_all(self) -> None:
        now = time.time()
        tasks = []
        for st in self.registry.states.values():
            if not st.enabled:
                continue
            if st.cfg.provider == "opencode" and st.cfg.opencode_plan == "go" and now >= st.go_usage_next_at:
                tasks.append(self.refresh_go_usage(st))
            if now < st.rate_limit_until:
                continue
            interval = self._station_probe_interval(st)
            cooling = (st.status == STATUS_DOWN
                       and now - st.down_since < self.gateway.cool_down)
            due = now - st.last_probe_at >= interval
            quota_due = self._quota_recovery_due(st, now)
            if quota_due:
                tasks.append(self.probe_quota_recovery(st))
                continue

            generation_probe_due = (
                not cooling
                and self.gateway.generation_probe_enabled
                and st.cfg.probe_chat
                and bool(st.selected_probe_model)
                and not st.quota_exhausted
                and now - st.probe_chat_at >= self.gateway.generation_probe_interval
                and now - st.last_used_at >= self.gateway.generation_probe_interval
            )
            if generation_probe_due:
                # Keep token-generating traffic out of the same scheduler burst as metadata checks.
                tasks.append(self._probe_chat(st))
                continue

            if (st.cfg.probe_models and st.cfg.probe_path.strip("/") != "models"
                    and now >= st.models_next_refresh_at):
                tasks.append(self.discover_models(st))
            if due and not cooling:
                tasks.append(self.probe(st))
                # 余额探测（默认开，GET 免费；不支持则静默降级为 None）
                if (st.cfg.probe_balance
                        and now - st.balance_at >= self.gateway.balance_probe_interval):
                    tasks.append(self._probe_balance(st))
        if tasks:
            await asyncio.gather(*tasks)
        await self._notify_changes()

    async def refresh_go_usage(self, st: StationState, *, manual=False):
        """Fetch quota metadata with one request, caching, and independent backoff."""
        if not st.enabled or st.cfg.provider != "opencode" or st.cfg.opencode_plan != "go":
            return st.go_usage_view()
        async with st.go_usage_lock:
            now = time.time()
            due = max(st.go_usage_checked_at + GO_USAGE_MANUAL_INTERVAL, st.go_usage_retry_at) if manual else st.go_usage_next_at
            if now < due:
                return st.go_usage_view()
            cfg = st.cfg
            status, error, windows = "error", "", None
            next_at = now + GO_USAGE_INTERVAL
            retry_at = 0.0
            try:
                client = self.registry.ensure_client(st)
                # Fixed origin and no configured client/session headers on the quota endpoint.
                async with client.stream("GET", GO_USAGE_URL, headers={
                        "Authorization": "Bearer " + cfg.api_key, "Accept": "application/json",
                        "User-Agent": "ModelHarbor/1.0"}, follow_redirects=False,
                        timeout=httpx.Timeout(connect=self.gateway.connect_timeout, read=10, write=10, pool=5)) as response:
                    if response.status_code == 429:
                        status, error = "rate_limited", "上游用量接口限流，保留上次数据"
                        retry_at = now + max(300, _retry_after_seconds(response.headers.get("retry-after")))
                        next_at = max(next_at, retry_at)
                    elif response.status_code in (401, 403):
                        status, error = "auth_error", "当前密钥无权读取 Go 用量"
                        next_at = now + 86400
                    elif response.status_code == 404:
                        status, error = "unsupported", "上游暂不提供 Go 用量接口"
                        next_at = now + 86400
                    elif response.status_code != 200:
                        error = f"上游用量接口 HTTP {response.status_code}，保留上次数据"
                    else:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > GO_USAGE_MAX_BYTES:
                                raise ValueError("用量响应超过大小限制")
                        windows = normalize_usage(json.loads(raw))
                        status = "ready"
            except (httpx.HTTPError, ValueError, TypeError) as e:
                if isinstance(e, httpx.PoolTimeout):
                    self.registry.reset_client(st, expected_client=client)
                error = "暂时无法读取上游用量，保留上次数据"
            if self.registry.get(cfg.name) is not st or st.cfg is not cfg:
                return st.go_usage_view()
            st.go_usage_checked_at = now
            st.go_usage_status, st.go_usage_error = status, error
            st.go_usage_retry_at = retry_at
            st.go_usage_failures = 0 if status == "ready" else st.go_usage_failures + 1
            if status == "error":
                next_at = now + min(14400, GO_USAGE_INTERVAL * (2 ** min(st.go_usage_failures, 4)))
            st.go_usage_next_at = next_at
            if windows is not None:
                st.go_usage, st.go_usage_updated_at = windows, now
            st._persist_state()
            return st.go_usage_view()

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
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if isinstance(e, httpx.PoolTimeout):
                self.registry.reset_client(st, expected_client=client)
            pass  # 网络异常静默降级
        except Exception as e:  # noqa: BLE001
            print(f"[health] {st.cfg.name} 余额探测异常: {e}")

    async def _notify_changes(self) -> None:
        """每轮探测后统一刷新评分，并检测状态变化推送 webhook（挂机托管）。"""
        for st in self.registry.states.values():
            st._update_score()  # 按样本真实时间衰减，探测结果不制造业务评分样本
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
        source = st.quota_source or "request"
        if source != "models":
            if not self.gateway.generation_probe_enabled or not st.cfg.probe_chat:
                return False
            interval = max(
                self.gateway.generation_probe_interval,
                st.quota_probe_backoff or self.gateway.quota_probe_interval,
                self._station_probe_interval(st),
            )
        else:
            interval = max(
                self._station_probe_interval(st),
                st.quota_probe_backoff or self.gateway.quota_probe_interval,
            )
        last = st.quota_probe_at or st.quota_at
        return now - last >= interval

    def _advance_quota_backoff(self, st: StationState) -> None:
        base = max(self.gateway.quota_probe_interval,
                   self._station_probe_interval(st))
        cap = max(self.gateway.quota_probe_max_interval, base)
        current = st.quota_probe_backoff or base
        st.quota_probe_backoff = min(cap, current * 2)

    async def probe_quota_recovery(self, st: StationState, *, manual: bool = False) -> bool:
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
            if manual:
                status, latency, err = await self._generation_probe(st)
                if status == "ok":
                    recovered = True
                    detail = "管理员手动生成探测已成功"
                    if latency is not None:
                        st.record_probe(latency)
                else:
                    detail = ((err.message if err else "") or
                              "手动生成探测未能确认额度恢复")
                if recovered:
                    st.reset_quota()
                    st.quota_probe_error = ""
                    print(f"[health] {st.cfg.name} 额度探测恢复: {detail}")
                    return True
                st.quota_probe_error = detail
                self._advance_quota_backoff(st)
                return False
            previous_check = st.models_checked_at if st.cfg.probe_models else st.last_probe_at
            await self.probe(st)
            checked = st.models_checked_at if st.cfg.probe_models else st.last_probe_at
            if checked == previous_check:
                if st.cfg.billing == "balance" and st.cfg.probe_balance:
                    await self._probe_balance(st)
                    if st.balance is not None and st.balance > 0:
                        recovered = True
                        detail = f"模型目录仍在缓存，余额已恢复至 ${st.balance:.2f}"
                    elif st.balance is not None:
                        detail = f"模型目录仍在缓存，余额仍不足 ${st.balance:.2f}"
                    else:
                        detail = "模型目录仍在缓存，且余额接口不可用；未发送生成检查"
                else:
                    detail = "模型接口尚在缓存或冷却期，未发送恢复检查"
            elif st.last_probe_quota:
                detail = st.last_probe_error or "模型探测仍无额度"
            elif st.last_probe_ok and st.cfg.probe_balance and st.cfg.billing == "balance":
                await self._probe_balance(st)
                if st.balance is not None and st.balance > 0:
                    recovered = True
                    detail = f"模型目录已恢复且余额为 ${st.balance:.2f}"
                elif st.balance is not None:
                    detail = f"目录可达但余额仍不足 ${st.balance:.2f}"
                else:
                    detail = "目录可达但余额接口不可用，不能据此确认额度已恢复"
            elif st.last_probe_ok:
                detail = "模型目录恢复不证明生成额度可用，等待真实成功调用或管理员确认"
            else:
                detail = st.last_probe_error or "模型探测失败"
        else:
            if (not manual and
                    (not self.gateway.generation_probe_enabled or not st.cfg.probe_chat)):
                detail = "已关闭生成探测，保留额度保护；等待业务成功或手动确认"
                st.quota_probe_error = detail
                self._advance_quota_backoff(st)
                return False
            status, latency, err = await self._generation_probe(st)
            if status == "ok":
                recovered = True
                detail = "生成探测已恢复"
                if latency is not None:
                    st.record_probe(latency)
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
                        detail = (err.message if err else "无可用探测模型且余额不可查，无法确认额度")
                else:
                    detail = (err.message if err else "无可用探测模型，无法确认额度")
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
        """One generation at a time; manual checks also respect upstream backoff."""
        now = time.time()
        reason = ""
        if st.cfg.provider == "opencode" and st.cfg.opencode_plan == "go":
            reason = "Go 套餐用于编程任务，不发送通用 ping 生成探测；可手动发起智商测试或正常编程请求"
        elif not st.enabled or self._stop.is_set():
            reason = "站点已停用，未发送生成请求"
        elif now < st.rate_limit_until:
            reason = f"上游限流冷却中，{math.ceil(st.rate_limit_until - now)} 秒后可测试"
        elif st.status == STATUS_DOWN and now < st.down_since + self.gateway.cool_down:
            reason = f"网关熔断保护中，{math.ceil(st.down_since + self.gateway.cool_down - now)} 秒后可测试"
        elif st.recovery_inflight:
            reason = "正在用业务请求验证恢复，请稍后再试"
        elif st.generation_probe_lock.locked() or now - st.probe_chat_at < 60:
            reason = "生成测试正在执行或距上次不足 60 秒，请稍后再试"
        if reason:
            return "skip", None, UpstreamError(st.cfg.name, 0, reason)
        sem = self.registry.ensure_semaphore(st)
        if sem.locked():
            return "skip", None, UpstreamError(st.cfg.name, 0, "站点并发已满，未发送生成请求")
        async with st.generation_probe_lock, sem:
            result = await self._send_generation_probe(st)
            status, _, err = result
            st.generation_probe_ok = None if status == "skip" else status == "ok"
            st.generation_probe_error = err.message if err else ""
            return result

    async def _send_generation_probe(
            self, st: StationState,
    ) -> tuple[str, float | None, UpstreamError | None]:
        """极小生成请求。返回 (ok|quota|client_error|server_error|skip, latency, err)。"""
        configured_probe_model = st.selected_probe_model
        if not configured_probe_model:
            err = UpstreamError(
                st.cfg.name, 0,
                "暂无可用探测模型，请先读取模型列表或手动补充模型；未发送生成请求",
            )
            return "skip", None, err
        client = self.registry.ensure_client(st)
        probe_config = st.cfg
        # A manual choice never silently falls back when it disappears from the catalog.
        if (configured_probe_model and st.cfg.probe_models
                and st.models_discovery_status == "ready"
                and configured_probe_model not in st.probed_models
                and configured_probe_model not in st.cfg.models):
            st.last_probe_ok = None
            st.last_probe_error = (
                f"已配置的探测模型 {configured_probe_model} 不在当前模型目录中；"
                f"请在站点设置中改选可用模型或恢复自动选择")
            return "skip", None, UpstreamError(
                st.cfg.name, 0, st.last_probe_error)
        model = configured_probe_model
        if st.cfg.provider == "opencode" and model not in st.model_api_paths:
            return "skip", None, UpstreamError(st.cfg.name, 0, "此模型不支持当前网关接入，未发送生成请求")
        path = st.generation_path(model)
        if path == "/responses":
            body = {"model": model, "input": "ping", "max_output_tokens": 1,
                    "store": False}
            if st.cfg.provider == "opencode":
                body["max_output_tokens"] = 16
        elif path == "/messages":
            body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}
        elif path == "/generateContent":
            body = {"contents": [{"role": "user", "parts": [{"text": "ping"}]}],
                    "generationConfig": {"maxOutputTokens": 1}}
        else:
            path = "/chat/completions"
            body = {"model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1}
        url = inference_url(st.cfg, path, model)
        headers = inference_headers(st.cfg, path=path)
        st.probe_chat_at = time.time()
        st.last_probe_kind = "generation"
        t0 = time.perf_counter()
        # Reasoning models may stay silent for minutes before the single token.
        # A 30s cap falsely marks healthy stations as failed and re-trips the breaker.
        probe_read_timeout = max(30.0, min(st.cfg.timeout, 120.0))
        try:
            resp = await client.post(url, json=body, headers=headers,
                                     timeout=httpx.Timeout(
                                         connect=self.gateway.connect_timeout,
                                         read=probe_read_timeout, write=10.0, pool=10.0))
            if st.cfg is not probe_config or not st.enabled:
                return "skip", None, UpstreamError(st.cfg.name, 0, "站点配置已变更，忽略旧配置的生成测试结果")
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
            if resp.status_code == 429 and not err.quota:
                st.mark_rate_limited(max(
                    self.gateway.rate_limit_cooldown,
                    _retry_after_seconds(resp.headers.get("retry-after")),
                ), reason=str(err), source="generation_probe",
                    wait_source="upstream" if _retry_after_seconds(resp.headers.get("retry-after")) > self.gateway.rate_limit_cooldown else "gateway")
            if err.quota:
                return "quota", latency, err
            if resp.status_code >= 500:
                return "server_error", latency, err
            return "client_error", latency, err
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if isinstance(e, httpx.PoolTimeout):
                self.registry.reset_client(st, expected_client=client)
                err = UpstreamError(
                    st.cfg.name, 0,
                    "网关本地连接池已重置，本次生成测试未计入站点故障，请稍后重试")
                st.last_probe_ok = None
                st.last_probe_error = str(err)
                st.last_probe_quota = False
                return "skip", None, err
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
        - /responses       → responses 格式 body（仅支持 Responses 的站点）
        - 其他（默认 chat/completions）→ chat 格式 body
        4xx 为配置类错误（协议/模型不匹配），只记录不熔断站点；
        5xx/超时/连接失败才累计熔断。
        """
        if not self.gateway.generation_probe_enabled or not st.cfg.probe_chat:
            return
        if not st.selected_probe_model:
            return
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
            self._mark_failure(st, err, source="generation_probe")

    async def test_generation(self, st: StationState) -> dict:
        """Explicit one-shot test; does not enable background generation."""
        model = st.selected_probe_model
        status, latency, err = await self._generation_probe(st)
        if status == "ok":
            st.reset_quota()
            if latency is not None:
                st.record_probe(latency)
        elif status == "quota" and err:
            st.mark_quota_exhausted(err, source="chat")
        elif status == "server_error":
            self._mark_failure(st, err, source="generation_probe")
        return {"name": st.cfg.name, "model": model, "ok": status == "ok",
                "skipped": status == "skip", "error": err.message if err else "",
                "latency_ms": round(latency * 1000, 1) if latency is not None else None}

    async def probe(self, st: StationState) -> None:
        """立即探测单个站点（供后台循环与手动触发共用）。"""
        if st.cfg.probe_models and st.cfg.probe_path.strip("/") == "models":
            # Reuse the cached metadata operation; never add another /models poll.
            st.last_probe_at = time.time()
            await self.discover_models(st)
            return
        client = self.registry.ensure_client(st)
        sem = self.registry.ensure_semaphore(st)
        if sem.locked():
            return  # 并发已满，跳过探测
        path = st.cfg.probe_path if st.cfg.probe_path.startswith("/") else "/" + st.cfg.probe_path
        url = st.cfg.models_url if path == "/models" else st.cfg.api_base + path
        headers = inference_headers(st.cfg, catalog=True)
        t0 = time.perf_counter()
        st.last_probe_at = time.time()
        st.last_probe_kind = "connection"
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
                st._record_history(True)  # 探测成功 → 热力图绿格
                # A reachable metadata endpoint does not prove generation recovery.
                st._update_score()
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
                elif resp.status_code == 429:
                    st.mark_rate_limited(max(
                        self.gateway.rate_limit_cooldown,
                        _retry_after_seconds(resp.headers.get("retry-after")),
                    ), reason=str(err), source="connection_probe",
                        wait_source="upstream" if _retry_after_seconds(resp.headers.get("retry-after")) > self.gateway.rate_limit_cooldown else "gateway")
                elif resp.status_code == 408 or resp.status_code >= 500:
                    self._mark_failure(st, err)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            st.last_probe_latency = None
            st.last_probe_quota = False
            if isinstance(e, httpx.PoolTimeout):
                self.registry.reset_client(st, expected_client=client)
                st.last_probe_ok = None
                st.last_probe_error = "网关本地连接池已重置，本次检查未计入站点故障，请稍后重试"
                return
            st.last_probe_ok = False
            st.last_probe_error = f"探测失败: {type(e).__name__}"
            self._mark_failure(st)
        except Exception as e:  # noqa: BLE001 保证探测健壮
            st.last_probe_ok = False
            st.last_probe_latency = None
            st.last_probe_error = f"探测异常: {e}"
            st.last_probe_quota = False
            self._mark_failure(st)

    def _mark_failure(self, st: StationState, err: UpstreamError | None = None,
                      *, source: str = "connection_probe") -> None:
        st._record_history(False)  # 探测失败 → 热力图红格
        st.mark_transient_failure(
            err or UpstreamError(st.cfg.name, 0, st.last_probe_error or "连接检查失败"),
            self.gateway, source=source)

    async def discover_models(self, st: StationState, *, manual: bool = False) -> None:
        """Read key-visible metadata only. No generation, endpoint guessing or fan-out.

        A failed/partial response keeps the last complete catalog. Deterministic
        failures stop automatic refresh until credentials change or a manual retry.
        Model-list results do not prove generation health or quota availability.
        """
        now = time.time()
        if (not st.enabled or not st.cfg.probe_models or self._stop.is_set()
                or st.models_discovery_lock.locked()
                or now < st.rate_limit_until or now < st.models_retry_after_until):
            return
        if manual:
            if now - st.models_checked_at < MODEL_MANUAL_MIN_INTERVAL:
                return
            if st.models_discovery_status == "rate_limited" and now < st.models_next_refresh_at:
                return
        elif (st.models_discovery_status in MODEL_STOP_STATUSES
              or now < st.models_next_refresh_at):
            return
        sem = self.registry.ensure_semaphore(st)
        if sem.locked():
            return
        async with st.models_discovery_lock:
            cfg = st.cfg
            revision = st.models_discovery_revision
            st.models_checked_at = now
            previous_status = st.models_discovery_status
            st.models_discovery_status = "loading"
            st.models_next_refresh_at = now + self._station_probe_interval(st)
            error = ""
            status = "error"
            retry_after = 0.0
            found = None
            api_paths, unavailable = {}, {}
            started = time.perf_counter()
            try:
                async with sem:
                    response = await self._fetch_catalog(st, cfg)
                retry_header = response.headers.get("retry-after", "")
                try:
                    retry_after = max(0.0, float(retry_header))
                    if not math.isfinite(retry_after):
                        retry_after = 0.0
                except ValueError:
                    try:
                        date = parsedate_to_datetime(retry_header)
                        retry_after = max(0.0, date.replace(tzinfo=date.tzinfo or timezone.utc).timestamp() - time.time())
                    except (TypeError, ValueError, OverflowError):
                        pass
                if response.status_code in (401, 403):
                    status, error = "auth_error", "模型接口鉴权失败，请检查 Key 或权限；自动刷新已暂停"
                elif response.status_code in (404, 405):
                    status, error = "unsupported", "上游不支持 /models；可手动补充模型，自动刷新已暂停"
                elif response.status_code == 429:
                    status, error = "rate_limited", "模型接口限流，等待冷却后重试"
                elif not 200 <= response.status_code < 300:
                    error = f"模型接口返回 HTTP {response.status_code}，已保留上次列表"
                    if 300 <= response.status_code < 500:
                        status = "unsupported"
                else:
                    parsed_models = self._parse_models(response.content)
                    if cfg.provider == "opencode":
                        api_paths, unavailable = opencode_capabilities(json.loads(response.content)["data"], cfg.opencode_plan)
                    found = parsed_models
                    status = "ready"
            except (ValueError, UnicodeError):
                status, error = "invalid_response", "模型列表格式无效或不完整，已保留上次列表；自动刷新已暂停"
            except (httpx.TimeoutException, httpx.TransportError) as e:
                error = ("网关本地连接池已重置，已保留上次列表，稍后重试"
                         if isinstance(e, httpx.PoolTimeout) else
                         "模型接口连接失败，已保留上次列表，稍后重试")
            except asyncio.CancelledError:
                if st.models_discovery_revision == revision:
                    st.models_discovery_status = previous_status
                    st.models_next_refresh_at = now + MODEL_MANUAL_MIN_INTERVAL
                raise
            except Exception:
                error = "模型接口请求失败，请检查连接配置，稍后重试"
            # Ignore a response for a replaced credential, removed or disabled station.
            if (st.models_discovery_revision != revision or not st.enabled
                    or self.registry.get(cfg.name) is not st):
                if st.models_discovery_revision == revision:
                    st.models_discovery_status = previous_status
                return
            st.models_discovery_status = status
            st.models_discovery_error = error
            st.models_retry_after_until = time.time() + retry_after if retry_after else 0.0
            if found is not None:
                st.probed_models = found
                st.model_api_paths = api_paths
                st.model_unavailable = unavailable
                st.probed_model_order = list(dict.fromkeys(
                    item.strip() for item in self._parse_model_order(response.content)
                    if item.strip()
                ))
                st.models_discovered_at = time.time()
                st.models_discovery_failures = 0
                st.models_next_refresh_at = time.time() + self._station_probe_interval(st)
            else:
                st.models_discovery_failures += 1
                delay = min(3600.0, 300.0 * 2 ** min(st.models_discovery_failures - 1, 4))
                st.models_next_refresh_at = (0.0 if status in MODEL_STOP_STATUSES
                                             else time.time() + max(delay, retry_after))
            if cfg.probe_path.strip("/") == "models":
                st.last_probe_kind = "catalog"
                st.last_probe_at = now
                st.last_probe_ok = found is not None
                st.last_probe_latency = time.perf_counter() - started if found is not None else None
                st.last_probe_error = error
                st.last_probe_quota = False

    async def _fetch_catalog(self, st, cfg):
        """Fetch bounded native catalog pages, never a URL returned by upstream."""
        items, seen, params = [], set(), {}
        for _ in range(10):
            client = self.registry.ensure_client(st)
            try:
                response = await client.get(
                    cfg.models_url, params=params, headers=inference_headers(cfg, catalog=True),
                    timeout=httpx.Timeout(connect=self.gateway.connect_timeout, read=10, write=10, pool=10),
                    follow_redirects=False)
            except httpx.PoolTimeout:
                self.registry.reset_client(st, expected_client=client)
                raise
            if not response.is_success or cfg.provider not in ("anthropic", "gemini"):
                return response
            if len(response.content) > 2_000_000:
                raise ValueError("Catalog too large")
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Invalid native catalog")
            page = data.get("models" if cfg.provider == "gemini" else "data")
            if not isinstance(page, list):
                raise ValueError("Invalid native models")
            for model in page:
                if not isinstance(model, dict):
                    raise ValueError("Invalid model metadata")
                if cfg.provider == "gemini":
                    methods = model.get("supportedGenerationMethods", [])
                    if not isinstance(methods, list):
                        raise ValueError("Invalid generation methods")
                    if "generateContent" not in methods:
                        continue
                    name = model.get("name")
                    if not isinstance(name, str) or not name.startswith("models/"):
                        raise ValueError("Invalid model name")
                    items.append({"id": name.removeprefix("models/")})
                else:
                    items.append({"id": model.get("id")})
            if len(items) > 10000:
                raise ValueError("Too many models")
            token = data.get("nextPageToken") if cfg.provider == "gemini" else data.get("last_id") if data.get("has_more") else None
            if not token:
                if data.get("has_more"):
                    raise ValueError("Incomplete catalog")
                return httpx.Response(response.status_code, json={"data": items}, headers=response.headers)
            if not isinstance(token, str) or len(token) > 4096 or token in seen:
                raise ValueError("Invalid catalog pagination")
            seen.add(token)
            params = {"pageToken" if cfg.provider == "gemini" else "after_id": token}
        raise ValueError("Catalog pagination limit exceeded")

    @staticmethod
    def _parse_models(content: bytes) -> set[str]:
        if len(content) > 2_000_000:
            raise ValueError("Model list too large")
        data = json.loads(content)
        items = data.get("data") if isinstance(data, dict) else data
        if (not isinstance(items, list) or len(items) > 10000
                or (isinstance(data, dict) and (data.get("has_more") or data.get("next")))):
            raise ValueError("Incomplete model list")
        found = set()
        for item in items:
            model = item.get("id") if isinstance(item, dict) else item
            if (not isinstance(model, str) or not model.strip() or len(model) > 200
                    or any(ord(char) < 32 or ord(char) == 127 for char in model)):
                raise ValueError("Invalid model identifier")
            found.add(model.strip())
        return found

    @staticmethod
    def _parse_model_order(content: bytes) -> list[str]:
        data = json.loads(content)
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return [
            (item.get("id") if isinstance(item, dict) else item)
            for item in items
            if isinstance(item, str)
            or isinstance(item, dict) and isinstance(item.get("id"), str)
        ]

    async def stop(self) -> None:
        self._stop.set()
