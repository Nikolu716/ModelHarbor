"""仪表盘管理 API（/admin/*）。

供 Web 仪表盘与 CLI 使用：状态查询、站点启停、立即探测、日志查看。
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import time
from datetime import date
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from ..config import GatewayConfig, StationConfig
from ..access_control import (
    AccessControlStore, is_ip_allowed, validate_allowed_ips,
)
from ..gateway_settings import (
    MAX_EVENT_RETENTION,
    GatewaySettingsStore,
    probe_settings_view,
    retry_settings_view,
    validate_probe_settings,
    validate_retry_settings,
    scoring_settings_view,
    validate_scoring_settings,
)
from ..health import HealthMonitor
from ..notifier import Notifier
from ..state import Registry
from ..stations import StationStore
from ..visual_tests import VisualTestStore, VisualTestRunner, VisualTestScheduler, create_visual_test_router
from ..stats import EVENT_PAGE_MAXLEN, EventLog
from ..service_settings import (
    PortUnavailableError, ServiceSettingsStore, check_port_available, validate_port,
    is_deployment_client, endpoint_view, normalize_public_url,
)


def create_admin_router(registry: Registry, gateway: GatewayConfig,
                        monitor: HealthMonitor, eventlog: EventLog,
                        notifier: Notifier | None = None,
                        store: StationStore | None = None,
                        settings_store: GatewaySettingsStore | None = None,
                        juice_registry=None,
                        access_store: AccessControlStore | None = None,
                        inactive=None,
                        service_store: ServiceSettingsStore | None = None,
                        restart_service=None, visual_scheduler=None) -> APIRouter:
    api = APIRouter(prefix="/admin", tags=["admin"])
    station_fields = {f.name for f in dataclasses.fields(StationConfig)} - {"name"}
    retry_store = settings_store or (
        GatewaySettingsStore(gateway.data_dir) if gateway is not None else None
    )
    access_store = access_store or (
        AccessControlStore(gateway.data_dir) if gateway is not None else None
    )
    service_store = service_store or (
        ServiceSettingsStore(gateway.data_dir) if gateway is not None else None
    )
    visual_store = (visual_scheduler.store if visual_scheduler else VisualTestStore(gateway.data_dir)) if gateway is not None else None
    visual_runner = None
    if visual_store is not None:
        visual_runner = visual_scheduler.runner if visual_scheduler else VisualTestRunner(registry, gateway, visual_store)
        visual_scheduler = visual_scheduler or VisualTestScheduler(registry, visual_runner, visual_store)
        api.include_router(create_visual_test_router(registry, gateway, visual_store, visual_runner, visual_scheduler))

    def _require_deployment_client(request: Request) -> None:
        host = request.client.host if request.client else None
        if not is_deployment_client(host):
            raise HTTPException(403, "服务设置仅允许从部署机器操作")

    @api.get("/capabilities")
    async def capabilities(request: Request):
        return {"can_manage_service": is_deployment_client(
            request.client.host if request.client else None)}

    def _persist(stations: list[StationConfig]) -> bool:
        """Persist the complete station set before reporting success."""
        if store is None:
            return False
        try:
            store.save(stations)
        except OSError as exc:
            raise HTTPException(500, "站点配置保存失败，请稍后重试") from exc
        return True

    def _station_body(b: dict) -> dict:
        """Validate and extract only declared StationConfig fields."""
        unknown = sorted(set(b) - station_fields - {"name"})
        if unknown:
            raise HTTPException(400, f"不支持的站点字段: {', '.join(unknown)}")
        return {k: v for k, v in b.items() if k in station_fields}

    def _validated_station(name: object, values: dict) -> StationConfig:
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "站点名称不能为空")
        normalized_name = name.strip()
        if len(normalized_name) > 80:
            raise HTTPException(400, "站点名称不能超过 80 个字符")
        if any(ord(char) < 32 for char in normalized_name):
            raise HTTPException(400, "站点名称不能包含控制字符")
        try:
            cfg = StationConfig.from_dict(normalized_name, values)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

        if not isinstance(cfg.base_url, str) or not cfg.base_url.strip():
            raise HTTPException(400, "Base URL 不能为空")
        base_url = cfg.base_url.strip().rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise HTTPException(400, "Base URL 必须是有效的 http 或 https 地址")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise HTTPException(400, "Base URL 不能包含账号、查询参数或片段")
        if len(base_url) > 2048:
            raise HTTPException(400, "Base URL 过长")

        if not isinstance(cfg.api_key, str) or not cfg.api_key.strip():
            raise HTTPException(400, "API Key 不能为空")
        if len(cfg.api_key.strip()) > 4096:
            raise HTTPException(400, "API Key 过长")
        if any(ord(char) < 32 or ord(char) == 127 for char in cfg.api_key):
            raise HTTPException(400, "API Key 不能包含控制字符")
        if not isinstance(cfg.models, list) or len(cfg.models) > 500 or any(
                not isinstance(model, str) or not model.strip() or len(model.strip()) > 200
                for model in cfg.models):
            raise HTTPException(400, "模型必须是最多 500 项的非空字符串列表")
        if isinstance(cfg.priority, bool) or not isinstance(cfg.priority, int) \
                or not 1 <= cfg.priority <= 10000:
            raise HTTPException(400, "优先级必须是 1 到 10000 之间的整数")
        if isinstance(cfg.max_concurrency, bool) or not isinstance(cfg.max_concurrency, int) \
                or not 1 <= cfg.max_concurrency <= 10000:
            raise HTTPException(400, "最大并发必须是 1 到 10000 之间的整数")
        if isinstance(cfg.weight, bool) or not isinstance(cfg.weight, (int, float)) \
                or not math.isfinite(cfg.weight) or cfg.weight <= 0:
            raise HTTPException(400, "权重必须大于 0")
        if isinstance(cfg.timeout, bool) or not isinstance(cfg.timeout, (int, float)) \
                or not math.isfinite(cfg.timeout) or cfg.timeout <= 0:
            raise HTTPException(400, "超时必须大于 0")
        if cfg.probe_interval is not None and (
                isinstance(cfg.probe_interval, bool)
                or not isinstance(cfg.probe_interval, (int, float))
                or not math.isfinite(cfg.probe_interval)
                or not 300 <= cfg.probe_interval <= 86400):
            raise HTTPException(400, "站点检查间隔必须在 300 到 86400 秒之间")
        for field in ("priority_auto", "probe_models", "probe_chat", "probe_balance", "enabled"):
            if not isinstance(getattr(cfg, field), bool):
                raise HTTPException(400, f"{field} 必须是布尔值")
        for field in ("probe_path", "probe_chat_path"):
            value = getattr(cfg, field)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise HTTPException(400, f"{field} 必须是有效路径")
        if (not isinstance(cfg.probe_chat_model, str)
                or len(cfg.probe_chat_model) > 200
                or any(ord(char) < 32 or ord(char) == 127
                       for char in cfg.probe_chat_model)):
            raise HTTPException(400, "生成探测模型名称无效")
        if not isinstance(cfg.headers, dict) or len(cfg.headers) > 100 or any(
                not isinstance(key, str) or not key or len(key) > 256
                or any(not char.isascii()
                       or not (char.isalnum() or char in "!#$%&'*+-.^_`|~")
                       for char in key)
                or not isinstance(value, str) or len(value) > 4096
                or any(char in "\r\n" for char in value)
                for key, value in cfg.headers.items()):
            raise HTTPException(400, "附加请求头必须是有效的字符串键值对")

        models = list(dict.fromkeys(model.strip() for model in cfg.models))
        return dataclasses.replace(
            cfg,
            name=normalized_name,
            base_url=base_url,
            api_key=cfg.api_key.strip(),
            probe_chat_model=cfg.probe_chat_model.strip(),
            models=models,
        )

    def _configs_with(replacement: StationConfig) -> list[StationConfig]:
        return [replacement if cfg.name == replacement.name else cfg
                for cfg in registry.stations]

    def _station_view(st, runtime: dict) -> dict:
        cfg = st.cfg
        return {
            "name": cfg.name,
            "base_url": cfg.base_url,
            "models": list(cfg.models),
            "model_routing": cfg.model_routing,
            "route_models": list(cfg.route_models),
            "provider": cfg.provider,
            "opencode_plan": cfg.opencode_plan,
            "go_usage": st.go_usage_view(),
            "model_api_paths": st.protocol_view(),
            "model_unavailable": dict(st.model_unavailable),
            "detected_models": sorted(st.probed_models),
            "probe_model_candidates": st.probe_model_candidates,
            "generation_probe": st.generation_probe_view(),
            "effective_models": sorted(st.effective_models),
            "accepts_any_model": st.accepts_any_model,
            "model_discovery": st.model_discovery_view(),
            "priority": cfg.priority,
            "priority_auto": cfg.priority_auto,
            "priority_bias": st.priority_bias,
            "health_score": runtime["health_score"],
            "health_score_breakdown": runtime["health_score_breakdown"],
            "rank": runtime["rank"],
            "weight": cfg.weight,
            "timeout": cfg.timeout,
            "max_concurrency": cfg.max_concurrency,
            "billing": cfg.billing,
            "enabled": st.enabled,
            "probe_models": cfg.probe_models,
            "probe_chat": cfg.probe_chat,
            "probe_chat_model": cfg.probe_chat_model,
            "probe_interval": cfg.probe_interval,
            "probe_chat_path": cfg.probe_chat_path,
            "has_api_key": bool(cfg.api_key),
            "status": runtime["status"],
            "protection": runtime["protection"],
            "quota_exhausted": runtime["quota_exhausted"],
            "last_probe_ok": runtime["last_probe_ok"],
            "last_probe_kind": runtime["last_probe_kind"],
            "last_probe_error": runtime["last_probe_error"],
        }

    @api.get("/status")
    async def status():
        # 事件由独立分页接口读取，避免 3 秒状态轮询重复传输日志。
        snap = registry.snapshot()
        snap["statistics_storage"] = eventlog.storage_view()
        # 合并今日每站缓存命中率（监控列表"缓存命中"列数据源）
        try:
            daily = eventlog.daily_stats()
            cache_by_station = {st["name"]: st for st in daily["stations"]}
            for s in snap["stations"]:
                ds = cache_by_station.get(s["name"])
                s["cache_hit_rate"] = ds.get("cache_hit_rate") if ds else None
                s["cache_read"] = ds.get("cache_read", 0) if ds else 0
                s["avg_ttft_ms"] = ds.get("avg_ttft_ms") if ds else None
        except Exception:
            for s in snap["stations"]:
                s["cache_hit_rate"] = None
                s["cache_read"] = 0
        # 合并单token指纹（第二层掺水检测）并重算 juice 分
        try:
            fps_by_station = (juice_registry.snapshot()
                              if juice_registry is not None else {})
            from ..juice import juice_score as _js
            for s in snap["stations"]:
                fps = fps_by_station.get(s["name"], [])
                s["fingerprints"] = fps
                worst = "mismatch" if any(
                    fp["flag"] == "mismatch" for fp in fps) else ""
                s["juice_score"] = _js(registry.get(s["name"]), worst)
        except Exception as e:
            import traceback
            print(f'[admin] fingerprint merge error: {e}')
            traceback.print_exc()
        return snap

    def access_settings_view(request: Request) -> dict:
        return {
            "allowed_ips": list(gateway.allowed_ips),
            "always_allowed_ips": [],
            "client_ip": request.client.host if request.client else None,
        }

    @api.get("/settings/access")
    async def get_access_settings(request: Request):
        if gateway is None:
            raise HTTPException(503, "网关访问设置暂不可用")
        return access_settings_view(request)

    @api.put("/settings/access")
    async def update_access_settings(request: Request, body: dict):
        if gateway is None or access_store is None:
            raise HTTPException(503, "网关访问设置暂不可用")
        if set(body) != {"allowed_ips"}:
            raise HTTPException(400, "请仅提交 allowed_ips 列表")
        try:
            values = validate_allowed_ips(body["allowed_ips"])
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        host = request.client.host if request.client else None
        if not is_ip_allowed(host, values):
            raise HTTPException(400, "请保留当前访问 IP，避免保存后失去访问权限")
        try:
            access_store.save(values)
        except OSError as exc:
            raise HTTPException(500, "白名单保存失败，请稍后重试") from exc
        gateway.allowed_ips = values
        return {**access_settings_view(request), "persisted": True}

    @api.get("/settings/retry")
    async def get_retry_settings():
        """Return the live retry policy without gateway keys or other secrets."""
        if gateway is None:
            raise HTTPException(503, "网关重试设置暂不可用")
        return retry_settings_view(gateway)

    @api.put("/settings/retry")
    async def update_retry_settings(body: dict):
        """Validate, persist, then atomically expose a new retry policy."""
        if gateway is None or retry_store is None:
            raise HTTPException(503, "网关重试设置暂不可用")
        if not body:
            raise HTTPException(400, "没有可更新的重试设置")
        try:
            values = validate_retry_settings(
                body,
                current=retry_settings_view(gateway),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            retry_store.save(values)
        except OSError as exc:
            raise HTTPException(500, "重试设置保存失败，请稍后重试") from exc
        for field, value in values.items():
            setattr(gateway, field, value)
        return {**values, "persisted": True}

    @api.get("/settings/scoring")
    async def get_scoring_settings():
        return scoring_settings_view(gateway)

    @api.put("/settings/scoring")
    async def update_scoring_settings(body: dict):
        try:
            values = validate_scoring_settings(body)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            retry_store.save_scoring(values)
        except (OSError, ValueError) as exc:
            raise HTTPException(500, "评分设置保存失败，请稍后重试") from exc
        gateway.scoring_speed_weight = values["scoring_speed_weight"]
        for st in registry.states.values():
            st._update_score()
        return {**values, "persisted": True}

    @api.get("/settings/probe")
    async def get_probe_settings():
        if gateway is None:
            raise HTTPException(503, "网关探测设置暂不可用")
        return probe_settings_view(gateway)

    @api.put("/settings/probe")
    async def update_probe_settings(body: dict):
        if gateway is None or retry_store is None:
            raise HTTPException(503, "网关探测设置暂不可用")
        try:
            values = validate_probe_settings(
                body,
                current=probe_settings_view(gateway),
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            retry_store.save_probe(values)
        except OSError as exc:
            raise HTTPException(500, "探测设置保存失败，请稍后重试") from exc
        for field, value in values.items():
            setattr(gateway, field, value)
        return {**values, "persisted": True}

    @api.get("/settings/logs")
    async def get_log_settings():
        if retry_store is None:
            raise HTTPException(503, "日志设置暂不可用")
        return {
            "event_retention": eventlog.maxlen,
            "min_event_retention": 100,
            "max_event_retention": MAX_EVENT_RETENTION,
            "storage": eventlog.storage_view(),
        }

    @api.put("/settings/logs")
    async def update_log_settings(body: dict):
        if retry_store is None:
            raise HTTPException(503, "日志设置暂不可用")
        if set(body) != {"event_retention"}:
            raise HTTPException(400, "请只提供 event_retention 字段")
        value = body.get("event_retention")
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(400, "event_retention 必须是整数")
        if not 100 <= value <= MAX_EVENT_RETENTION:
            raise HTTPException(400, "保留条数必须在 100 到 10000 之间")
        try:
            retry_store.save_logs(value)
        except OSError as exc:
            raise HTTPException(500, "日志保留设置保存失败") from exc
        eventlog.set_limit(value)
        saved = eventlog.persist()
        if not saved:
            raise HTTPException(503, "留存上限已保存，但统计文件暂时不可写；" + eventlog.storage_view()["message"])
        return {"event_retention": value, "persisted": True, "storage": eventlog.storage_view()}

    @api.get("/settings/endpoint")
    async def get_endpoint_settings(request: Request):
        return endpoint_view(gateway, service_store, request)

    @api.put("/settings/endpoint")
    async def update_endpoint_settings(request: Request, body: dict):
        _require_deployment_client(request)
        if service_store is None:
            raise HTTPException(503, "服务设置暂不可用")
        if set(body) != {"public_url"}:
            raise HTTPException(400, "请只提供 public_url 字段")
        try:
            service_store.save_public_url(normalize_public_url(body["public_url"]))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, "接入地址保存失败") from exc
        return {**endpoint_view(gateway, service_store, request), "persisted": True}

    @api.get("/settings/service")
    async def get_service_settings(request: Request):
        _require_deployment_client(request)
        if service_store is None:
            raise HTTPException(503, "服务设置暂不可用")
        return {
            "port": service_store.load_port(gateway.port),
            "active_port": gateway.port,
        }

    @api.put("/settings/service")
    async def update_service_settings(request: Request, body: dict):
        _require_deployment_client(request)
        if service_store is None:
            raise HTTPException(503, "服务设置暂不可用")
        if set(body) != {"port"}:
            raise HTTPException(400, "请只提供 port 字段")
        try:
            port = validate_port(body.get("port"))
            if port != gateway.port:
                check_port_available(port, gateway.host)
            service_store.save_port(port)
        except PortUnavailableError as exc:
            raise HTTPException(409, "该端口已被占用或无法绑定，请选择其他端口") from exc
        except ValueError as exc:
            raise HTTPException(400, "端口必须是 1 到 65535 之间的整数") from exc
        except OSError as exc:
            raise HTTPException(500, "服务端口设置保存失败") from exc
        return {
            "port": port,
            "active_port": gateway.port,
            "restart_required": port != gateway.port,
            "persisted": True,
        }

    @api.post("/service/restart", status_code=202)
    async def restart_gateway_service(request: Request):
        _require_deployment_client(request)
        if restart_service is None or not restart_service():
            raise HTTPException(503, "当前进程无法通过服务管理器重启")
        return {"scheduled": True}

    @api.get("/activity/stream")
    async def activity_stream(request: Request):
        """Push current route changes immediately; status polling is fallback."""
        activation_ended = inactive() if callable(inactive) else inactive
        async def events():
            subscriber_id, queue = registry.subscribe_route_activity()
            try:
                while True:
                    if activation_ended is not None and activation_ended.is_set():
                        yield 'event: account_changed\ndata: {}\n\n'
                        break
                    try:
                        snapshot = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": keep-alive\n\n"
                        continue
                    payload = json.dumps(
                        {"route_activity": snapshot},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"event: activity\ndata: {payload}\n\n"
            except asyncio.CancelledError:
                raise
            finally:
                registry.unsubscribe_route_activity(subscriber_id)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @api.post("/station/{name}/toggle")
    async def toggle(name: str):
        st = registry.get(name)
        if st is None:
            raise HTTPException(404, f"中转站不存在: {name}")
        next_cfg = dataclasses.replace(st.cfg, enabled=not st.enabled)
        _persist(_configs_with(next_cfg))
        st.cfg = next_cfg
        st.enabled = next_cfg.enabled
        st._update_score()
        if st.enabled:
            # 重新启用后允许立即探测
            st.last_probe_at = 0.0
        print(f"[admin] {name} -> {'启用' if st.enabled else '禁用'}")
        return {"name": name, "enabled": st.enabled}

    @api.post("/station/{name}/probe")
    async def probe(name: str):
        st = registry.get(name)
        if st is None:
            raise HTTPException(404, f"中转站不存在: {name}")
        if not st.enabled:
            raise HTTPException(400, f"中转站 {name} 已禁用，无法探测")
        await monitor.probe(st)
        return {
            "name": name,
            "ok": st.last_probe_ok,
            "latency_ms": None if st.last_probe_latency is None
            else round(st.last_probe_latency * 1000, 1),
            "error": st.last_probe_error,
        }

    @api.post("/station/{name}/models/refresh")
    async def refresh_models(name: str):
        st = registry.get(name)
        if st is None:
            raise HTTPException(404, "站点不存在")
        if not st.enabled or not st.cfg.probe_models:
            raise HTTPException(400, "请先启用站点并开启自动发现模型")
        await monitor.discover_models(st, manual=True)
        return {"name": name, "detected_models": sorted(st.probed_models),
                "model_discovery": st.model_discovery_view()}

    @api.post("/station/{name}/go-usage/refresh")
    async def refresh_go_usage(name: str):
        st = registry.get(name)
        if st is None:
            raise HTTPException(404, "站点不存在")
        if st.cfg.provider != "opencode" or st.cfg.opencode_plan != "go":
            raise HTTPException(400, "本站未使用 Go 订阅")
        if not st.enabled:
            raise HTTPException(400, "站点已停用，仅显示缓存用量")
        return await monitor.refresh_go_usage(st, manual=True)

    @api.post("/station/{name}/generation-probe")
    async def generation_probe(name: str):
        st = registry.get(name)
        if st is None:
            raise HTTPException(404, "站点不存在")
        if not st.enabled:
            raise HTTPException(400, "请先启用站点再进行生成测试")
        return await monitor.test_generation(st)

    @api.post("/station/{name}/reset")
    async def reset(name: str):
        """立即探测额度是否恢复；仍无额度则保持排除并继续自动探测。

        后台默认会自动探测，此接口只是立刻再探一次，无需再手动点。
        """
        st = registry.get(name)
        if st is None:
            raise HTTPException(404, f"中转站不存在: {name}")
        st.last_probe_at = 0.0
        if not st.enabled:
            st.reset_quota()
            recovered = True
        else:
            recovered = await monitor.probe_quota_recovery(st, manual=True)
        print(f"[admin] {name} 额度探测: "
              f"{'已恢复' if recovered else '仍无额度，继续自动探测'}")
        if recovered and notifier is not None:
            await notifier.send("station_reset", name, "探测确认额度已恢复")
        return {
            "name": name,
            "quota_exhausted": st.quota_exhausted,
            "recovered": recovered,
            "status": st.status,
            "probe_ok": st.last_probe_ok,
            "probe_error": st.last_probe_error or st.quota_probe_error,
        }

    @api.get("/logs")
    async def logs(
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=EVENT_PAGE_MAXLEN),
        tail: int | None = Query(None),
        ip: str | None = Query(None, max_length=64),
        model: str | None = Query(None, max_length=256),
        date_from: date | None = Query(None),
        date_to: date | None = Query(None),
    ):
        """Paginate retained events, newest first.

        ``tail`` remains as a compatibility alias for existing CLI and UI
        clients.  As before, its value is clamped to 1..500; when supplied it
        selects the first (newest) page and overrides ``page_size``.
        """
        if tail is not None:
            page = 1
            page_size = max(1, min(tail, EVENT_PAGE_MAXLEN))
        if date_from and date_to and date_from > date_to:
            raise HTTPException(400, "date_from must not be after date_to")
        return eventlog.paginated(
            page=page, page_size=page_size, ip=ip, model=model,
            date_from=date_from, date_to=date_to)

    @api.get("/logs/analytics")
    async def logs_analytics(
        date_from: date | None = Query(None),
        date_to: date | None = Query(None),
        ip: str | None = Query(None, max_length=64),
        model: str | None = Query(None, max_length=256),
        group_by: str = Query("ip"),
    ):
        try:
            return eventlog.log_analytics(
                date_from=date_from, date_to=date_to, ip=ip,
                model=model, group_by=group_by)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/stats/daily")
    async def stats_daily():
        """今日统计：各站使用占比 + 按小时时间线（甘特图数据源）。"""
        return eventlog.daily_stats()

    @api.get("/stats/history")
    async def stats_history(days: int = Query(365, ge=1, le=365)):
        """Bounded daily usage history for charts; no cost is inferred."""
        return eventlog.daily_history(days)

    # ---------------- 动态站点管理（用户自己配置，实时生效） ----------------
    @api.get("/stations")
    async def list_stations():
        """Return editable configuration plus safe runtime state, never secrets."""
        runtime = {item["name"]: item for item in registry.snapshot()["stations"]}
        states = sorted(
            registry.states.values(),
            key=lambda item: (
                runtime[item.cfg.name]["rank"] is None,
                runtime[item.cfg.name]["rank"] or 100000,
                item.cfg.name.casefold(),
            ),
        )
        visual_results = visual_store.summaries(registry) if visual_store else {}
        visual_plans = visual_store.schedules() if visual_store else {}
        return [_station_view(st, runtime[st.cfg.name]) | {"visual_test": visual_results.get(st.cfg.name),
                "visual_running": bool(visual_runner and st.cfg.name in visual_runner.busy),
                "visual_job": visual_runner.job(st.cfg.name, summary=True) if visual_runner else None,
                "visual_model": (visual_runner.job(st.cfg.name) or {}).get("settings", {}).get("model")
                    if visual_runner and (visual_runner.job(st.cfg.name) or {}).get("state") == "running"
                    else (visual_plans.get(st.cfg.name, {}).get("settings") or {}).get("model"),
                "visual_schedule": {key: visual_plans.get(st.cfg.name, {}).get(key) for key in ("enabled", "paused_reason")}}
                for st in states]

    @api.post("/stations", status_code=201)
    async def add_station(body: dict):
        """新增站点（实时生效 + 持久化）。"""
        body = dict(body)
        body.setdefault("probe_models", True)
        body.setdefault("model_routing", "catalog")
        body.setdefault("priority_auto", "priority" not in body)
        cfg = _validated_station(body.get("name"), _station_body(body))
        name = cfg.name
        if any(existing.casefold() == name.casefold()
               for existing in registry.states):
            raise HTTPException(409, f"站点已存在: {name}")
        previous_configs = list(registry.stations)
        persisted = _persist([*previous_configs, cfg])
        try:
            st = registry.add_station(cfg)
        except Exception as exc:
            if store is not None:
                try:
                    store.save(previous_configs)
                except OSError as rollback_error:
                    print(f"[stations] 新增运行态失败且持久化回滚失败: {rollback_error}")
            raise HTTPException(500, "站点运行时加载失败，配置已回滚") from exc
        st.last_probe_at = 0.0  # 健康监控下一轮自动探测，不阻塞保存反馈
        print(f"[admin] 新增站点: {name}")
        return {
            "name": name,
            "ok": True,
            "persisted": persisted,
            "probe_scheduled": st.enabled,
        }

    @api.put("/stations/{name}")
    async def update_station(name: str, body: dict):
        """修改站点配置（实时生效 + 持久化）。"""
        current = registry.get(name)
        if current is None:
            raise HTTPException(404, f"站点不存在: {name}")
        requested_name = body.get("name")
        if requested_name is not None and requested_name != name:
            raise HTTPException(400, "站点名称不可直接修改，请新建后删除旧站点")
        changes = _station_body(body)
        if not changes:
            raise HTTPException(400, "没有可更新的字段")
        candidate_values = {
            field: getattr(current.cfg, field) for field in station_fields
        }
        candidate_values.update(changes)
        candidate = _validated_station(name, candidate_values)
        normalized_changes = {
            field: getattr(candidate, field)
            for field in station_fields
            if getattr(candidate, field) != getattr(current.cfg, field)
        }
        if not normalized_changes:
            return {
                "name": name,
                "ok": True,
                "persisted": store is not None,
                "probe_scheduled": False,
                "unchanged": True,
            }
        transport_fields = {
            "base_url", "api_key", "headers", "timeout", "max_concurrency", "provider", "opencode_plan",
        }
        if transport_fields.intersection(normalized_changes):
            activity = registry.route_activity_snapshot()
            active = next((item for item in activity["active_stations"]
                           if item["station"] == name), None)
            if active:
                raise HTTPException(
                    409,
                    f"站点仍有 {active['inflight']} 个请求进行中，请稍后修改连接配置",
                )
        persisted = _persist(_configs_with(candidate))
        st = registry.update_station(name, normalized_changes)
        if st.enabled:
            st.last_probe_at = 0.0
        print(f"[admin] 更新站点: {name}")
        return {
            "name": name,
            "ok": True,
            "persisted": persisted,
            "probe_scheduled": st.enabled,
        }

    @api.delete("/stations/{name}")
    async def delete_station(name: str):
        """删除站点（实时生效 + 持久化）。"""
        if not registry.get(name):
            raise HTTPException(404, f"站点不存在: {name}")
        if len(registry.states) <= 1:
            raise HTTPException(409, "至少保留一个站点，请先添加替代站点")
        activity = registry.route_activity_snapshot()
        active = next((item for item in activity["active_stations"]
                       if item["station"] == name), None)
        if active:
            raise HTTPException(
                409,
                f"站点仍有 {active['inflight']} 个请求进行中，请稍后再删除",
            )
        remaining = [cfg for cfg in registry.stations if cfg.name != name]
        persisted = _persist(remaining)
        registry.remove_station(name)
        if visual_store:
            visual_store.delete_result(name)
            visual_store.delete_schedule(name)
        if juice_registry is not None:
            active = {s.name for s in registry.stations}
            juice_registry.prune(active_stations=active)
        print(f"[admin] 删除站点: {name}")
        return {
            "name": name,
            "ok": True,
            "persisted": persisted,
            "remaining": len(remaining),
        }

    @api.get("/ping")
    async def ping():
        return {"ts": time.time(), "ok": True}

    return api
