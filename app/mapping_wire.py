"""Explicit Responses -> Chat adaptation for account model mappings."""
from __future__ import annotations

import codecs
import json
import uuid

from .wire import UnsupportedWireRequest, _sse_event


def unsupported(message):
    raise UnsupportedWireRequest(message + '；未向此渠道发送请求。可使用原生接口或其他目标渠道。')


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(p, dict) and p.get('type') in
                                       ('input_text', 'output_text', 'text') and isinstance(p.get('text'), str) for p in content):
        return ''.join(p['text'] for p in content)
    unsupported('模型映射的协议转换仅支持文本和标准函数工具，不支持图片等多模态内容')


def responses_to_chat(body):
    accepted = {'model', 'input', 'instructions', 'max_output_tokens', 'temperature', 'top_p',
                'stream', 'parallel_tool_calls', 'tools', 'tool_choice', 'reasoning', 'text',
                'previous_response_id', 'conversation', 'background', 'include', 'truncation',
                'store', 'metadata', 'prompt_cache_key', 'safety_identifier', 'user'}
    if set(body) - accepted:
        unsupported('模型映射包含无法转换的 Responses 参数：' + ', '.join(sorted(set(body) - accepted)))
    if body.get('store'):
        unsupported('模型映射的协议转换不支持服务端保存会话')
    for key in ('previous_response_id', 'conversation', 'background', 'include', 'truncation'):
        if body.get(key) and not (key == 'truncation' and body[key] == 'disabled'):
            unsupported('模型映射无法转换 Responses 参数 ' + key)
    if body.get('text') and body['text'] != {'format': {'type': 'text'}}:
        unsupported('模型映射暂不转换结构化输出或文本控制参数')
    reasoning = body.get('reasoning') or {}
    if not isinstance(reasoning, dict) or set(reasoning) - {'effort'}:
        unsupported('模型映射仅支持 reasoning.effort，不支持推理摘要等专用参数')
    messages = []
    if body.get('instructions'):
        messages.append({'role': 'system', 'content': _text(body['instructions'])})
    inputs = body.get('input', [])
    if isinstance(inputs, str):
        inputs = [{'role': 'user', 'content': inputs}]
    if not isinstance(inputs, list):
        unsupported('Responses input 须为文本或输入项数组')
    for item in inputs:
        if not isinstance(item, dict):
            unsupported('无法转换此 Responses 输入项')
        kind = item.get('type', 'message')
        if kind == 'message':
            role = item.get('role', 'user')
            if role not in ('user', 'assistant', 'system', 'developer'):
                unsupported('无法转换此消息角色')
            messages.append({'role': role, 'content': _text(item.get('content', ''))})
        elif kind == 'function_call':
            if item.get('namespace') or not all(isinstance(item.get(k), str) and item[k] for k in ('call_id', 'name', 'arguments')):
                unsupported('函数调用需要 call_id、name、arguments，且不支持命名空间')
            call = {'id': item['call_id'], 'type': 'function',
                    'function': {'name': item['name'], 'arguments': item['arguments']}}
            if messages and messages[-1].get('tool_calls'):
                messages[-1]['tool_calls'].append(call)
            else:
                messages.append({'role': 'assistant', 'content': None, 'tool_calls': [call]})
        elif kind == 'function_call_output':
            if not isinstance(item.get('call_id'), str) or not item['call_id']:
                unsupported('函数输出缺少 call_id')
            messages.append({'role': 'tool', 'tool_call_id': item['call_id'], 'content': _text(item.get('output'))})
        else:
            unsupported('无法转换 Responses 输入类型 ' + str(kind))
    chat = {'model': body.get('model'), 'messages': messages}
    tools = body.get('tools', [])
    if not isinstance(tools, list):
        unsupported('tools 须为数组')
    if tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get('type') != 'function' or tool.get('namespace') or not isinstance(tool.get('name'), str):
                unsupported('模型映射仅转换标准 function 工具，不支持内置工具、自定义工具或命名空间')
            if set(tool) - {'type', 'name', 'description', 'parameters', 'strict'}:
                unsupported('函数工具包含无法转换的字段')
            converted.append({'type': 'function', 'function': {k: v for k, v in tool.items() if k != 'type'}})
        chat['tools'] = converted
    if 'tool_choice' in body:
        choice = body['tool_choice']
        if isinstance(choice, dict) and set(choice) == {'type', 'name'} and choice['type'] == 'function':
            choice = {'type': 'function', 'function': {'name': choice['name']}}
        elif not isinstance(choice, str) or choice not in ('auto', 'none', 'required'):
            unsupported('无法转换此 tool_choice')
        chat['tool_choice'] = choice
    if 'max_output_tokens' in body:
        chat['max_tokens'] = body['max_output_tokens']
    for key in ('temperature', 'top_p', 'stream', 'parallel_tool_calls', 'user'):
        if key in body:
            chat[key] = body[key]
    if reasoning.get('effort'):
        chat['reasoning_effort'] = reasoning['effort']
    if chat.get('stream'):
        chat['stream_options'] = {'include_usage': True}
    return chat


