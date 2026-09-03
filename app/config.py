"""配置加载与校验。

从 config.yaml 读取网关全局设置与中转站列表。
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_CONFIG = "config.yaml"


@dataclass
class GatewayConfig:
    host: str = "0.0.0.0"
    port: int = 8000
    max_attempts: int = 3
    retry_backoff: float = 0.3
    max_rounds: int = 0  # 0 表示不限轮次
    round_backoff: float = 1.0  # 轮间基础退避秒数（第 N 轮前等待 round_backoff * 2^(N-1)）
    max_round_backoff: float = 10.0  # 轮间退避上限
    max_retry_seconds: float = 60.0  # 0 表示不限总时长（仅限有限轮次）
    probe_interval: float = 15.0
    fail_threshold: int = 3
    cool_down: float = 60.0
    connect_timeout: float = 10.0
    first_byte_timeout: float = 10.0
    first_token_timeout: float = 120.0  # 流式首个内容事件超时（推理型模型可达数十秒）
    read_timeout: float = 60.0
    rate_limit_cooldown: float = 30.0  # 429 限流后的短冷却秒数
    balance_probe_interval: float = 300.0  # 余额探测间隔（秒，GET 免费）
    balance_alert_threshold: float = 1.0  # 余额低于该值(美元)触发低余额预警
    quota_auto_recover: bool = True  # 配额耗尽后自动探测恢复，无需手动 reset
    quota_probe_interval: float = 60.0  # 额度恢复探测的起始间隔（秒）
    quota_probe_max_interval: float = 300.0  # 仍无额度时的探测间隔上限（秒）
    webhook_url: str = ""  # 挂机告警 webhook（留空关闭；支持企业微信/钉钉/Server酱等）
    # 对外开放：统一 key 列表（空 = 不鉴权）。客户端必须带 Authorization: Bearer <key>
    api_keys: list[str] = field(default_factory=list)
    # 管理接口 key（空 = 管理接口不鉴权；配置后 /admin/* 数据接口需带该 key）
    admin_key: str = ""
    # 流式 SSE 保活间隔（秒，0 = 关闭）：长流定期发注释行防止客户端空闲超时
    sse_keepalive_interval: float = 60.0
    juice_probe_interval: float = 600.0  # 单token指纹巡检间隔（只测使用中站点）
    data_dir: str = "data"
    data_retention_days: int = 30  # 按天统计/指纹保留天数（自动裁剪，防无限增长）

    @classmethod
    def from_dict(cls, d: dict) -> "GatewayConfig":
        allowed = {f.name for f in dataclasses.fields(cls)}
        cfg = cls(**{k: v for k, v in (d or {}).items() if k in allowed})
        if isinstance(cfg.max_rounds, bool) or not isinstance(cfg.max_rounds, int) \
                or cfg.max_rounds < 0:
            raise ValueError("gateway.max_rounds must be an integer >= 0")

        def non_negative_number(value: object, name: str) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise ValueError(f"gateway.{name} must be a finite number >= 0")
            return float(value)

        cfg.round_backoff = non_negative_number(cfg.round_backoff, "round_backoff")
        cfg.max_round_backoff = non_negative_number(
            cfg.max_round_backoff, "max_round_backoff")
        cfg.max_retry_seconds = non_negative_number(
            cfg.max_retry_seconds, "max_retry_seconds")
        if cfg.max_round_backoff <= 0:
            raise ValueError("gateway.max_round_backoff must be greater than 0")
        if cfg.max_round_backoff < cfg.round_backoff:
            raise ValueError(
                "gateway.max_round_backoff must be >= gateway.round_backoff")
        if cfg.max_rounds == 0 and cfg.max_retry_seconds == 0:
            raise ValueError(
                "max_rounds and max_retry_seconds cannot both be unlimited")
        if not isinstance(cfg.quota_auto_recover, bool):
            raise ValueError("gateway.quota_auto_recover must be a boolean")
        cfg.quota_probe_interval = non_negative_number(
            cfg.quota_probe_interval, "quota_probe_interval")
        cfg.quota_probe_max_interval = non_negative_number(
            cfg.quota_probe_max_interval, "quota_probe_max_interval")
        if cfg.quota_probe_interval <= 0:
            raise ValueError("gateway.quota_probe_interval must be greater than 0")
        if cfg.quota_probe_max_interval < cfg.quota_probe_interval:
            raise ValueError(
                "gateway.quota_probe_max_interval must be >= gateway.quota_probe_interval")
        return cfg


@dataclass
class StationConfig:
    name: str
    base_url: str
    api_key: str
    models: list[str] = field(default_factory=list)
    priority: int = 5
    weight: float = 1.0
    timeout: float = 300.0
    max_concurrency: int = 20
    probe_models: bool = False
    probe_path: str = "/models"
    probe_interval: float | None = None  # None 时使用 gateway.probe_interval
    probe_chat: bool = False  # 真实生成链路探测（极小成本，至少 60s 一次）
    probe_chat_path: str = "/chat/completions"  # 探测端点（responses-only 站用 /responses）
    probe_balance: bool = True  # 探测余额接口 credit_grants（不支持则静默降级）
    billing: str = "balance"  # 计费模式: balance(余额制) / subscription(订阅制)
    headers: dict[str, str] = field(default_factory=dict)
    # 该站支持的协议（auto 表示与入口协议一致，不做翻译）：
    #   auto / chat_completions / responses
    # 例：Codex 走 /responses 入口，但某站只实现 chat/completions，
    # 设 wire_api=chat_completions 后网关自动做双向翻译。
    wire_api: str = "auto"
    # 按站注入的缺省请求参数（不覆盖用户显式值），如 reasoning effort=max
    inject: dict = field(default_factory=dict)
    enabled: bool = True

    @property
    def api_base(self) -> str:
        """规范化的 API 基础地址，保证以 /v1 结尾。

        兼容用户写成 https://host 或 https://host/v1 两种形式。
        """
        url = self.base_url.rstrip("/")
        if not url.endswith("/v1"):
            url += "/v1"
        return url

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "StationConfig":
        allowed = {f.name for f in dataclasses.fields(cls)} - {"name"}
        kw = {k: v for k, v in (d or {}).items() if k in allowed}
        cfg = cls(name=name, **kw)
        if cfg.billing not in ("balance", "subscription"):
            raise ValueError(f"中转站 [{name}] billing 必须是 balance 或 subscription")
        return cfg


@dataclass
class AppConfig:
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    stations: list[StationConfig] = field(default_factory=list)

    @property
    def stations_by_name(self) -> dict[str, StationConfig]:
        return {s.name: s for s in self.stations}


def load_config(path: str = DEFAULT_CONFIG) -> AppConfig:
    """加载并校验配置文件。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p.resolve()}")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    cfg = AppConfig()
    cfg.gateway = GatewayConfig.from_dict(raw.get("gateway") or {})

    stations_raw = raw.get("stations") or []
    if not isinstance(stations_raw, list):
        raise ValueError("config.yaml 中 stations 必须是列表")

    for item in stations_raw:
        name = item.get("name")
        if not name:
            raise ValueError("每个中转站必须配置 name")
        if not item.get("base_url"):
            raise ValueError(f"中转站 [{name}] 缺少 base_url")
        if not item.get("api_key"):
            raise ValueError(f"中转站 [{name}] 缺少 api_key")
        cfg.stations.append(StationConfig.from_dict(name, item))

    names = [s.name for s in cfg.stations]
    if len(set(names)) != len(names):
        raise ValueError("中转站名称必须唯一")

    if not cfg.stations:
        raise ValueError("config.yaml 中至少需要一个中转站")

    return cfg
