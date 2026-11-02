"""Machine-wide listener settings that apply when the gateway starts."""
from __future__ import annotations

import json
import ipaddress
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .access_control import AccessControlStore, LOOPBACK_IPS, normalize_ip


def deployment_addresses() -> frozenset[str]:
    """Local interface addresses, never values from HTTP forwarding headers."""
    addresses = set(LOOPBACK_IPS)
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None):
            try:
                addresses.add(normalize_ip(item[4][0]))
            except ValueError:
                pass
    except OSError:
        pass
    return frozenset(addresses)


def is_deployment_client(host: str | None) -> bool:
    try:
        return bool(host and normalize_ip(host) in deployment_addresses())
    except ValueError:
        return False


def local_service_url(gateway) -> str:
    """Choose a local listener address allowed by the active account, read-only.

    This is for deployment tools; it never adds an address to a policy. If no
    local address is allowed, the fallback URL correctly receives a denial.
    """
    root = Path(gateway.data_dir)
    directory, defaults = root, gateway.allowed_ips
    database = root / "accounts.sqlite3"
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute("SELECT value FROM metadata WHERE key='active_account'").fetchone()
        if not row:
            raise ValueError("Missing active account")
        if row[0] != "default":
            if len(row[0]) != 32 or any(c not in "0123456789abcdef" for c in row[0]):
                raise ValueError("Invalid active account ID")
            directory, defaults = root / "accounts" / row[0], []
    allowed = AccessControlStore(str(directory)).load(defaults)
    addresses = deployment_addresses()
    candidates = [ip for ip in allowed if ip in addresses]
    listener = gateway.host
    if listener not in ("0.0.0.0", "::"):
        candidates = [ip for ip in candidates if ip == listener]
    elif listener == "0.0.0.0":
        candidates = [ip for ip in candidates if ":" not in ip]
    host = next(iter(candidates), "::1" if listener == "::" else
                "127.0.0.1" if listener == "0.0.0.0" else listener)
    port = ServiceSettingsStore(str(root)).load_port(gateway.port)
    return f"http://{'[' + host + ']' if ':' in host else host}:{port}"


class PortUnavailableError(OSError):
    """The requested listener address and port cannot be bound."""


def validate_port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return value


def check_port_available(port: int, host: str) -> None:
    """Raise OSError when the configured listener address cannot bind this port.

    This is a preflight check only: another process can still claim the port
    between saving the setting and the next service start.
    """
    port = validate_port(port)
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        # SO_REUSEADDR can make an occupied listener appear bindable on Windows.
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            listener.setsockopt(socket.SOL_SOCKET, exclusive, 1)
        try:
            listener.bind((host, port))
        except OSError as exc:
            raise PortUnavailableError(str(exc)) from exc


class ServiceSettingsStore:
    """Persist listener settings outside account-specific configuration."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "service-settings.json"

    def load_port(self, default: int) -> int:
        if not self.path.exists():
            return validate_port(default)
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("service settings root must be an object")
            return validate_port(payload.get("port", default))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"[service] listener settings invalid; using configured default: {exc}")
            return validate_port(default)

    def save_port(self, port: int) -> None:
        self._save({"port": validate_port(port)})

    def load_public_url(self, default: str = "") -> str:
        if not self.path.exists():
            return normalize_public_url(default)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("service settings root must be an object")
            return normalize_public_url(payload.get("public_url", default))
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"[service] public endpoint setting invalid; using configured default: {exc}")
            return normalize_public_url(default)

    def save_public_url(self, value: str) -> None:
        self._save({"public_url": normalize_public_url(value)})

    def _save(self, update: dict) -> None:
        payload = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(payload, dict):
            raise ValueError("service settings root must be an object")
        payload.update(update)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, indent=1), encoding="utf-8")
        temporary.replace(self.path)


def normalize_public_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("接入地址必须是字符串")
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if any(c.isspace() or ord(c) < 32 for c in value) or len(value) > 2048:
        raise ValueError("接入地址包含空白或过长")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("请填写完整的 HTTP(S) 地址，不含密钥、查询参数或锚点")
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    if parsed.hostname in ("localhost", "0.0.0.0", "::"):
        raise ValueError("请使用客户端可访问的网关 IP 或域名")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and address.is_loopback:
        raise ValueError("对外接入地址不能使用 127.0.0.1 或 ::1")
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return parsed._replace(path=path).geturl()


def endpoint_view(gateway, store, request) -> dict:
    """Explicit proxy URLs are exact; automatic URLs use the active listener."""
    configured = store.load_public_url(gateway.public_url) if store else gateway.public_url
    root = configured
    source, candidates = "configured", []
    if not root:
        host = request.url.hostname or ""
        try:
            local = ipaddress.ip_address(host).is_loopback or ipaddress.ip_address(host).is_unspecified
        except ValueError:
            local = host.lower() in ("localhost", "testserver", "")
        if not local:
            root = str(request.url.replace(path="", query="", fragment=""))
            source = "request"
        else:
            networks = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
            for value in sorted(deployment_addresses()):
                address = ipaddress.ip_address(value)
                if address.version == 4 and any(address in network for network in networks):
                    if gateway.host in ("0.0.0.0", "::", value):
                        candidates.append(f"http://{value}:{gateway.port}")
            if len(candidates) == 1:
                root, source = candidates[0], "local_interface"
            else:
                source = "needs_configuration"
    return {"public_url": configured, "endpoint": root.rstrip("/") + "/v1" if root else "",
            "port": gateway.port, "source": source, "candidates": candidates}
