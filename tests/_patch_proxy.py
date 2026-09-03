"""批量修改 proxy.py：cached_tokens + TTFT 接入。"""
import os

p = 'app/proxy.py'
s = open(p, encoding='utf-8').read()

# 1. _extract_tokens → _extract_usage (total, cached)
old = '''def _extract_tokens(content: bytes) -> int | None:
    """从完整响应体中提取 usage.total_tokens（chat/completions/embeddings/responses 兼容）。"""
    try:
        obj = json.loads(content.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, (int, float)):
            return int(total)
        # responses API 兼容：input_tokens + output_tokens
        it, ot = usage.get("input_tokens"), usage.get("output_tokens")
        if isinstance(it, (int, float)) and isinstance(ot, (int, float)):
            return int(it) + int(ot)
    return None'''
new = '''def _extract_usage(content: bytes) -> dict | None:
    """提取响应体 usage: {"total": n, "cached": m}（chat/responses 兼容）。"""
    try:
        obj = json.loads(content.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if not isinstance(total, (int, float)):
        it, ot = usage.get("input_tokens"), usage.get("output_tokens")
        if isinstance(it, (int, float)) and isinstance(ot, (int, float)):
            total = it + ot
    if not isinstance(total, (int, float)):
        return None
    # 缓存命中 token：chat 用 prompt_tokens_details、responses 用 input_tokens_details
    cached = 0
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict):
            c = details.get("cached_tokens")
            if isinstance(c, (int, float)):
                cached = max(cached, int(c))
    return {"total": int(total), "cached": int(cached)}'''
assert old in s, 'extract fn not found'
s = s.replace(old, new, 1)

# 2. 非流式：_extract_tokens 调用点
old2 = '''                    tokens = _extract_tokens(body_bytes)
                    self.eventlog.add(st.cfg.name, model, True, latency,
                                      detail, attempts=len(attempts) + 1,
                                      tokens=tokens)'''
new2 = '''                    usage = _extract_usage(body_bytes)
                    tokens = usage["total"] if usage else None
                    cached = usage["cached"] if usage else 0
                    self.eventlog.add(st.cfg.name, model, True, latency,
                                      detail, attempts=len(attempts) + 1,
                                      tokens=tokens, cached_tokens=cached)'''
assert old2 in s, 'json add site not found'
s = s.replace(old2, new2, 1)

# 3. 流式：gen 中提取缓存
old3 = '''    def _extract_stream_tokens(chunk: bytes) -> int | None:
    """从 SSE 数据块中提取 usage（chat include_usage 尾块 / responses usage 事件）。

    块可能包含多个事件，取最后一个可解析且含 usage 的 JSON。
    """
    text = chunk.decode("utf-8", errors="replace")
    result = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if isinstance(total, (int, float)):
                result = int(total)
            else:
                it, ot = usage.get("input_tokens"), usage.get("output_tokens")
                if isinstance(it, (int, float)) and isinstance(ot, (int, float)):
                    result = int(it) + int(ot)
    return result'''
new3 = '''    def _extract_stream_usage(chunk: bytes) -> dict | None:
    """从 SSE 数据块提取 usage: {"total": n, "cached": m}。

    块可能含多个事件，取最后一个可解析且含 usage 的 JSON。
    """
    text = chunk.decode("utf-8", errors="replace")
    result = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        total = usage.get("total_tokens")
        if not isinstance(total, (int, float)):
            it, ot = usage.get("input_tokens"), usage.get("output_tokens")
            if isinstance(it, (int, float)) and isinstance(ot, (int, float)):
                total = it + ot
        if isinstance(total, (int, float)):
            cached = 0
            for key in ("prompt_tokens_details", "input_tokens_details"):
                details = usage.get(key)
                if isinstance(details, dict):
                    c = details.get("cached_tokens")
                    if isinstance(c, (int, float)):
                        cached = max(cached, int(c))
            result = {"total": int(total), "cached": int(cached)}
    return result'''
assert old3 in s, 'stream extract fn not found'
s = s.replace(old3, new3, 1)

# 4. 流式 gen 内引用替换
old4 = '''            async def gen(resp=resp, st=st, first=first, stream_iter=stream_iter):
                stream_tokens = None
                try:
                    if first:
                        yield first
                        stream_tokens = _extract_stream_tokens(first) or stream_tokens
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                stream_iter.__anext__(), timeout=keepalive or None)
                            yield chunk
                            stream_tokens = _extract_stream_tokens(chunk) or stream_tokens
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            # SSE 保活：长流无事件时发注释行，防止客户端空闲超时
                            if keepalive:
                                yield b": keep-alive\\n\\n"
                except (httpx.TransportError, httpx.StreamError) as e:
                    # 流中途断开：SSE 协议下无法切换，补发错误事件
                    yield _error_sse(f"上游 {st.cfg.name} 流中断: {type(e).__name__}")
                finally:
                    if stream_tokens:
                        eventlog.add_tokens(st.cfg.name, route_model, stream_tokens)'''
new4 = '''            async def gen(resp=resp, st=st, first=first, stream_iter=stream_iter):
                stream_usage = None
                try:
                    if first:
                        yield first
                        stream_usage = _extract_stream_usage(first) or stream_usage
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                stream_iter.__anext__(), timeout=keepalive or None)
                            yield chunk
                            stream_usage = _extract_stream_usage(chunk) or stream_usage
                        except StopAsyncIteration:
                            break
                        except asyncio.TimeoutError:
                            # SSE 保活：长流无事件时发注释行，防止客户端空闲超时
                            if keepalive:
                                yield b": keep-alive\\n\\n"
                except (httpx.TransportError, httpx.StreamError) as e:
                    # 流中途断开：SSE 协议下无法切换，补发错误事件
                    yield _error_sse(f"上游 {st.cfg.name} 流中断: {type(e).__name__}")
                finally:
                    if stream_usage:
                        eventlog.add_tokens(st.cfg.name, route_model,
                                            stream_usage["total"],
                                            stream_usage["cached"])'''
assert old4 in s, 'gen block not found'
s = s.replace(old4, new4, 1)

# 5. 流式提交处记录 TTFT（send→首块）并更新站 EMA
old5 = '''            # ===== 已提交：开始透传（此后不可切换） =====
            latency = (time.perf_counter() - t0) * 1000
            switched = bool(attempts)
            st.record_success(latency / 1000, switched=switched)'''
new5 = '''            # ===== 已提交：开始透传（此后不可切换） =====
            latency = (time.perf_counter() - t0) * 1000
            # 首字时间(TTFT)：响应头到达(send 返回) → 首个数据块
            ttft_ms = (time.perf_counter() - t_first) * 1000
            st.record_ttft(ttft_ms / 1000)
            switched = bool(attempts)
            st.record_success(latency / 1000, switched=switched)'''
assert old5 in s, 'stream commit not found'
s = s.replace(old5, new5, 1)

# 6. 流式 t_first 记录：send 后、_read_first 前
old6 = '''            # ---- 等首字节（卡死检测点） ----
            try:
                stream_iter, first = await _read_first(
                    resp, timeout=self.gateway.first_byte_timeout)'''
new6 = '''            # ---- 等首字节（卡死检测点，同时测量首字时间） ----
            t_first = time.perf_counter()
            try:
                stream_iter, first = await _read_first(
                    resp, timeout=self.gateway.first_byte_timeout)'''
assert old6 in s, 'read_first site not found'
s = s.replace(old6, new6, 1)

open(p, 'w', encoding='utf-8').write(s)
print('proxy.py 全部修改完成')
