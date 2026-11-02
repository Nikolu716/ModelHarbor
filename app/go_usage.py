"""Read-only OpenCode Go quota snapshots; never generate content to measure usage."""
from __future__ import annotations

import math
import time
from datetime import datetime

URL = "https://opencode.ai/inference/go/v1/usage"
AUTO_INTERVAL = 900
MANUAL_INTERVAL = 60
MAX_RESPONSE_BYTES = 65536


def normalize_usage(data):
    raw = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise ValueError("上游未返回订阅用量")
    windows = {}
    for key, label in (("rolling", "5 小时"), ("weekly", "本周"), ("monthly", "本订阅周期")):
        item = raw.get(key)
        if item is None:
            windows[key] = {"label": label, "percent": None, "status": "unknown", "resets_at": None}
            continue
        if not isinstance(item, dict):
            raise ValueError("上游订阅用量格式无效")
        percent = item.get("percent")
        if percent is not None and (isinstance(percent, bool) or not isinstance(percent, (int, float))
                                    or not math.isfinite(percent) or percent < 0):
            raise ValueError("上游用量百分比无效")
        reset = item.get("resetsAt")
        if reset is not None:
            try:
                parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError()
                reset = parsed.timestamp()
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise ValueError("上游用量重置时间无效") from None
        status = item.get("status")
        windows[key] = {"label": label, "percent": percent,
                        "status": status[:40] if isinstance(status, str) else "unknown", "resets_at": reset}
    if not any(v["percent"] is not None for v in windows.values()):
        raise ValueError("上游尚未提供可用的订阅额度")
    return windows


def usage_view(st):
    if st.cfg.provider != "opencode" or st.cfg.opencode_plan != "go":
        return None
    now = time.time()
    return {"source": "opencode", "windows": st.go_usage,
            "updated_at": st.go_usage_updated_at, "checked_at": st.go_usage_checked_at,
            "next_refresh_at": st.go_usage_next_at,
            "manual_refresh_at": max(st.go_usage_checked_at + MANUAL_INTERVAL, st.go_usage_retry_at),
            "status": st.go_usage_status, "error": st.go_usage_error,
            "stale": bool(st.go_usage) and (now - st.go_usage_updated_at > AUTO_INTERVAL or st.go_usage_status != "ready"),
            "refresh_interval": AUTO_INTERVAL}
