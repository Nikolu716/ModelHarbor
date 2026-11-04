"""Juice（模型匹配度）检测：单 token 分布指纹（第二层）。

原理（One Token Is Enough, arXiv:2607.10252）：让模型"报 1-100 随机数"等
单 token 任务，聚合答案分布即为模型指纹；相同模型指纹距离远小于不同模型。
无需 logprobs，8 次探针即可粗判，与官方基线或同模型多站互比。

本实现：
- 指纹按 (站点, 模型) 存储，只在站点"使用中"（最近有成功流量）巡检
- 同模型多站互比 Jensen-Shannon 散度：JSD > 阈值 → 疑似后端不一致
- 单站无对比对象时仅存档（不妄下结论）
"""
from __future__ import annotations

import math
import re
import time

# 指纹探针 prompt（多语言/多任务均匀混排，控制 token 便宜）
PROMPTS = [
    "从1到100之间选一个随机数，只输出数字",
    "Pick a random number between 1 and 100, output only the number",
    "报一个1到100的随机整数，只写数字",
    "Choose a random integer from 1 to 100, digits only",
    "给我一个1-100的随机数，只要数字",
    "Say a random number from 1 to 100, just the number",
    "随机输出一个1到100之间的数子，仅数字",
    "Reply with a random number between 1 and 100 only",
]

OTHER_BUCKET = "__other__"

# ------------------------------------------------------------------
# 第一层：被动统计（上游头家族透视 + model 回显比对）
# ------------------------------------------------------------------
_FAMILY_RULES = [
    ("anthropic", ("anthropic-ratelimit", "anthropic-organization")),
    ("bedrock", ("x-amzn-requestid", "x-aws-")),
    ("vertex", ("x-vertex-", "x-goog-")),
    ("openai", ("openai-processing-ms", "x-ratelimit-", "x-request-id")),
    ("oneapi", ("x-oneapi-request-id",)),
    ("cloudflare", ("cf-ray",)),
]
UNKNOWN_FAMILY = "unknown"


def header_family(headers) -> str:
    """从响应头判断真实上游家族。"""
    if headers is None:
        return UNKNOWN_FAMILY
    for family, keywords in _FAMILY_RULES:
        for kw in keywords:
            for key in headers.keys():
                if key.lower().startswith(kw):
                    return family
    return UNKNOWN_FAMILY


def response_model(content: bytes) -> str | None:
    """从响应体提取 model 字段（chat/responses/SSE 兼容）。"""
    for obj in _iter_json_payloads(content):
        m = obj.get("model")
        if isinstance(m, str) and m:
            return m
    return None


def juice_score(st, fingerprint_flag: str = "") -> int:
    """juice 匹配度（0-100）。第一层被动信号 + 第二层指纹信号综合。

    - model 回显不一致（实锤）：-30
    - 上游头家族异常（声称 OpenAI 系却出现 anthropic/bedrock/vertex）：-30
    - 指纹与同模型其他站 JSD 超阈值（疑似后端不一致）：-25
    样本采集中的指纹不扣分；无对比对象不扣分。
    """
    score = 100.0
    if getattr(st, "model_mismatch", 0) > 0:
        score -= 30.0
    if getattr(getattr(st, "cfg", None), "provider", "openai") == "openai":
        for fam in ("anthropic", "bedrock", "vertex"):
            if st.upstream_families.get(fam, 0) > 0:
                score -= 30.0
    if fingerprint_flag == "mismatch":
        score -= 25.0
    return max(0, min(100, int(round(score))))

_NUM_RE = re.compile(r"\d+")


def parse_single_token(text: str) -> str:
    """从单 token 响应中归一化出一个桶。"""
    if not text:
        return OTHER_BUCKET
    # 取响应中的最后一个数字（多数模型数字在末尾）
    numbers = _NUM_RE.findall(text)
    if not numbers:
        return OTHER_BUCKET
    try:
        n = int(numbers[-1])
    except ValueError:
        return OTHER_BUCKET
    if not (1 <= n <= 100):
        return OTHER_BUCKET
    return str(n)


def _kl(p: dict, q: dict) -> float:
    total = 0.0
    for key, prob in p.items():
        if prob > 0:
            total += prob * math.log2(prob / q[key])
    return total


