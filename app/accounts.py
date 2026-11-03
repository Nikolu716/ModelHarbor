"""SQLite account metadata and isolated, request-pinned gateway runtimes."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .access_control import AccessControlStore, is_ip_allowed, validate_allowed_ips
from .gateway_settings import GatewaySettingsStore
from .service_settings import ServiceSettingsStore
from .health import HealthMonitor
from .juice import JuiceRegistry
from .notifier import Notifier
from .proxy import OpenAIProxy
from .state import Registry
from .stations import StationStore
from .stats import EventLog
from .web.admin import create_admin_router
from .visual_tests import VisualTestStore, VisualTestRunner, VisualTestScheduler


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 600_000)
    return f'pbkdf2_sha256$600000${salt.hex()}${digest.hex()}'


def _verify_password(password: str, encoded: str) -> bool:
    try:
        kind, rounds, salt, expected = encoded.split('$')
        if kind != 'pbkdf2_sha256' or int(rounds) != 600_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


class AccountStore:
    def __init__(self, data_dir: str):
        self.root = Path(data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'accounts.sqlite3'
        with self.connect() as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, 1):
                raise ValueError('Unsupported account database version')
            db.execute('''CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL, password_hash TEXT NOT NULL DEFAULT '',
                require_password INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute("INSERT OR IGNORE INTO accounts VALUES ('default', 'default', '默认账号', '', 0, ?)", (time.time(),))
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('active_account', 'default')")
            db.execute('PRAGMA user_version=1')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return closing(db)

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM accounts ORDER BY created_at, id')]

    def get(self, account_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        if not row:
            raise HTTPException(404, '账号不存在')
        return dict(row)

    def active_id(self):
        with self.connect() as db:
            return db.execute("SELECT value FROM metadata WHERE key='active_account'").fetchone()[0]

    def activate(self, account_id):
        with self.connect() as db, db:
            if not db.execute('SELECT 1 FROM accounts WHERE id=?', (account_id,)).fetchone():
                raise HTTPException(404, '账号不存在')
            db.execute("UPDATE metadata SET value=? WHERE key='active_account'", (account_id,))

    def directory(self, account_id):
        self.get(account_id)  # IDs are database-generated; never accept a path from clients.
        return self.root if account_id == 'default' else self.root / 'accounts' / account_id

    async def save(self, body, account_id=None):
        allowed = {'username', 'display_name', 'password', 'require_password'}
        if set(body) - allowed:
            raise HTTPException(400, '账号字段不受支持')
        old = self.get(account_id) if account_id else {}
        username = body.get('username', old.get('username', ''))
        display = body.get('display_name', old.get('display_name', ''))
        password = body.get('password', '')
        required = body.get('require_password', bool(old.get('require_password', False)))
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{3,40}', username):
            raise HTTPException(400, '用户名需为 3–40 位字母、数字、点、横线或下划线')
        if not isinstance(display, str) or not 1 <= len(display.strip()) <= 60:
            raise HTTPException(400, '显示名称需为 1–60 个字符')
        if not isinstance(required, bool) or not isinstance(password, str):
            raise HTTPException(400, '密码或验证开关格式错误')
        if password and not 8 <= len(password) <= 256:
            raise HTTPException(400, '密码需为 8–256 个字符')
        encoded = await asyncio.to_thread(_password_hash, password) if password else old.get('password_hash', '')
        if required and not encoded:
            raise HTTPException(400, '开启切换验证前，请先设置密码')
        account_id = account_id or uuid.uuid4().hex
        try:
            with self.connect() as db, db:
                if not old and db.execute('SELECT COUNT(*) FROM accounts').fetchone()[0] >= 32:
                    raise HTTPException(409, '最多支持 32 个账号')
                db.execute('''INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET username=excluded.username,
                    display_name=excluded.display_name, password_hash=excluded.password_hash,
                    require_password=excluded.require_password''',
                    (account_id, username, display.strip(), encoded, int(required), old.get('created_at', time.time())))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, '用户名已存在') from exc
        return self.get(account_id)

    def restore(self, row):
        with self.connect() as db, db:
            db.execute('''UPDATE accounts SET username=?, display_name=?, password_hash=?,
                require_password=? WHERE id=?''',
                (row['username'], row['display_name'], row['password_hash'],
                 row['require_password'], row['id']))

    def delete(self, account_id):
        if account_id == 'default' or account_id == self.active_id():
            raise HTTPException(409, '默认账号和当前账号不能删除')
        self.get(account_id)
        with self.connect() as db, db:
            db.execute('DELETE FROM accounts WHERE id=?', (account_id,))


class AccountRuntime:
    def __init__(self, manager, account_id):
        self.id = account_id
        directory = str(manager.store.directory(account_id))
        self.gateway = manager.original_gateway if account_id == 'default' else copy.deepcopy(manager.template)
        self.gateway.data_dir = directory
        access = AccessControlStore(directory)
        fallback_ips = manager.template.allowed_ips if account_id == 'default' else []
        self.gateway.allowed_ips = access.load(fallback_ips)
        settings = GatewaySettingsStore(directory)
        for field, value in (settings.load(self.gateway) or {}).items():
            setattr(self.gateway, field, value)
        for field, value in (settings.load_probe(self.gateway) or {}).items():
            setattr(self.gateway, field, value)
        for field, value in (settings.load_scoring() or {}).items():
            setattr(self.gateway, field, value)
        stations = StationStore(directory)
        saved = stations.load()
        # A broken saved station file must never reactivate a different account's YAML stations.
        if stations.path.exists() and saved is None:
            raise ValueError('账号站点配置损坏，已停止加载')
        configs = saved if saved is not None else (manager.seed_stations if account_id == 'default' else [])
        self.registry = Registry(self.gateway, configs)
        self.eventlog = EventLog(
            directory, maxlen=settings.load_logs(),
            retention_days=self.gateway.data_retention_days)
        self.eventlog.load()
        self.registry.bind_statistics(self.eventlog)
        self.notifier = Notifier(self.gateway.webhook_url)
        self.monitor = HealthMonitor(self.registry, self.gateway, self.notifier)
        self.juice = JuiceRegistry(persist_path=str(Path(directory) / 'juice.json'))
        self.registry.juice_registry = self.juice
        self.proxy = OpenAIProxy(self.registry, self.gateway, self.eventlog, self.notifier)
        self.inactive = asyncio.Event()
        self.drained = asyncio.Event()
        self.drained.set()
        self.requests = 0
        self.monitor_task = None
        visual_store = VisualTestStore(directory)
        self.visual_scheduler = VisualTestScheduler(self.registry, VisualTestRunner(self.registry, self.gateway, visual_store), visual_store)
        self.app = FastAPI(title='ModelHarbor account', docs_url=None, redoc_url=None, openapi_url=None)
        self.app.include_router(self.proxy.api)
        self.app.include_router(self.proxy.google_api)
        self.app.include_router(create_admin_router(
            self.registry, self.gateway, self.monitor, self.eventlog, self.notifier,
            stations, settings, self.juice, access_store=access,
            inactive=lambda: self.inactive,
            service_store=manager.service_settings,
            visual_scheduler=self.visual_scheduler,
            restart_service=lambda: bool(manager.request_restart and manager.request_restart())))

    def acquire(self):
        self.requests += 1
        self.drained.clear()

    def release(self):
        self.requests -= 1
        if not self.requests:
            self.drained.set()

    def start(self):
        self.inactive = asyncio.Event()
        self.visual_scheduler.activate()
        self.monitor_task = asyncio.create_task(self._background(), name=f'background-{self.id}')

    async def _background(self):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self.monitor.run())
            tasks.create_task(self.visual_scheduler.run())

    def pause(self):
        self.inactive.set()
        self.visual_scheduler.deactivate()
        self.registry._activity_changed()  # Wake old SSE listeners so they reconnect to the new account.
        task = self.monitor_task
        self.monitor_task = None
        tasks = self.visual_scheduler.runner.cancel_pending()
        if task:
            task.cancel()
            tasks.append(task)
        return asyncio.gather(*tasks, return_exceptions=True) if tasks else None

    def persist(self):
        self.eventlog.maintenance()
        self.juice.persist(retention_days=self.gateway.data_retention_days)


class AccountManager:
    def __init__(self, cfg):
        self.store = AccountStore(cfg.gateway.data_dir)
        self.original_gateway = cfg.gateway
        self.template = copy.deepcopy(cfg.gateway)
        self.service_settings = ServiceSettingsStore(cfg.gateway.data_dir)
        self.request_restart = None
        self.seed_stations = cfg.stations
        self.runtimes = {}
        self.lock = asyncio.Lock()
        self.running = False
        self.revision = uuid.uuid4().hex
        self.failures = defaultdict(deque)
        self.cleanup_tasks = set()
        self.active = self.runtime(self.store.active_id())

    def runtime(self, account_id):
        if account_id not in self.runtimes:
            self.runtimes[account_id] = AccountRuntime(self, account_id)
        return self.runtimes[account_id]

    def view(self, row):
        return {key: row[key] for key in ('id', 'username', 'display_name', 'created_at')} | {
            'require_password': bool(row['require_password']),
            'has_password': bool(row['password_hash']), 'active': row['id'] == self.active.id}

    def snapshot(self):
        return {'accounts': [self.view(row) for row in self.store.list()],
                'active_id': self.active.id, 'revision': self.revision, 'scope': 'gateway'}

    async def switch(self, account_id, body, host):
        row = self.store.get(account_id)
        if set(body) - {'username', 'password'}:
            raise HTTPException(400, '切换字段不受支持')
        if row['require_password']:
            key = (host, account_id)
            now = time.monotonic()
            # Bound memory; rate limiting is independent of account metadata edits.
            if key not in self.failures and len(self.failures) >= 2048:
                self.failures.pop(next(iter(self.failures)))
            failures = self.failures[key]
            while failures and failures[0] <= now - 300:
                failures.popleft()
            if len(failures) >= 5:
                raise HTTPException(429, '验证失败次数过多，请稍后重试', headers={'Retry-After': '300'})
            username, password = body.get('username'), body.get('password')
            valid_input = isinstance(username, str) and isinstance(password, str) and len(password) <= 256
            valid_password = await asyncio.to_thread(_verify_password, password if valid_input else '', row['password_hash'])
            if not valid_input or username.casefold() != row['username'].casefold() or not valid_password:
                failures.append(now)
                raise HTTPException(403, '账号或密码错误', headers={'X-ModelHarbor-Auth': 'account'})
            self.failures.pop(key, None)
        try:
            target = self.runtime(account_id)
        except ValueError as exc:
            raise HTTPException(503, '目标账号配置损坏，请修复配置后重试') from exc
        if not is_ip_allowed(host, target.gateway.allowed_ips):
            raise HTTPException(400, '目标账号白名单未包含当前 IP，请先编辑该账号白名单')
        if target is self.active:
            return self.snapshot()
        self.store.activate(account_id)  # Commit before publishing; no await between these two writes.
        previous = self.active
        self.active = target
        self.revision = uuid.uuid4().hex
        old_monitor = previous.pause()
        if self.running:
            target.start()
            task = asyncio.create_task(self._drain(previous, old_monitor))
            self.cleanup_tasks.add(task)
            task.add_done_callback(self.cleanup_tasks.discard)
        return self.snapshot()

    async def _drain(self, runtime, monitor_task):
        if monitor_task:
            await asyncio.gather(monitor_task, return_exceptions=True)
        await runtime.drained.wait()
        async with self.lock:
            if runtime is not self.active and not runtime.requests:
                runtime.persist()
                await runtime.registry.close_clients()

    async def start(self):
        self.running = True
        self.active.start()
        self.persist_task = asyncio.create_task(self._persist_loop())

    async def _persist_loop(self):
        while True:
            await asyncio.sleep(30)
            for runtime in list(self.runtimes.values()):
                runtime.persist()

    async def stop(self):
        self.running = False
        tasks = [self.persist_task, *self.cleanup_tasks]
        for runtime in self.runtimes.values():
            task = runtime.pause()
            if task:
                tasks.append(task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for runtime in self.runtimes.values():
            runtime.eventlog.persist()
            runtime.juice.persist(retention_days=runtime.gateway.data_retention_days)
            await runtime.registry.close_clients()
            await runtime.notifier.close()


def create_account_router(manager):
    api = APIRouter(prefix='/admin/accounts', tags=['accounts'])

    @api.get('')
    async def accounts():
        return manager.snapshot()

    @api.get('/{account_id}')
    async def account(account_id: str):
        row = manager.store.get(account_id)
        try:
            runtime = manager.runtime(account_id)
        except ValueError as exc:
            raise HTTPException(503, '账号配置损坏，请修复配置后重试') from exc
        return manager.view(row) | {'allowed_ips': list(runtime.gateway.allowed_ips)}

    async def save_account(body, account_id=None):
        values = dict(body)
        has_ips = 'allowed_ips' in values
        ips = values.pop('allowed_ips', None)
        if has_ips:
            try:
                ips = validate_allowed_ips(ips)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc
        if account_id and account_id == manager.active.id and ips is not None:
            raise HTTPException(400, '当前账号的白名单请在「IP 白名单」中修改')
        previous = manager.store.get(account_id) if account_id else None
        row = await manager.store.save(values, account_id)
        if ips is not None:
            try:
                AccessControlStore(str(manager.store.directory(row['id']))).save(ips)
            except OSError:
                # No existing allowlist is changed when the atomic file replacement fails.
                if previous:
                    manager.store.restore(previous)
                else:
                    manager.store.delete(row['id'])
                raise HTTPException(500, '白名单保存失败')
            if row['id'] in manager.runtimes:
                manager.runtimes[row['id']].gateway.allowed_ips = ips
        return manager.view(row)

    @api.post('', status_code=201)
    async def create(body: dict):
        return await save_account(body)

    @api.put('/{account_id}')
    async def update(account_id: str, body: dict):
        return await save_account(body, account_id)

    @api.post('/{account_id}/switch')
    async def switch(account_id: str, body: dict, request: Request):
        return await manager.switch(account_id, body, request.client.host if request.client else None)

    @api.delete('/{account_id}')
    async def delete(account_id: str):
        runtime = manager.runtimes.get(account_id)
        if runtime and runtime.requests:
            raise HTTPException(409, '账号仍有请求进行中，请稍后删除')
        manager.store.delete(account_id)
        if runtime:
            await runtime.registry.close_clients()
            await runtime.notifier.close()
            manager.runtimes.pop(account_id)
        return {'ok': True}

    return api


class RuntimeMiddleware:
    """Pin the full ASGI response (including its stream) to one account."""
    def __init__(self, app, manager, check_bearer):
        self.app, self.manager, self.check_bearer = app, manager, check_bearer

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        path, method = scope['path'], scope['method']
        accounts = path == '/admin/accounts' or path.startswith('/admin/accounts/')
        mutation = path.startswith('/admin/') and method not in ('GET', 'HEAD', 'OPTIONS')
        if mutation:
            async with self.manager.lock:
                return await self.dispatch(scope, receive, send, accounts)
        return await self.dispatch(scope, receive, send, accounts)

    async def dispatch(self, scope, receive, send, accounts):
        manager = self.manager
        runtime = manager.active
        revision = manager.revision
        path = scope['path']
        headers = dict(scope.get('headers', []))
        host = scope['client'][0] if scope.get('client') else None
        error = None
        if not is_ip_allowed(host, runtime.gateway.allowed_ips):
            error = JSONResponse(status_code=403, content={'error': {
                'message': 'Client IP is not allowed.', 'type': 'permission_error',
                'param': None, 'code': 'ip_not_allowed'}})
        else:
            auth = headers.get(b'authorization', b'').decode('latin1')
            cfg = manager.template
            inference = path.startswith(('/v1/', '/v1beta/'))
            if inference and not auth:
                native_key = (headers.get(b'x-api-key', b'') if path == '/v1/messages'
                              else headers.get(b'x-goog-api-key', b'') if '/models/' in path or path == '/v1beta/models' else b'')
                if native_key:
                    auth = 'Bearer ' + native_key.decode('latin1')
            keys = cfg.api_keys if inference else ([cfg.admin_key] if path.startswith('/admin/') and cfg.admin_key else [])
            if keys and not self.check_bearer(auth, keys):
                admin = path.startswith('/admin/')
                error = JSONResponse(status_code=401, content={'error': {
                    'message': 'Invalid admin key.' if admin else 'Invalid API key provided.',
                    'type': 'admin_error' if admin else 'authentication_error', 'param': None,
                    'code': 'invalid_admin_key' if admin else 'invalid_api_key'}})
            expected = headers.get(b'x-modelharbor-account', b'').decode('latin1')
            expected_revision = headers.get(b'x-modelharbor-revision', b'').decode('latin1')
            if not error and path.startswith('/admin/') and not accounts and (
                    (expected and expected != runtime.id) or (expected_revision and expected_revision != revision)):
                error = JSONResponse(status_code=409, content={'detail': '当前账号已切换，请刷新后重试', 'code': 'account_changed'})
        if error:
            from .protocols import MESSAGES, GENERATE, error_body
            native_path = MESSAGES if path == '/v1/messages' else GENERATE if path.startswith('/v1beta/') or ':generateContent' in path or ':streamGenerateContent' in path else None
            if native_path:
                payload = json.loads(error.body)
                message = (payload.get('error') or {}).get('message', 'Request rejected')
                error = JSONResponse(status_code=error.status_code, content=error_body(error.status_code, message, native_path))
            error.headers['X-ModelHarbor-Account'] = runtime.id
            error.headers['X-ModelHarbor-Revision'] = revision
            error.headers['Cache-Control'] = 'no-store'
            return await error(scope, receive, send)

        async def decorated_send(message):
            if message['type'] == 'http.response.start':
                message = dict(message)
                extra = [(b'x-modelharbor-account', runtime.id.encode()), (b'x-modelharbor-revision', revision.encode())]
                if path.startswith(('/static/', '/admin/')):
                    extra.append((b'cache-control', b'no-store'))
                message['headers'] = list(message.get('headers', [])) + extra
            await send(message)

        target = self.app if accounts or not path.startswith(('/v1/', '/v1beta/', '/admin/')) else runtime.app
        runtime.acquire()
        try:
            return await target(scope, receive, decorated_send)
        finally:
            runtime.release()