def _usage(usage):
    result = {'input_tokens': usage.get('prompt_tokens', 0),
              'output_tokens': usage.get('completion_tokens', 0),
              'total_tokens': usage.get('total_tokens', 0)}
    if 'prompt_tokens_details' in usage:
        result['input_tokens_details'] = usage['prompt_tokens_details']
    if 'completion_tokens_details' in usage:
        result['output_tokens_details'] = usage['completion_tokens_details']
    return result


def chat_to_responses(obj, model):
    if (not isinstance(obj, dict) or not isinstance(obj.get('choices'), list)
            or not obj['choices'] or not isinstance(obj['choices'][0], dict)
            or not isinstance(obj['choices'][0].get('message'), dict)):
        raise ValueError('上游返回了无效的 Chat Completions 响应')
    choice = obj['choices'][0]
    message = choice.get('message') or {}
    output = []
    if message.get('content'):
        output.append({'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                       'status': 'completed', 'content': [{'type': 'output_text', 'text': message['content'], 'annotations': []}]})
    if message.get('refusal'):
        output.append({'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant',
                       'status': 'completed', 'content': [{'type': 'refusal', 'refusal': message['refusal']}]})
    for call in message.get('tool_calls') or []:
        if call.get('type') != 'function':
            raise ValueError('上游返回了无法转换的工具调用')
        output.append({'id': 'fc_' + uuid.uuid4().hex, 'type': 'function_call', 'status': 'completed',
                       'call_id': call['id'], 'name': call['function']['name'], 'arguments': call['function']['arguments']})
    result = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'status': 'completed',
              'model': model, 'output': output}
    if isinstance(obj.get('usage'), dict):
        result['usage'] = _usage(obj['usage'])
    if choice.get('finish_reason') in ('length', 'content_filter'):
        result.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens' if choice['finish_reason'] == 'length' else 'content_filter'})
    return result


class ChatStreamToResponsesTranslator:
    """Preserve text/tool lifecycle, usage and incomplete/error outcomes across SSE chunks."""
    def __init__(self, model):
        self.model = model
        self.id = 'resp_' + uuid.uuid4().hex
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.buffer = ''
        self.lines = []
        self.started = self.finished = self.ended = False
        self.reason = None
        self.usage = None
        self.error = ''
        self.sequence = 0
        self.output = []
        self.text_index = None
        self.tools = {}

    def emit(self, event, **data):
        payload = {'type': event, 'sequence_number': self.sequence, **data}
        self.sequence += 1
        return _sse_event(event, payload)

    def response(self, status):
        result = {'id': self.id, 'object': 'response', 'status': status, 'model': self.model, 'output': self.output}
        if self.usage is not None:
            result['usage'] = _usage(self.usage)
        return result

    def handle(self, data):
        try:
            return self._handle_data(data)
        except (KeyError, TypeError, AttributeError, IndexError):
            self.error = '上游返回了无法转换的 SSE 数据结构'
            return self.complete()

    def _handle_data(self, data):
        if self.finished:
            return b''
        if data.strip() == '[DONE]':
            self.ended = True
            return self.complete()
        try:
            obj = json.loads(data)
        except ValueError:
            self.error = '上游返回了无效 SSE 数据'
            return b''
        if not isinstance(obj, dict):
            self.error = '上游返回了无效 SSE 对象'
            return b''
        if obj.get('error'):
            self.error = '上游流返回错误'
            return self.complete()
        out = b''
        if not self.started:
            self.started = True
            out += self.emit('response.created', response=self.response('in_progress'))
        if isinstance(obj.get('usage'), dict):
            self.usage = obj['usage']
        choices = obj.get('choices') or []
        if not choices:
            return out
        choice = choices[0]
        self.reason = choice.get('finish_reason') or self.reason
        delta = choice.get('delta') or {}
        if delta.get('refusal'):
            self.error = '上游拒绝了此请求'
            return out + self.complete()
        if delta.get('content'):
            if self.text_index is None:
                self.text_index = len(self.output)
                item = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}
                self.output.append(item)
                out += self.emit('response.output_item.added', output_index=self.text_index, item=item)
                part = {'type': 'output_text', 'text': '', 'annotations': []}
                item['content'].append(part)
                out += self.emit('response.content_part.added', output_index=self.text_index, item_id=item['id'], content_index=0, part=part)
            item = self.output[self.text_index]
            item['content'][0]['text'] += delta['content']
            out += self.emit('response.output_text.delta', output_index=self.text_index, item_id=item['id'], content_index=0, delta=delta['content'])
        for call in delta.get('tool_calls') or []:
            index = call.get('index', 0)
            function = call.get('function') or {}
            if index not in self.tools:
                if not call.get('id') or call.get('type', 'function') != 'function':
                    self.error = '上游工具调用缺少 ID 或类型不受支持'
                    return out + self.complete()
                self.tools[index] = len(self.output)
                item = {'id': 'fc_' + uuid.uuid4().hex, 'type': 'function_call', 'status': 'in_progress',
                        'call_id': call['id'], 'name': function.get('name', ''), 'arguments': ''}
                self.output.append(item)
                out += self.emit('response.output_item.added', output_index=self.tools[index], item=item)
            else:
                item = self.output[self.tools[index]]
                if function.get('name'):
                    item['name'] += function['name']
            arguments = function.get('arguments') or ''
            if arguments:
                item['arguments'] += arguments
                out += self.emit('response.function_call_arguments.delta', output_index=self.tools[index], item_id=item['id'], delta=arguments)
        return out

    def complete(self):
        if self.finished:
            return b''
        self.finished = True
        if not self.error and not (self.ended or self.reason):
            self.error = '上游流提前结束，未收到完整结束事件'
        out = b''
        if not self.started:
            out += self.emit('response.created', response=self.response('in_progress'))
        if self.error:
            resp = self.response('failed')
            resp['error'] = {'code': 'upstream_stream_error', 'message': self.error}
            return out + self.emit('response.failed', response=resp)
        incomplete = self.reason in ('length', 'content_filter')
        for index, item in enumerate(self.output):
            item['status'] = 'incomplete' if incomplete else 'completed'
            if item['type'] == 'message':
                part = item['content'][0]
                out += self.emit('response.output_text.done', output_index=index, item_id=item['id'], content_index=0, text=part['text'])
                out += self.emit('response.content_part.done', output_index=index, item_id=item['id'], content_index=0, part=part)
            else:
                out += self.emit('response.function_call_arguments.done', output_index=index, item_id=item['id'], arguments=item['arguments'])
            out += self.emit('response.output_item.done', output_index=index, item=item)
        status = 'incomplete' if incomplete else 'completed'
        resp = self.response(status)
        if incomplete:
            resp['incomplete_details'] = {'reason': 'max_output_tokens' if self.reason == 'length' else 'content_filter'}
        return out + self.emit('response.' + status, response=resp)

    def feed(self, chunk):
        self.buffer += self.decoder.decode(chunk, final=False)
        out = b''
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            line = line.rstrip('\r')
            if not line and self.lines:
                out += self.handle('\n'.join(self.lines))
                self.lines = []
            elif line.startswith('data:'):
                self.lines.append(line[5:].lstrip(' '))
        return out

    def finish(self):
        out = self.feed(self.decoder.decode(b'', final=True).encode('utf-8') + b'\n\n')
        return out + self.complete()
