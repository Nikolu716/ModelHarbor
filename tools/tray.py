"""ModelHarbor 模型网关 - 系统托盘管理（轻量化 ~30MB）。

功能：托盘图标 + 右键菜单
  - 双击/菜单打开管理面板（仪表盘网页）
  - 服务状态提示（NSSM 服务 ModelHarbor + 网关健康）
  - 服务管理：重启 / 停止 / 启动
  - 开机自启开关（注册表 HKCU Run，随系统启动）

运行：pythonw.exe tools\tray.py（无控制台窗口，最省资源）
"""
from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

if TYPE_CHECKING:
    import pystray

PROJECT_DIR = Path(__file__).resolve().parent.parent
NSSM = PROJECT_DIR / "tools" / "nssm-2.24" / "win64" / "nssm.exe"
SERVICE = "ModelHarbor"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ModelHarborTray"
TRAY_ICON = PROJECT_DIR / "app" / "web" / "static" / "brand-mark-64.png"


def service_port() -> int:
    """Read the configured listener port, including saved service settings."""
    try:
        if str(PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(PROJECT_DIR))
        from app.config import load_config
        from app.service_settings import ServiceSettingsStore

        cfg = load_config(str(PROJECT_DIR / "config.yaml"))
        data_dir = Path(cfg.gateway.data_dir)
        if not data_dir.is_absolute():
            data_dir = PROJECT_DIR / data_dir
        return ServiceSettingsStore(str(data_dir)).load_port(cfg.gateway.port)
    except (OSError, ValueError, ImportError):
        return 8000


def dashboard_url() -> str:
    try:
        if str(PROJECT_DIR) not in sys.path:
            sys.path.insert(0, str(PROJECT_DIR))
        from app.config import load_config
        from app.service_settings import local_service_url

        cfg = load_config(str(PROJECT_DIR / "config.yaml"))
        if not Path(cfg.gateway.data_dir).is_absolute():
            cfg.gateway.data_dir = str(PROJECT_DIR / cfg.gateway.data_dir)
        return local_service_url(cfg.gateway) + "/dashboard"
    except (OSError, ValueError, ImportError, sqlite3.Error):
        return f"http://127.0.0.1:{service_port()}/dashboard"

# 单实例锁：绑定固定端口，第二个实例直接退出
_LOCK_PORT = 28765
_lock_sock = None  # 全局引用保持 socket 存活（局部变量会被 GC 关闭导致锁失效）


def _acquire_lock() -> bool:
    global _lock_sock
    try:
        _lock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _lock_sock.bind(("127.0.0.1", _LOCK_PORT))
        _lock_sock.listen(1)
        return True
    except OSError:
        return False


# ----------------------------------------------------------------------
# 服务与状态
# ----------------------------------------------------------------------
def service_state() -> str:
    try:
        out = subprocess.run(["sc", "query", SERVICE], capture_output=True,
                             text=True, timeout=10).stdout
        if "RUNNING" in out:
            return "运行中"
        if "STOPPED" in out:
            return "已停止"
        return "未知"
    except Exception:
        return "查询失败"


def gateway_ok() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(
                dashboard_url().removesuffix('/dashboard') + '/healthz', timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def nssm(action: str) -> None:
    """静默执行 NSSM 操作（无窗口）。"""
    subprocess.Popen([str(NSSM), action, SERVICE],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ----------------------------------------------------------------------
# 菜单动作
# ----------------------------------------------------------------------
def open_dashboard(icon, item=None):
    os.startfile(dashboard_url())


def show_status(icon, item=None):
    state = service_state()
    ok = gateway_ok()
    url = dashboard_url()
    icon.notify(f"服务: {state} | 网关: {'在线' if ok else '离线'}\n"
                f"管理面板: {url}", "ModelHarbor 模型网关")


def restart_service(icon, item=None):
    nssm("restart")
    icon.notify("正在重启服务...", "ModelHarbor 模型网关")


def stop_service(icon, item=None):
    nssm("stop")
    icon.notify("正在停止服务...", "ModelHarbor 模型网关")


def start_service(icon, item=None):
    nssm("start")
    icon.notify("正在启动服务...", "ModelHarbor 模型网关")


def _tray_cmd() -> str:
    return f'"{sys.executable}" "{PROJECT_DIR / "tools" / "tray.py"}"'


def is_autostart() -> bool:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0] == _tray_cmd()
    except FileNotFoundError:
        return False


def set_autostart(on: bool) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if on:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, _tray_cmd())
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass


def toggle_autostart(icon, item=None):
    on = not is_autostart()
    set_autostart(on)
    icon.notify("开机自启已" + ("开启" if on else "关闭"), "ModelHarbor 模型网关")


def quit_tray(icon, item=None):
    icon.stop()


# ----------------------------------------------------------------------
# 图标与菜单
# ----------------------------------------------------------------------
def make_icon() -> Image.Image:
    """加载统一品牌图标；文件缺失时绘制同款黑白降级版本。"""
    try:
        with Image.open(TRAY_ICON) as source:
            return source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        pass

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(9, 9, 9),
                        outline=(245, 245, 247), width=2)
    d.rectangle([15, 15, 49, 23], fill=(245, 245, 247))
    d.polygon([(39, 15), (49, 15), (25, 49), (15, 49)],
              fill=(245, 245, 247))
    d.rectangle([15, 41, 49, 49], fill=(245, 245, 247))
    return img


def build_menu() -> pystray.Menu:
    import pystray

    return pystray.Menu(
        pystray.MenuItem("🌐 打开管理面板", open_dashboard, default=True),
        pystray.MenuItem("📊 服务状态", show_status),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("服务管理", pystray.Menu(
            pystray.MenuItem("🔄 重启服务", restart_service),
            pystray.MenuItem("⏹ 停止服务", stop_service),
            pystray.MenuItem("▶️ 启动服务", start_service),
        )),
        pystray.MenuItem("🖥 开机自启", toggle_autostart,
                         checked=lambda item: is_autostart()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🚪 退出托盘", quit_tray),
    )


def main():
    if not _acquire_lock():
        sys.exit(0)  # 已有托盘在运行
    import pystray

    icon = pystray.Icon("ModelHarbor", make_icon(), "ModelHarbor 模型网关",
                        menu=build_menu())
    icon.run()


if __name__ == "__main__":
    main()