def jsd(dist_a: dict, dist_b: dict) -> float:
    """Jensen-Shannon 散度（base-2，0~1 bit）。"""
    keys = set(dist_a) | set(dist_b)
    mid = {}
    for k in keys:
        mid[k] = (dist_a.get(k, 0.0) + dist_b.get(k, 0.0)) / 2.0
    return 0.5 * (_kl(dist_a, mid) + _kl(dist_b, mid))


def normalize(dist: dict) -> dict:
    """计数 → 归一化概率分布。"""
    total = sum(dist.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in dist.items()}


class Fingerprint:
    """单个 (站点, 模型) 的指纹（真实响应特征分布，顺路采集）。"""

    __slots__ = ("station", "model", "counts", "samples", "updated_at", "jsd", "flag")

    def __init__(self, station: str, model: str):
        self.station = station
        self.model = model
        self.counts: dict[str, int] = {}
        self.samples = 0
        self.updated_at = 0.0
        # 与同模型其他站指纹的 JSD；None=暂无对比
        self.jsd: float | None = None
        self.flag = ""  # "match"/"mismatch"/"no_peer"/"collecting"

    def add_feature(self, text: str) -> None:
        """真实响应文本 → 特征桶（首字符类型 + 长度区间）。"""
        self.counts[response_feature(text)] = self.counts.get(
            response_feature(text), 0) + 1
        self.samples += 1

    def add(self, bucket: str) -> None:
        self.counts[bucket] = self.counts.get(bucket, 0) + 1
        self.samples += 1

    def distribution(self) -> dict:
        return normalize(self.counts)


def _iter_json_payloads(content: bytes):
    """提取内容中的所有 JSON payload：兼容单 JSON 与 SSE(data: 行) 格式。"""
    text = content.decode("utf-8", errors="replace")
    if "data:" in text:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            import json as _json
            try:
                obj = _json.loads(payload)
            except _json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj
    else:
        import json as _json
        try:
            obj = _json.loads(text)
        except _json.JSONDecodeError:
            return
        if isinstance(obj, dict):
            yield obj


def _extract_text_from_obj(obj: dict) -> str:
    """从单个响应对象提取输出文本（chat/responses 兼容）。"""
    from .protocols import native_text
    native = native_text(obj)
    if native:
        return native
    if obj.get("type") == "content_block_delta":
        return obj.get("delta", {}).get("text", "")
    for choice in obj.get("choices", []) or []:
        if isinstance(choice, dict):
            msg = choice.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("content"), str):
                return msg["content"]
    if obj.get("type") == "response.output_text.delta":
        d = obj.get("delta")
        if isinstance(d, str) and d:
            return d
    output = obj.get("output") or []
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and isinstance(item.get("content"), list):
                for c in item["content"]:
                    if isinstance(c, dict) and c.get("type") == "output_text"                             and isinstance(c.get("text"), str):
                        return c["text"]
    return ""


def response_text(content: bytes) -> str:
    """从响应体提取输出文本（chat/responses 兼容）。返回空串表示不可提取。"""
    import json as _json
    for obj in _iter_json_payloads(content):
        text = _extract_text_from_obj(obj)
        if text:
            return text
    return ""


def response_feature(text: str) -> str:
    """真实响应文本的特征桶：首字符类别 + 长度区间。"""
    text = (text or "").strip()
    if not text:
        return "empty"
    first = text[0]
    if first.isdigit():
        cat = "digit"
    elif first.isalpha():
        cat = "alpha"
    elif "一" <= first <= "鿿":
        cat = "chinese"
    elif first in "，。！？、；：,.!?;:'\"":
        cat = "punct"
    else:
        cat = "other"
    ln = len(text)
    length = "lt10" if ln < 10 else ("lt50" if ln < 50
            else ("lt200" if ln < 200 else "ge200"))
    return f"{cat}_{length}"


