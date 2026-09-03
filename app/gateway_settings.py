"""Runtime-editable gateway retry settings and atomic persistence."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .config import GatewayConfig


RETRY_SETTING_FIELDS = (
    "max_attempts",
    "retry_backoff",
    "max_rounds",
    "round_backoff",
    "max_round_backoff",
    "max_retry_seconds",
)


def retry_settings_view(gateway: GatewayConfig) -> dict[str, int | float]:
    """Return the public, non-secret retry configuration."""
    return {field: getattr(gateway, field) for field in RETRY_SETTING_FIELDS}


def validate_retry_settings(
    values: object,
    *,
    current: dict[str, int | float] | None = None,
) -> dict[str, int | float]:
    """Validate a partial update and return a complete normalized snapshot."""
    if not isinstance(values, dict):
        raise ValueError("重试设置必须是 JSON 对象")
    unknown = sorted(set(values) - set(RETRY_SETTING_FIELDS))
    if unknown:
        raise ValueError(f"不支持的重试字段: {', '.join(unknown)}")
    if not values and current is None:
        raise ValueError("重试设置不能为空")

    result: dict[str, int | float] = dict(current or {})
    result.update(values)
    missing = [field for field in RETRY_SETTING_FIELDS if field not in result]
    if missing:
        raise ValueError(f"缺少重试字段: {', '.join(missing)}")

    def integer(name: str, minimum: int, maximum: int) -> int:
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} 必须是整数")
        if not minimum <= value <= maximum:
            raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
        return value

    def number(name: str, minimum: float, maximum: float) -> float:
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} 必须是数字")
        normalized = float(value)
        if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
            raise ValueError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
        return normalized

    normalized: dict[str, int | float] = {
        "max_attempts": integer("max_attempts", 1, 100),
        "retry_backoff": number("retry_backoff", 0, 60),
        "max_rounds": integer("max_rounds", 0, 10_000),
        "round_backoff": number("round_backoff", 0.1, 300),
        "max_round_backoff": number("max_round_backoff", 0.1, 600),
        "max_retry_seconds": number("max_retry_seconds", 0, 86_400),
    }
    if normalized["max_round_backoff"] < normalized["round_backoff"]:
        raise ValueError("max_round_backoff 不能小于 round_backoff")
    if normalized["max_rounds"] == 0 and normalized["max_retry_seconds"] == 0:
        raise ValueError("重试轮次和总时长不能同时不限")
    return normalized


class GatewaySettingsStore:
    """Persist retry settings without exposing unrelated gateway secrets."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "gateway-settings.json"

    def load(self, defaults: GatewayConfig) -> dict[str, int | float] | None:
        if not self.path.exists():
            return None
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("根节点必须是对象")
            values = payload.get("retry")
            return validate_retry_settings(
                values,
                current=retry_settings_view(defaults),
            )
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"[settings] 加载失败（忽略，使用 config.yaml）: {exc}")
            return None

    def save(self, values: dict[str, int | float]) -> None:
        normalized = validate_retry_settings(values)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"retry": normalized}, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[settings] 保存失败: {exc}")
            raise
