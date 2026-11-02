"""IP allowlist validation and fail-closed, atomic persistence."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path


LOOPBACK_IPS = ("127.0.0.1", "::1")


def normalize_ip(value: str) -> str:
    if "%" in value:
        raise ValueError("不支持带接口标识的 IP 地址")
    address = ipaddress.ip_address(value.strip())
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def validate_allowed_ips(values: object) -> list[str]:
    if not isinstance(values, list) or len(values) > 256:
        raise ValueError("IP 白名单必须是最多 256 项的列表")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("白名单的每一项必须是有效的 IPv4 或 IPv6 地址")
        try:
            address = normalize_ip(value)
        except ValueError as exc:
            raise ValueError(f"无效的 IP 地址: {value}（请只填写 IP，不含协议、端口或网段）") from exc
        if address not in result:
            result.append(address)
    return result


def is_ip_allowed(host: str | None, allowed_ips: list[str]) -> bool:
    if not host:
        return False
    try:
        address = normalize_ip(host)
    except ValueError:
        return False
    return address in allowed_ips


class AccessControlStore:
    """A saved allowlist overrides YAML; corrupt policy never opens access."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "access-control.json"

    def load(self, defaults: list[str]) -> list[str]:
        if not self.path.exists():
            return validate_allowed_ips(defaults)
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or set(payload) != {"allowed_ips"}:
            raise ValueError("access-control.json 必须包含 allowed_ips 列表")
        return validate_allowed_ips(payload["allowed_ips"])

    def save(self, values: list[str]) -> None:
        normalized = validate_allowed_ips(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"allowed_ips": normalized}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)
