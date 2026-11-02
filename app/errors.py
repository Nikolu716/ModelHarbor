"""上游错误分类。

判断一个上游错误是否"可切换重试"：
- 可切换：401/403/404/408/429/5xx/超时/连接错误/容量不足 等站点侧问题
- 不可切换：400/422 等客户端请求本身的问题，重试其他站无意义
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# 这些 HTTP 状态码视为站点侧问题，可切换下一站重试
RETRYABLE_STATUS = {401, 403, 404, 408, 409, 429, 500, 501, 502, 503, 504}

# 客户端请求问题：直接返回，不重试
CLIENT_ERROR_STATUS = {400, 422}

# 确定性失败：重试也不会变好的错误（key 错/权限/模型不存在/客户端问题）。
# 站级切换仍会换站（其他站可能正常），但"整体重试轮次"对其无意义（fail fast）。
# 瞬时失败（429/5xx/超时/卡死/连接失败/容量）才会触发整体重试。
DETERMINISTIC_STATUS = {400, 401, 402, 403, 404, 422}

# 即使状态码不在上表（如 200 带错误体、400 报容量），命中关键词也视为可切换
CAPACITY_KEYWORDS = (
    "capacity", "overloaded", "overload", "high demand", "insufficient",
    "quota", "server busy", "temporarily", "try again later", "no available",
    "exhausted", "负载", "繁忙", "排队", "无余量", "容量", "超载", "爆满", "拥堵",
)

# 资金/配额问题关键词：命中即标记为"配额耗尽"（quota_exhausted）
# 余额制：402 / insufficient_quota / 余额不足 / 欠费
# 订阅制：subscription expired / inactive / 套餐到期 / 未续费
QUOTA_KEYWORDS = (
    "insufficient_quota", "quota exhausted", "out of quota", "quota",
    "balance", "insufficient balance", "billing", "payment required",
    "欠费", "余额", "额度", "配额", "充值", "计费", "已用完", "透支",
)
SUBSCRIPTION_KEYWORDS = (
    "subscription", "plan expired", "plan has expired", "trial",
    "renew", "inactive", "expired", "membership", "套餐", "订阅",
    "到期", "续费", "会员", "未激活", "已失效",
)

# 429 中的"用量限制"类关键词：部分站用 429 + USAGE_LIMIT_EXCEEDED
# 表示每日/总用量（额度）用尽，属于资金/配额问题而非速率限流
USAGE_LIMIT_KEYWORDS = (
    "usage_limit", "usage limit", "daily_limit", "daily limit",
    "usage_limit_exceeded", "用量", "额度用尽", "已达上限",
)

TIMEOUT_MSG = "请求超时（上游无响应）"
CONNECT_MSG = "连接失败"
STUCK_MSG = "上游卡死（无数据输出）"


@dataclass
class UpstreamError(Exception):
    """一次上游请求失败。retryable=False 表示不应再尝试其他站。

    quota=True 表示资金/配额层面问题（余额不足、订阅到期）：
    该站将被标记为 quota_exhausted 并从路由排除，后台自动探测直到额度恢复。

    deterministic=True 表示确定性失败（key 错/权限/模型不存在等），
    重试不会变好——站级切换仍会换站，但整体重试轮次会跳过它。
    """

    station: str
    status_code: int
    message: str
    retryable: bool = True
    quota: bool = False
    deterministic: bool = False

    def __str__(self) -> str:
        code = self.status_code or "-"
        return f"[{self.station}] HTTP {code}: {self.message}"


def _extract_message(payload: bytes | str) -> str:
    """从上游响应体中尽量提取 OpenAI 风格的 error.message。"""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    payload = payload.strip()
    if not payload:
        return "(空响应体)"
    try:
        data = json.loads(payload)
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if msg:
                return str(msg)[:500]
        elif isinstance(err, str) and err:
            return err[:500]
    except (json.JSONDecodeError, AttributeError):
        pass
    return payload[:500]


def _is_quota_error(status_code: int, lowered: str) -> bool:
    """判断是否为资金/配额层面问题（余额不足、订阅到期、用量限制等）。"""
    if status_code == 402:
        return True
    if status_code == 429:
        # 429 通常是速率限流（RPM/TPM），不判配额；
        # 但部分站用 429 + USAGE_LIMIT_EXCEEDED 表示每日/总用量（额度）用尽
        if any(k in lowered for k in QUOTA_KEYWORDS):
            return True
        if any(k in lowered for k in USAGE_LIMIT_KEYWORDS):
            return True
        return False
    if any(k in lowered for k in QUOTA_KEYWORDS):
        return True
    if status_code in (401, 403) and any(k in lowered for k in SUBSCRIPTION_KEYWORDS):
        # 订阅到期常表现为 401/403 + 订阅关键词
        return True
    return False


def classify_error(station: str, status_code: int, payload: bytes | str = "") -> UpstreamError:
    """根据状态码 + 响应体判断错误类型。"""
    message = _extract_message(payload)
    lowered = message.lower()
    quota = _is_quota_error(status_code, lowered)
    if quota:
        # Funding/entitlement cannot recover through immediate retries. Other
        # matching stations may still serve the request, but do not loop rounds.
        return UpstreamError(station, status_code, message,
                             retryable=True, quota=True, deterministic=True)
    # 确定性 = 状态码命中且消息不含容量关键词（容量关键词代表瞬时拥挤）
    deterministic = (status_code in DETERMINISTIC_STATUS
                     and not any(k in lowered for k in CAPACITY_KEYWORDS))

    if status_code in CLIENT_ERROR_STATUS:
        # 客户端错误默认不重试；但若命中容量关键词（如 400 报 quota），仍切换
        if any(k in lowered for k in CAPACITY_KEYWORDS):
            return UpstreamError(station, status_code, message,
                                 retryable=True, quota=quota,
                                 deterministic=False)
        return UpstreamError(station, status_code, message,
                             retryable=False, quota=quota,
                             deterministic=True)

    if status_code in RETRYABLE_STATUS:
        return UpstreamError(station, status_code, message,
                             retryable=True, quota=quota,
                             deterministic=deterministic)

    if status_code == 200 and any(k in lowered for k in CAPACITY_KEYWORDS):
        # 部分中转站用 200 + 错误体 表示容量不足
        return UpstreamError(station, status_code, message,
                             retryable=True, quota=quota,
                             deterministic=False)

    return UpstreamError(station, status_code, message,
                         retryable=True, quota=quota,
                         deterministic=deterministic)


def timeout_error(station: str, kind: str = TIMEOUT_MSG) -> UpstreamError:
    return UpstreamError(station, 0, kind, retryable=True)


def connect_error(station: str, detail: str = "") -> UpstreamError:
    msg = CONNECT_MSG + (f" ({detail})" if detail else "")
    return UpstreamError(station, 0, msg, retryable=True)


def first_byte_error(station: str) -> UpstreamError:
    """流式首字节超时 —— 站点卡死。"""
    return UpstreamError(station, 0, STUCK_MSG, retryable=True)
