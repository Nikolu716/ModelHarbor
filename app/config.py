"""配置加载与校验。

从 config.yaml 读取网关全局设置与中转站列表。
"""
from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .access_control import validate_allowed_ips
from .scoring import SPEED_WEIGHT, validate_speed_weight

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
    probe_interval: float = 900.0
    fail_threshold: int = 3
    cool_down: float = 60.0
    scoring_speed_weight: float = SPEED_WEIGHT  # 0–1; each account can override at runtime.
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
    # Allowed source IPs for all routes, including local clients; [] denies all.
    allowed_ips: list[str] = field(default_factory=lambda: ["127.0.0.1", "::1"])
    # Optional stable URL shown to clients; useful when the dashboard is opened via loopback.
    public_url: str = ""
    # Token-generating background probes require explicit global and per-station opt-in.
    generation_probe_enabled: bool = False
    generation_probe_interval: float = 900.0
    # 流式 SSE 保活间隔（秒，0 = 关闭）：长流定期发注释行防止客户端空闲超时
    sse_keepalive_interval: float = 60.0
    juice_probe_interval: float = 600.0  # 单token指纹巡检间隔（只测使用中站点）
    data_dir: str = "data"
    data_retention_days: int = 365  # 按天统计/指纹保留天数（自动裁剪，防无限增长）

    @classmethod
    def from_dict(cls, d: dict) -> "GatewayConfig":
        allowed = {f.name for f in dataclasses.fields(cls)}
        cfg = cls(**{k: v for k, v in (d or {}).items() if k in allowed})
        if isinstance(cfg.port, bool) or not isinstance(cfg.port, int) or not 1 <= cfg.port <= 65535:
            raise ValueError("gateway.port must be an integer between 1 and 65535")
        cfg.allowed_ips = validate_allowed_ips(cfg.allowed_ips)
        cfg.scoring_speed_weight = validate_speed_weight(cfg.scoring_speed_weight)
        if not isinstance(cfg.public_url, str):
            raise ValueError("gateway.public_url must be a string")
        cfg.public_url = cfg.public_url.strip().rstrip("/")
        if cfg.public_url:
            from urllib.parse import urlsplit
            parsed_url = urlsplit(cfg.public_url)
            if (parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc
                    or parsed_url.username or parsed_url.password or parsed_url.query
                    or parsed_url.fragment or parsed_url.path not in {"", "/", "/v1"}):
                raise ValueError("gateway.public_url must be a base http(s) URL")
            try:
                explicit_port = parsed_url.port
            except ValueError as exc:
                raise ValueError("gateway.public_url contains an invalid port") from exc
            if explicit_port is not None and not 1 <= explicit_port <= 65535:
                raise ValueError("gateway.public_url port must be between 1 and 65535")
            if parsed_url.path == "/v1":
                cfg.public_url = cfg.public_url[:-3]
        if isinstance(cfg.max_rounds, bool) or not isinstance(cfg.max_rounds, int) \
                or cfg.max_rounds < 0:
            raise ValueError("gateway.max_rounds must be an integer >= 0")

        def non_negative_number(value: object, name: str) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise ValueError(f"gateway.{name} must be a finite number >= 0")
            return float(value)

        if not isinstance(cfg.generation_probe_enabled, bool):
            raise ValueError("gateway.generation_probe_enabled must be a boolean")
        cfg.generation_probe_interval = non_negative_number(
            cfg.generation_probe_interval, "generation_probe_interval")
        if not 300 <= cfg.generation_probe_interval <= 86400:
            raise ValueError(
                "gateway.generation_probe_interval must be between 300 and 86400 seconds")

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
    model_routing: str = "catalog"  # catalog: exact known models; passthrough: explicit opt-in.
    route_models: list[str] = field(default_factory=list)  # Optional exact IDs or trailing-* prefixes.
    provider: str = "openai"  # openai-compatible or OpenCode's documented native endpoints.
    opencode_plan: str = "balance"  # balance / go; never fall back between billing sources.
    priority: int = 5
    priority_auto: bool = False  # Legacy stations keep their manual score bias.
    weight: float = 1.0
    timeout: float = 300.0
    max_concurrency: int = 20
    probe_models: bool = False
    probe_path: str = "/models"
    probe_interval: float | None = None  # None 时使用 gateway.probe_interval
    probe_chat: bool = False  # Background generation also requires the global opt-in.
    probe_chat_path: str = "/chat/completions"  # 探测端点（responses-only 站用 /responses）
    probe_chat_model: str = ""  # Empty: first discovered model in upstream order, then configured models.
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
        if self.provider == "gemini":
            return url if url.endswith(("/v1beta", "/v1")) else url + "/v1beta"
        if self.provider == "opencode":
            prefix = "go/" if self.opencode_plan == "go" else ""
            return f"https://opencode.ai/inference/{prefix}openai/v1"
        if not url.endswith("/v1"):
            url += "/v1"
        return url

    @property
    def models_url(self) -> str:
        if self.provider == "opencode":
            prefix = "go/" if self.opencode_plan == "go" else ""
            return f"https://opencode.ai/inference/{prefix}v1/models"
        return self.api_base + "/models"

    @classmethod
    def from_dict(cls, name: str, d: dict) -> "StationConfig":
        allowed = {f.name for f in dataclasses.fields(cls)} - {"name"}
        kw = {k: v for k, v in (d or {}).items() if k in allowed}
        if "model_routing" not in kw:
            # Preserve legacy wildcard stations on reload; new API/UI stations set catalog explicitly.
            kw["model_routing"] = "catalog" if kw.get("models") or kw.get("probe_models") else "passthrough"
        if "opencode_plan" not in kw and str(kw.get("base_url", "")).startswith("https://opencode.ai/inference/go/"):
            kw["opencode_plan"] = "go"
        cfg = cls(name=name, **kw)
        if cfg.provider not in ("openai", "opencode", "anthropic", "gemini"):
            raise ValueError("provider 必须是 openai、opencode、anthropic 或 gemini")
        if cfg.provider in ("anthropic", "gemini"):
            cfg.probe_balance = False
            cfg.wire_api = "auto"
            cfg.probe_chat_path = "/messages" if cfg.provider == "anthropic" else "/generateContent"
        if cfg.opencode_plan not in ("balance", "go"):
            raise ValueError("OpenCode 套餐必须是 balance 或 go")
        if cfg.provider == "opencode":
            if not isinstance(cfg.base_url, str) or cfg.base_url.strip().rstrip("/") not in {
                    "https://opencode.ai", "https://opencode.ai/inference",
                    "https://opencode.ai/inference/v1", "https://opencode.ai/inference/openai/v1",
                    "https://opencode.ai/inference/go/v1", "https://opencode.ai/inference/go/openai/v1"}:
                raise ValueError("OpenCode 必须使用官方 inference 地址")
            cfg.base_url = cfg.api_base
            cfg.billing = "subscription" if cfg.opencode_plan == "go" else "balance"
            if cfg.opencode_plan == "go":
                cfg.probe_chat = False  # No synthetic ping traffic on the coding subscription.
            cfg.probe_models = True
            cfg.probe_path = "/models"
            cfg.probe_balance = False  # No documented credit_grants endpoint.
            cfg.model_routing = "catalog"
            cfg.wire_api = "auto"
        if cfg.model_routing not in ("catalog", "passthrough"):
            raise ValueError("model_routing 必须是 catalog 或 passthrough")
        if not isinstance(cfg.route_models, list) or len(cfg.route_models) > 500 or any(
                not isinstance(model, str) or not model.strip() or len(model) > 200
                or any(ord(c) < 32 or ord(c) == 127 for c in model) or "*" in model.strip().rstrip("*")
                or model.count("*") > 1 for model in cfg.route_models):
            raise ValueError("路由模型须为模型 ID 或末尾带 * 的前缀，最多 500 项")
        cfg.route_models = list(dict.fromkeys(model.strip() for model in cfg.route_models))
        if not isinstance(cfg.priority_auto, bool):
            raise ValueError("priority_auto 必须是布尔值")
        if cfg.billing not in ("balance", "subscription"):
            raise ValueError(f"中转站 [{name}] billing 必须是 balance 或 subscription")
        if not isinstance(cfg.probe_chat_model, str):
            raise ValueError(f"中转站 [{name}] probe_chat_model 必须是字符串")
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
