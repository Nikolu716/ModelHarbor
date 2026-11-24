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


def target_models(mapping) -> list[str]:
    """Stable, de-duplicated actual models requested by one rule."""
    return list(dict.fromkeys(mapping.get('target_models') or ()))


def target_groups(states, mapping, path):
    """Keep model priority and every (station, model) combination intact."""
    return [(model, {name: model for name, st in states.items()
                     if supports_mapping(st, model, path)})
            for model in target_models(mapping)]


def resolve_target_stations(states, mapping, path) -> dict[str, str]:
    """Station inventory for catalogs; routing uses ordered target_groups."""
    resolved: dict[str, str] = {}
    for model, stations in target_groups(states, mapping, path):
        for name in stations:
            resolved.setdefault(name, model)
    return resolved


def pending_catalogs(states, path):
    """Only an initial/in-progress compatible catalog warrants a short wait."""
    from .protocols import provider_supports
    return [st for st in states.values()
            if st.enabled and not st.quota_exhausted and st.cfg.probe_models
            and not st.probed_models and st.models_discovery_status in {'pending', 'loading'}
            and (provider_supports(st.cfg.provider, path) or
                 path == '/responses' and provider_supports(st.cfg.provider, '/chat/completions'))
            and not (path == '/chat/completions' and st.cfg.wire_api == 'responses')]


def mapping_preview(registry, rule):
    """Read the same admission guards as routing, without acquiring or I/O."""
    from .router import Router
    router = Router(registry, registry.gateway)
    result = {}
    for path in MAPPING_PATHS:
        def station_view(st):
            reason = router.unavailable_reason(st)
            return {'name': st.cfg.name, 'available': reason is None, 'reason': reason or ''}
        targets = [{'model': model, 'stations': [station_view(registry.states[name]) for name in names]}
                   for model, names in target_groups(registry.states, rule, path)]
        fallback = [station_view(st) for st in registry.states.values()
                    if st.supports_model(rule['source_model'], path)]
        pending = [st.cfg.name for st in pending_catalogs(registry.states, path)]
        first = next((item for item in targets if any(st['available'] for st in item['stations'])), None)
        mode = ('disabled' if not rule['enabled'] else 'target' if first else 'pending' if pending
                else 'fallback' if any(st['available'] for st in fallback) else 'unavailable')
        result[path] = {'mode': mode, 'targets': targets, 'fallback': fallback, 'pending': pending,
                        'model': first['model'] if first else rule['source_model'],
                        'stations': [st['name'] for st in (first['stations'] if first else fallback) if st['available']]}
    return result


def _normalize_rule(item):
    """Convert legacy station-pinned targets into the model-only schema."""
    if not isinstance(item, dict) or 'targets' not in item:
        return item
    legacy = item['targets']
    if not isinstance(legacy, list):
        raise ValueError('映射目标格式无效')
    models: list[str] = []
    for target in legacy:
        model = target.get('model') if isinstance(target, dict) else target
        if not isinstance(model, str):
            raise ValueError('映射目标格式无效')
        model = model.strip()
        if model and model not in models:
            models.append(model)
    rule = {key: value for key, value in item.items() if key != 'targets'}
    rule.setdefault('target_models', models)
    return rule


def validate_rules(values, registry=None):
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError('最多配置 64 条模型映射')
    rules, ids, scopes = [], set(), {}
    for item in values:
        item = _normalize_rule(item)
        fields = {'id', 'name', 'enabled', 'source_model', 'source_ips', 'target_models'}
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
        models = rule['target_models']
        if not isinstance(models, list) or not 1 <= len(models) <= 16:
            raise ValueError('每条规则请选择 1–16 个实际回答模型')
        normalized = []
        for value in models:
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 or any(ord(c) < 32 for c in value):
                raise ValueError('实际回答模型无效')
            value = value.strip()
            if value in normalized:
                raise ValueError('同一规则中每个实际模型只能选择一次')
            normalized.append(value)
        rule['target_models'] = normalized
        rules.append(rule)
    return rules


class ModelMappings:
    def __init__(self, directory=None):
        self.path = Path(directory) / 'model-mappings.json' if directory else None
        self.rules = []
        self.error = ''
        self.migration_error = ''
        if self.path and self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding='utf-8'))
                if not isinstance(payload, dict) or set(payload) != {'rules'}:
                    raise ValueError('invalid mappings document')
                self.rules = validate_rules(payload['rules'])
            except (OSError, ValueError, TypeError):
                # A broken explicit routing policy must not silently send traffic elsewhere.
                self.error = '模型映射配置损坏，请在模型映射中重新保存后再请求'
            else:
                if any('targets' in item for item in payload['rules']):
                    try:
                        backup = self.path.with_suffix('.legacy.json')
                        if not backup.exists():
                            with backup.open('xb') as stream:
                                stream.write(self.path.read_bytes())
                        self._write(self.rules)
                    except OSError:
                        # Valid legacy policy remains active even on a read-only disk.
                        self.migration_error = '旧映射已兼容加载，但迁移保存失败；请检查存储后重新保存'

    def _write(self, normalized):
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'rules': normalized}, ensure_ascii=False, indent=2), encoding='utf-8')
            tmp.replace(self.path)

    def save(self, rules, registry):
        normalized = validate_rules(rules, registry)
        self._write(normalized)
        self.rules, self.error = normalized, ''
        self.migration_error = ''

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
        return {'rules': copy.deepcopy(self.rules), 'error': self.error, 'migration_error': self.migration_error}
