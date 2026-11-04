"""Native usage limits: upstream waits and passive, single-request recovery.

Metadata can lengthen a wait but cannot prove that a model call will succeed.
No policy in this module sends a generation or authentication request.
"""
import math

RETRY_INTERVAL = 900.0
MAX_RETRY_INTERVAL = 21600.0


def native_usage_error(cfg, code, message):
    return (cfg.provider == "chatgpt" and cfg.chatgpt_mode in {"codex_relay", "codex_cli"}
            and not any(text in message.lower() for text in ("subscription_sharing_", "chatpass_v2_"))
            and (code == "usage_limit_reached" or "usage_limit_reached" in message.lower()
                 or "http 429" in message.lower() and "usage limit" in message.lower()))


def positive(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else 0.0


def metadata_wait(usage, since, limit_id, now):
    """Return a documented blocking reset, never infer recovery from spare %."""
    updated = positive(usage.get("updated_at"))
    if (usage.get("status") != "connected" or usage.get("stale") or usage.get("error")
            or updated <= since or now - updated >= 1800):
        return 0.0
    until = 0.0
    for window in usage.get("windows") or []:
        if not isinstance(window, dict) or limit_id and window.get("limit_id") != limit_id:
            continue
        percent = positive(window.get("used_percent"))
        if percent >= 100:
            reset = positive(window.get("resets_at"))
            # An expired snapshot is not evidence for another full wait.
            until = max(until, reset if reset else updated + RETRY_INTERVAL)
    for bucket in usage.get("limits") or []:
        if not isinstance(bucket, dict) or limit_id and bucket.get("limit_id") != limit_id:
            continue
        reached = bucket.get("reached_type")
        if reached and str(reached).lower() not in {"none", "not_reached"}:
            until = max(until, updated + RETRY_INTERVAL)
    return until if until > now else 0.0
