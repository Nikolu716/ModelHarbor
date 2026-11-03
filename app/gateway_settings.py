"""Account-scoped retry, probe, scoring and log settings with atomic persistence."""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .config import GatewayConfig
from .scoring import validate_speed_weight


RETRY_SETTING_FIELDS = (
    "max_attempts",
    "retry_backoff",
    "max_rounds",
    "round_backoff",
    "max_round_backoff",
    "max_retry_seconds",
)


DEFAULT_EVENT_RETENTION = 500
MAX_EVENT_RETENTION = 10_000


def scoring_settings_view(gateway: GatewayConfig) -> dict[str, float]:
    return {"scoring_speed_weight": gateway.scoring_speed_weight}


def validate_scoring_settings(values: object) -> dict[str, float]:
    if not isinstance(values, dict) or set(values) != {"scoring_speed_weight"}:
        raise ValueError("评分设置须包含且仅包含 scoring_speed_weight")
    return {"scoring_speed_weight": validate_speed_weight(values["scoring_speed_weight"])}


def retry_settings_view(gateway: GatewayConfig) -> dict[str, int | float]:
    """Return the public, non-secret retry configuration."""
    return {field: getattr(gateway, field) for field in RETRY_SETTING_FIELDS}


def probe_settings_view(gateway: GatewayConfig) -> dict[str, int | float | bool]:
    return {
        "generation_probe_enabled": gateway.generation_probe_enabled,
        "generation_probe_interval": gateway.generation_probe_interval,
    }


def validate_probe_settings(
    values: object,
    *,
    current: dict[str, int | float | bool] | None = None,
) -> dict[str, int | float | bool]:
    if not isinstance(values, dict):
        raise ValueError("探测设置必须是 JSON 对象")
    fields = {"generation_probe_enabled", "generation_probe_interval"}
    unknown = sorted(set(values) - fields)
    if unknown:
        raise ValueError(f"不支持的探测字段: {', '.join(unknown)}")
    result = dict(current or {})
    result.update(values)
    if set(result) != fields:
        raise ValueError("探测设置缺少必需字段")
    enabled = result["generation_probe_enabled"]
    if not isinstance(enabled, bool):
        raise ValueError("generation_probe_enabled 必须是布尔值")
    interval = result["generation_probe_interval"]
    if isinstance(interval, bool) or not isinstance(interval, (int, float)):
        raise ValueError("generation_probe_interval 必须是数字")
    interval = float(interval)
    if not math.isfinite(interval) or not 300 <= interval <= 86400:
        raise ValueError("generation_probe_interval 必须在 300 到 86400 秒之间")
    return {
        "generation_probe_enabled": enabled,
        "generation_probe_interval": interval,
    }


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
    """Persist account policies without exposing unrelated gateway secrets."""

    def __init__(self, data_dir: str):
        self.path = Path(data_dir) / "gateway-settings.json"

    def load_scoring(self) -> dict[str, float] | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            values = payload.get("scoring")
            return validate_scoring_settings(values) if values is not None else None
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"[settings] 评分设置加载失败（使用配置默认值）: {exc}")
            return None

    def save_scoring(self, values: dict[str, float]) -> None:
        normalized = validate_scoring_settings(values)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Preserve retry/probe/log settings and only activate after the atomic write.
        payload = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(payload, dict):
            raise ValueError("settings root must be an object")
        payload["scoring"] = normalized
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

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

    def load_probe(self, defaults: GatewayConfig) -> dict[str, int | float | bool] | None:
        if not self.path.exists():
            return None
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("根节点必须是对象")
            values = payload.get("probe")
            if values is None:
                return None
            return validate_probe_settings(
                values,
                current=probe_settings_view(defaults),
            )
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"[settings] 探测设置加载失败（忽略，使用 config.yaml）: {exc}")
            return None

    def load_logs(self, default: int = DEFAULT_EVENT_RETENTION) -> int:
        if not self.path.exists():
            return default
        try:
            payload: Any = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("settings root must be an object")
            value = payload.get("event_retention", default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("event_retention must be an integer")
            if not 100 <= value <= MAX_EVENT_RETENTION:
                raise ValueError("event_retention is outside the supported range")
            return value
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
            print(f"[settings] event log retention load failed; using default: {exc}")
            return default

    def save_logs(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("event_retention must be an integer")
        if not 100 <= value <= MAX_EVENT_RETENTION:
            raise ValueError("event_retention must be between 100 and 10000")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload: dict[str, Any] = {}
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        payload = loaded
                except (json.JSONDecodeError, OSError):
                    pass
            payload["event_retention"] = value
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[settings] event log retention save failed: {exc}")
            raise

    def save(self, values: dict[str, int | float]) -> None:
        normalized = validate_retry_settings(values)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload: dict[str, Any] = {}
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        payload = loaded
                except (json.JSONDecodeError, OSError):
                    pass
            payload["retry"] = normalized
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[settings] 保存失败: {exc}")
            raise

    def save_probe(self, values: dict[str, int | float | bool]) -> None:
        normalized = validate_probe_settings(values)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload: dict[str, Any] = {}
            if self.path.exists():
                try:
                    loaded = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict):
                        payload = loaded
                except (json.JSONDecodeError, OSError):
                    pass
            payload["probe"] = normalized
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[settings] 探测设置保存失败: {exc}")
            raise
