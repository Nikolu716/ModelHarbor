"""单元验证：配额/订阅错误分类逻辑。"""
import sys
sys.path.insert(0, '.')
from app.errors import classify_error
from app.config import load_config

cases = [
    ('402余额不足', 402, b'{"error":{"message":"Insufficient quota: balance not enough"}}', True),
    ('403订阅到期', 403, b'{"error":{"message":"Your subscription has expired"}}', True),
    ('403普通禁止', 403, b'{"error":{"message":"Access forbidden"}}', False),
    ('429限流', 429, b'{"error":{"message":"Rate limit reached"}}', False),
    ('401key错误', 401, b'{"error":{"message":"Incorrect API key"}}', False),
    ('400余额不足', 400, b'{"error":{"message":"insufficient_quota"}}', True),
]
ok = True
for label, code, body, expect_quota in cases:
    e = classify_error('x', code, body)
    status = 'PASS' if e.quota == expect_quota else 'FAIL'
    if e.quota != expect_quota:
        ok = False
    print(f'{status} {label:12s} status={code} retryable={e.retryable} quota={e.quota}')

cfg = load_config('tests/gateway-test.yaml')
billings = ', '.join(f"{s.name}={s.billing}" for s in cfg.stations)
print(f'配置OK: {len(cfg.stations)} 站 | {billings}')

try:
    from app.config import StationConfig
    StationConfig.from_dict('x', {'name': 'x', 'base_url': 'http://x', 'api_key': 'k',
                                  'billing': 'pay_as_you_go'})
    print('FAIL 非法billing未拦截')
    ok = False
except ValueError as e:
    print(f'PASS 非法billing拦截: {e}')

print('ALL PASS' if ok else 'HAS FAILURES')
sys.exit(0 if ok else 1)
