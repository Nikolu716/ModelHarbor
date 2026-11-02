"""Native inference protocol routing. No metadata URLs or client keys are forwarded."""
from urllib.parse import quote

MESSAGES = "/messages"
GENERATE = "/generateContent"
STREAM_GENERATE = "/streamGenerateContent"
GEMINI_PATHS = (GENERATE, STREAM_GENERATE, STREAM_GENERATE + "?alt=sse")
OPENAI_PATHS = ("/responses", "/chat/completions", "/embeddings", "/completions")


def capability_path(path):
    return GENERATE if path in GEMINI_PATHS else path


def provider_supports(provider, path):
    if path is None:
        return True
    if provider == "anthropic":
        return path == MESSAGES
    if provider == "gemini":
        return path in GEMINI_PATHS
    return path in OPENAI_PATHS


def inference_url(cfg, path, model):
    """Use only configured roots and fixed protocol suffixes."""
    if cfg.provider == "opencode":
        prefix = "https://opencode.ai/inference/" + ("go/" if cfg.opencode_plan == "go" else "")
        base = prefix + ("anthropic/v1" if path == MESSAGES else "google/v1beta" if path in GEMINI_PATHS else "openai/v1")
    else:
        base = cfg.api_base
    if path in GEMINI_PATHS:
        if not isinstance(model, str) or not model or any(ch in model for ch in "/\\?#%") or model in (".", ".."):
            raise ValueError("Gemini 模型 ID 无效")
        return base + "/models/" + quote(model, safe="-._") + ":" + path[1:]
    return base + path


def native_text(obj):
    if not isinstance(obj, dict):
        return ""
    if isinstance(obj.get("content"), list):
        return "".join(part.get("text", "") for part in obj["content"] if isinstance(part, dict) and part.get("type") == "text")
    candidates = obj.get("candidates") or []
    if candidates and isinstance(candidates[0], dict):
        return "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])
                       if isinstance(part, dict) and not part.get("thought"))
    return ""


def error_body(status, message, path):
    if path == MESSAGES:
        kind = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error", 429: "rate_limit_error"}.get(status, "api_error")
        return {"type": "error", "error": {"type": kind, "message": message}}
    if path in GEMINI_PATHS:
        kind = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND", 429: "RESOURCE_EXHAUSTED", 503: "UNAVAILABLE"}.get(status, "INTERNAL")
        return {"error": {"code": status, "message": message, "status": kind}}
    return {"error": {"message": message, "type": "upstream_error"}}
