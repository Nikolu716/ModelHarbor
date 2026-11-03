"""Account-local, explicit model substitution; never changes access control."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .access_control import normalize_ip, validate_allowed_ips

MAPPING_PATHS = ('/responses', '/chat/completions')


def supports_mapping(st, model, path):
    return st.supports_model(model, path) or (
        path == '/responses' and st.supports_model(model, '/chat/completions'))


def mapping_path(st, model, path):
    if path == '/responses' and (st.cfg.wire_api == 'chat_completions' or (
            st.cfg.provider == 'opencode' and '/responses' not in st.model_api_paths.get(model, []))):
        return '/chat/completions'
    return path


def validate_rules(values, registry=None):
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError('最多配置 64 条模型映射')
    rules, ids, scopes = [], set(), {}
    for item in values:
        fields = {'id', 'name', 'enabled', 'source_model', 'source_ips', 'targets'}
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('映射规则字段不完整或包含未知字段')
        rule = copy.deepcopy(item)
        if not isinstance(rule['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', rule['id']) or rule['id'] in ids:
            raise ValueError('映射 ID 无效或重复')
        ids.add(rule['id'])
        for key, limit in (('name', 60), ('source_model', 200)):
            value = rule[key]
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit or any(ord(c) < 32 for c in value):
                raise ValueError('规则名称或请求模型无效')
            rule[key] = value.strip()
        if '*' in rule['source_model']:
            raise ValueError('请填写完整请求模型名称，不支持通配符')
        if not isinstance(rule['enabled'], bool):
            raise ValueError('启用状态须为布尔值')
        rule['source_ips'] = validate_allowed_ips(rule['source_ips'])
        if rule['enabled']:
            scope = scopes.setdefault(rule['source_model'], set())
            addresses = set(rule['source_ips'] or ['*'])
            if scope & addresses:
                raise ValueError('同一请求模型的 IP 范围重复；每个 IP 或全体范围只能启用一条规则')
            scope.update(addresses)
        targets = rule['targets']
        if not isinstance(targets, list) or not 1 <= len(targets) <= 16:
            raise ValueError('每条规则请选择 1–16 个目标渠道')
        seen = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {'station', 'model'}:
                raise ValueError('目标须包含渠道和实际模型')
            for key in ('station', 'model'):
                value = target[key]
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or any(ord(c) < 32 for c in value):
                    raise ValueError('目标渠道或模型无效')
                target[key] = value.strip()
            if target['station'] in seen:
                raise ValueError('同一规则中每个渠道只能选择一次')
            seen.add(target['station'])
            if registry is not None:
                st = registry.states.get(target['station'])
                if st is None:
                    raise ValueError('目标渠道不属于当前账号或已被删除')
                if not any(st.supports_model(target['model'], p) for p in MAPPING_PATHS):
                    raise ValueError('目标模型不在渠道的可路由 OpenAI 模型范围内')
        rules.append(rule)
    return rules


class ModelMappings:
    def __init__(self, directory=None):
        self.path = Path(directory) / 'model-mappings.json' if directory else None
        self.rules = []
        self.error = ''
        if self.path and self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(payload, dict) or set(payload) != {'rules'}:
                    raise ValueError('invalid mappings document')
                self.rules = validate_rules(payload['rules'])
            except (OSError, ValueError, TypeError):
                # A broken explicit routing policy must not silently send traffic elsewhere.
                self.error = '模型映射配置损坏，请在模型映射中重新保存后再请求'

    def save(self, rules, registry):
        normalized = validate_rules(rules, registry)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'rules': normalized}, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(self.path)
        self.rules, self.error = normalized, ''

    def match(self, model, client_ip, path):
        if path not in MAPPING_PATHS:
            return None
        if self.error:
            raise ValueError(self.error)
        try:
            address = normalize_ip(client_ip) if client_ip else None
        except ValueError:
            address = None
        matching = [rule for rule in self.rules if rule['enabled'] and rule['source_model'] == model
                    and (not rule['source_ips'] or address in rule['source_ips'])]
        matching.sort(key=lambda rule: not bool(rule['source_ips']))
        return copy.deepcopy(matching[0]) if matching else None

    def view(self):
        return {'rules': copy.deepcopy(self.rules), 'error': self.error}