class JuiceRegistry:
    """全部指纹仓库 + 同模型对比计算（持久化到 data/juice.json）。"""

    def __init__(self, threshold: float = 0.35,
                 persist_path: str | None = None):
        self.threshold = threshold
        self.fingerprints: dict[str, Fingerprint] = {}
        self.persist_path = persist_path
        if persist_path:
            import json as _json
            import os
            try:
                with open(persist_path, encoding="utf-8") as stream:
                    data = _json.load(stream)
                for key, row in data.get("fingerprints", data).items():
                    if not isinstance(row, dict):
                        continue  # 跳过损坏行
                    try:
                        fp = Fingerprint(row["station"], row["model"])
                        fp.counts = row["counts"]
                        fp.samples = row["samples"]
                        fp.updated_at = row["updated_at"]
                        self.fingerprints[key] = fp
                    except (KeyError, TypeError):
                        continue
                print(f"[juice] 已加载 {len(self.fingerprints)} 个指纹")
            except (OSError, KeyError, ValueError):
                pass

    def persist(self, retention_days: int = 30) -> None:
        if not self.persist_path:
            return
        self.prune(retention_days=retention_days)
        import json as _json
        import os
        try:
            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)
            tmp = self.persist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump({
                    "threshold": self.threshold,
                    "fingerprints": {
                        key: {"station": fp.station, "model": fp.model,
                              "counts": fp.counts, "samples": fp.samples,
                              "updated_at": fp.updated_at}
                        for key, fp in self.fingerprints.items()
                    },
                }, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.persist_path)
        except OSError as e:
            print(f"[juice] 指纹持久化失败: {e}")

    def _key(self, station: str, model: str) -> str:
        return f"{station}:::{model}"

    def get(self, station: str, model: str) -> Fingerprint:
        key = self._key(station, model)
        fp = self.fingerprints.get(key)
        if fp is None:
            fp = Fingerprint(station, model)
            self.fingerprints[key] = fp
        return fp

    def refresh_flags(self) -> None:
        """同模型多站互比：更新每个指纹的 JSD 与 flag。"""
        # 按模型分组
        by_model: dict[str, list[Fingerprint]] = {}
        for fp in self.fingerprints.values():
            if fp.samples < 4:
                continue  # 样本太少不参与比对
            by_model.setdefault(fp.model, []).append(fp)

        for fps in by_model.values():
            n = len(fps)
            if n < 2:
                for fp in fps:
                    fp.jsd = None
                    fp.flag = "no_peer"
                continue
            # 两两矩阵 → 多数派判定（少数服从多数）：
            # 与超过半数其他站 JSD<=阈值 → 正常；孤立/少数 → 疑似掺水
            for fp in fps:
                others = [o for o in fps if o is not fp]
                dists = [jsd(fp.distribution(), o.distribution()) for o in others]
                # N=2 冲突：无法裁定谁真（证据不足），标 conflict 不扣分
                if n == 2 and max(dists) > self.threshold:
                    fp.flag = "conflict"
                    fp.jsd = round(max(dists), 4)
                    continue
                agree = sum(1 for d in dists if d <= self.threshold)
                need = max(1, (n - 1) // 2 + (1 if (n - 1) % 2 else 0))
                if agree >= need:
                    fp.flag = "match"
                    # JSD 取同意者的中位
                    agreed = sorted(d for d in dists if d <= self.threshold)
                    fp.jsd = round(agreed[len(agreed) // 2], 4) if agreed else 0.0
                else:
                    fp.flag = "mismatch"
                    dists_sorted = sorted(dists)
                    fp.jsd = round(dists_sorted[len(dists_sorted) // 2], 4)

    def prune(self, retention_days: int = 30,
              active_stations: set[str] | None = None) -> int:
        """清理过期指纹（N 天未更新）与已删除站点的残留。返回清理数。"""
        import time as _t
        now = _t.time()
        stale = []
        for key, fp in self.fingerprints.items():
            if fp.updated_at <= 0:
                stale.append(key)
            elif now - fp.updated_at > retention_days * 86400:
                stale.append(key)
            elif active_stations is not None and fp.station not in active_stations:
                stale.append(key)
        for key in stale:
            del self.fingerprints[key]
        if stale:
            print(f"[juice] 已清理 {len(stale)} 个过期指纹")
        return len(stale)

    def snapshot(self) -> dict:
        """每站模型级指纹概要（供界面）。"""
        out: dict[str, list] = {}
        for fp in self.fingerprints.values():
            out.setdefault(fp.station, []).append({
                "model": fp.model,
                "samples": fp.samples,
                "updated_at": fp.updated_at,
                "jsd": fp.jsd,
                "flag": fp.flag or ("" if fp.samples >= 4 else "collecting"),
            })
        # 时间倒序：最新在前
        for lst in out.values():
            lst.sort(key=lambda x: -x["updated_at"])
        return out
