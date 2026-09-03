"""中转站智能网关 - 启动入口。

用法:
    python gateway.py [--config config.yaml] [--host 0.0.0.0] [--port 8000]
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.config import AppConfig, load_config
from app.gateway_settings import GatewaySettingsStore
from app.health import HealthMonitor
from app.juice import JuiceRegistry
from app.juice_monitor import JuiceMonitor
from app.notifier import Notifier
from app.proxy import OpenAIProxy
from app.state import Registry
from app.stations import StationStore
from app.stats import EventLog, persist_loop
from app.web.admin import create_admin_router

BASE_DIR = Path(__file__).resolve().parent


def _check_bearer(auth_header: str | None, keys: list[str]) -> bool:
    """校验 Authorization: Bearer <key>（常量时间比较）。"""
    if not auth_header or not keys:
        return False
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:].strip()
    return any(hmac.compare_digest(token, k) for k in keys)


def _unauthorized(message: str = "Invalid API key provided.",
                  err_type: str = "authentication_error",
                  code: str = "invalid_api_key") -> JSONResponse:
    """OpenAI 兼容的 401 响应（SDK 对 401 不重试）。"""
    return JSONResponse(
        status_code=401,
        content={"error": {"message": message, "type": err_type,
                           "param": None, "code": code}},
    )


async def _juice_persist(juice_registry, interval: float = 60.0) -> None:
    """周期保存指纹（采样会随时发生）。"""
    import asyncio
    while True:
        try:
            await asyncio.sleep(interval)
            juice_registry.persist(
                retention_days=cfg.gateway.data_retention_days)
        except asyncio.CancelledError:
            juice_registry.persist()
            break
        except Exception as e:  # noqa: BLE001
            print(f"[juice] 保存异常: {e}")


def build_app(cfg: AppConfig) -> FastAPI:
    # Retry policy is editable from the dashboard.  Apply the persisted,
    # non-secret override before constructing components that share cfg.gateway.
    settings_store = GatewaySettingsStore(cfg.gateway.data_dir)
    retry_settings = settings_store.load(cfg.gateway)
    if retry_settings:
        for field, value in retry_settings.items():
            setattr(cfg.gateway, field, value)

    # 动态站点：用户配置（data/stations.json）优先，无则用 config.yaml 种子
    store = StationStore(cfg.gateway.data_dir)
    user_stations = store.load()
    stations = user_stations if user_stations is not None else cfg.stations
    registry = Registry(cfg.gateway, stations)
    eventlog = EventLog(cfg.gateway.data_dir,
                          retention_days=cfg.gateway.data_retention_days)
    eventlog.load()
    notifier = Notifier(cfg.gateway.webhook_url)
    monitor = HealthMonitor(registry, cfg.gateway, notifier)
    juice_registry = JuiceRegistry(
        persist_path=cfg.gateway.data_dir + "/juice.json")
    registry.juice_registry = juice_registry
    juice_monitor = JuiceMonitor(registry, cfg.gateway, juice_registry)
    proxy = OpenAIProxy(registry, cfg.gateway, eventlog, notifier)
    admin = create_admin_router(
        registry,
        cfg.gateway,
        monitor,
        eventlog,
        notifier,
        store,
        settings_store,
        juice_registry,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = [
            asyncio.create_task(monitor.run(), name="health-monitor"),
            # 主动指纹轮询已停用：指纹改由真实请求顺路采集（juice_monitor 保留备用）
            asyncio.create_task(persist_loop(eventlog), name="stats-persist"),
            asyncio.create_task(
                _juice_persist(juice_registry), name="juice-persist"),
        ]
        print(f"[gateway] 启动: {len(cfg.stations)} 个中转站 | "
              f"最大尝试 {cfg.gateway.max_attempts} 次 | "
              f"探测间隔 {cfg.gateway.probe_interval}s | "
              f"动态托管 {'开' if len(cfg.stations) > 1 else '单站无需'}"
              + (f" | webhook告警已启用" if notifier.enabled else ""))
        yield
        await monitor.stop()
        await juice_monitor.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await notifier.close()
        await registry.close_clients()
        eventlog.persist()
        print("[gateway] 已关闭，统计已落盘")

    app = FastAPI(title="中转站智能网关", lifespan=lifespan)
    app.include_router(proxy.api)
    app.include_router(admin)

    # ---- 统一 key 鉴权（对外开放）----
    # /v1/*       对外转发端点：校验 gateway.api_keys（空则不校验，保持向后兼容）
    # /admin/*    管理数据接口：校验 gateway.admin_key（空则不校验）
    # /static、/dashboard 页面本身可打开，数据请求带 key 即可
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        auth = request.headers.get("authorization")
        if path.startswith("/v1/"):
            if cfg.gateway.api_keys and not _check_bearer(auth, cfg.gateway.api_keys):
                return _unauthorized()
        elif path.startswith("/admin/"):
            if cfg.gateway.admin_key and not _check_bearer(auth, [cfg.gateway.admin_key]):
                return _unauthorized("Invalid admin key.", "admin_error", "invalid_admin_key")
        response = await call_next(request)
        # 静态资源禁用缓存：避免前端升级后浏览器继续用旧版 CSS/JS
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    static_dir = BASE_DIR / "app" / "web" / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return RedirectResponse("/static/index.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return FileResponse(static_dir / "favicon.ico", media_type="image/x-icon")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True, "ts": time.time()}

    return app


def main():
    # Windows 控制台/日志输出统一 UTF-8，避免中文乱码
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="中转站智能网关")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--host", default=None, help="监听地址 (默认取配置)")
    parser.add_argument("--port", type=int, default=None, help="监听端口 (默认取配置)")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    cfg = load_config(args.config)
    host = args.host or cfg.gateway.host
    port = args.port or cfg.gateway.port

    app = build_app(cfg)
    uvicorn.run(app, host=host, port=port, reload=args.reload,
                log_level="info")


if __name__ == "__main__":
    main()
