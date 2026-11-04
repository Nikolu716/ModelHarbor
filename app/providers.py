"""Provider catalog capabilities; never follow URLs supplied by model metadata."""
from __future__ import annotations

from . import __version__

OPENCODE_SESSION_HEADERS = ("x-opencode-session", "session-id")
CLIENT_SESSION_ALIASES = ("x-session-id",)
CODEX_CONTEXT_HEADERS = ("session-id", "x-session-id", "thread-id", "x-codex-turn-state", "x-codex-turn-metadata")
CLIENT_CONTEXT_HEADERS = (*OPENCODE_SESSION_HEADERS, *CLIENT_SESSION_ALIASES,
                          "thread-id", "user-agent", "anthropic-version", "anthropic-beta",
                          "x-codex-turn-state", "x-codex-turn-metadata")


class MissingOpenCodeSession(ValueError):
    pass


def request_context(headers) -> dict[str, str]:
    """Carry an explicit context allowlist, never the gateway bearer credential."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    return {k: lowered[k] for k in CLIENT_CONTEXT_HEADERS if lowered.get(k)}


def inference_headers(cfg, client_headers=None, *, test_session: str | None = None, path=None, catalog=False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **cfg.headers,
    }
    context = request_context(client_headers)
    if cfg.provider == "chatgpt" and cfg.chatgpt_mode in ("codex_cli", "codex_relay"):
        return {**headers, **{key: value for key, value in context.items() if key in CODEX_CONTEXT_HEADERS}}
    if cfg.provider in ("anthropic", "gemini"):
        headers = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "x-api-key", "x-goog-api-key")}
        headers["x-api-key" if cfg.provider == "anthropic" else "x-goog-api-key"] = cfg.api_key
    if cfg.provider == "anthropic" or path == "/messages":
        headers = {k: v for k, v in headers.items() if k.lower() not in ("anthropic-version", "anthropic-beta")}
        configured = {k.lower(): v for k, v in cfg.headers.items()}
        headers["anthropic-version"] = context.get("anthropic-version", configured.get("anthropic-version", "2023-06-01"))
        beta = context.get("anthropic-beta", configured.get("anthropic-beta"))
        if beta:
            headers["anthropic-beta"] = beta
    if cfg.provider != "opencode" or catalog:
        return headers
    # A static configured session must never combine unrelated conversations.
    context = {key: value for key, value in context.items() if key not in ("x-codex-turn-state", "x-codex-turn-metadata")}
    headers = {k: v for k, v in headers.items()
               if k.lower() not in (*OPENCODE_SESSION_HEADERS, *CLIENT_SESSION_ALIASES, "thread-id", "user-agent")}
    # ZCode sends its real conversation ID as x-session-id. Translate only
    # when no native session header is present; never invent a shared ID.
    client_session = context.pop("x-session-id", "").strip()
    if client_session and not any(context.get(k, "").strip() for k in OPENCODE_SESSION_HEADERS):
        context["x-opencode-session"] = client_session
    if test_session:
        context = {"x-opencode-session": test_session}
    if cfg.opencode_plan == "go" and not any(context.get(k, "").strip() for k in OPENCODE_SESSION_HEADERS):
        raise MissingOpenCodeSession(
            "OpenCode Go 需要稳定的对话会话 ID：请让客户端发送 x-opencode-session "
            "或客户端原生 session-id / x-session-id；同一对话保持不变，不同对话使用不同值。未向上游发送请求。")
    headers.update(context)
    headers.setdefault("user-agent", "ModelHarbor/" + __version__)
    if headers["user-agent"].lower().startswith(("python-", "openai/", "go-http-client", "curl/", "httpx/")):
        headers["user-agent"] = "ModelHarbor/" + __version__ + " via " + headers["user-agent"]
    return headers


def opencode_capabilities(items: list, plan: str = "balance") -> tuple[dict[str, list[str]], dict[str, str]]:
    paths, unavailable = {}, {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("OpenCode catalog must include model metadata")
        model = item["id"].strip()
        api = item.get("api") or {}
        if not isinstance(api, dict):
            raise ValueError("Invalid model API capabilities")
        supported = [path for key, path in (
            ("responses", "/responses"), ("chat-completions", "/chat/completions"),
            ("messages", "/messages"), ("generate-content", "/generateContent")) if key in api]
        # Go Console translates all text models across its API families.
        # Preserve the client's full body; no local lossy tool conversion.
        # Keep the native OpenAI API first for generation tests when present.
        if plan == "go" and item.get("type") == "chat" and any(
                key in api for key in ("responses", "chat-completions", "messages", "generate-content")):
            supported = list(dict.fromkeys(supported + ["/responses", "/chat/completions", "/messages", "/generateContent"]))
        cost = item.get("cost") or {}
        if not isinstance(cost, dict):
            raise ValueError("Invalid model cost metadata")
        text_cost = cost.get("text") or {}
        if not isinstance(text_cost, dict):
            raise ValueError("Invalid text cost metadata")
        input_cost, output_cost = text_cost.get("input") or {}, text_cost.get("output") or {}
        if not isinstance(input_cost, dict) or not isinstance(output_cost, dict):
            raise ValueError("Invalid token cost metadata")
        input_price, output_price = input_cost.get("price"), output_cost.get("price")
        if plan != "go" and (model.endswith("-free") or (item.get("type") == "chat" and input_price == 0 and output_price == 0)):
            unavailable[model] = "免费聊天模型仅限 OpenCode 客户端"
        elif not supported or item.get("type") != "chat":
            unavailable[model] = "当前网关未接入此模型的原生协议"
        else:
            paths[model] = supported
    return paths, unavailable
