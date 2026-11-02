"""ModelHarbor 模型网关 - 启动入口。

用法:
    python gateway.py [--config config.yaml] [--host 0.0.0.0] [--port 8000]
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import hmac
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from app.config import AppConfig, load_config
from app.service_settings import ServiceSettingsStore

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


def build_app(cfg: AppConfig) -> FastAPI:
    from app.accounts import AccountManager, RuntimeMiddleware, create_account_router

    manager = AccountManager(cfg)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start()
        print(f"[ModelHarbor] started | active account: {manager.active.id}")
        try:
            yield
        finally:
            await manager.stop()
            print("[ModelHarbor] stopped; account statistics saved")

    app = FastAPI(title="ModelHarbor", lifespan=lifespan)
    @app.exception_handler(sqlite3.Error)
    @app.exception_handler(OSError)
    async def storage_error(request: Request, exc):
        return JSONResponse(status_code=503, content={'detail': '账号存储暂不可用，请稍后重试'})

    app.state.accounts = manager
    app.state.uvicorn_server = None
    app.state.restart_scheduled = False

    def schedule_restart() -> bool:
        server = app.state.uvicorn_server
        if server is None:
            return False
        if not app.state.restart_scheduled:
            app.state.restart_scheduled = True
            asyncio.get_running_loop().call_later(
                0.75, setattr, server, "should_exit", True)
        return True

    manager.request_restart = schedule_restart
    app.include_router(create_account_router(manager))
    # Include the public schema; middleware dispatches to the pinned runtime.
    app.include_router(manager.active.app.router)
    app.add_middleware(RuntimeMiddleware, manager=manager, check_bearer=_check_bearer)

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

    parser = argparse.ArgumentParser(description="ModelHarbor 模型网关")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--host", default=None, help="监听地址 (默认取配置)")
    parser.add_argument("--port", type=int, default=None, help="监听端口 (默认取配置)")
    parser.add_argument("--reload", action="store_true", help="开发模式热重载")
    args = parser.parse_args()

    cfg = load_config(args.config)
    host = args.host or cfg.gateway.host
    port = args.port if args.port is not None else ServiceSettingsStore(
        cfg.gateway.data_dir).load_port(cfg.gateway.port)
    cfg.gateway.port = port

    app = build_app(cfg)
    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, reload=args.reload,
        log_level="info", proxy_headers=False))
    app.state.uvicorn_server = server
    server.run()


if __name__ == "__main__":
    main()
