"""Staged official Codex logins, independent of subscription-sharing OAuth."""
from __future__ import annotations

import asyncio
import shutil
import time
import uuid

from .chatgpt import ChatGPTAuthError
from .codex_account import CodexAccount


class CodexBindings:
    def __init__(self, accounts):
        self.accounts = accounts
        self.attempts = {}
        self.start_lock = asyncio.Lock()
        self.cleanup_tasks = set()

    def _background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.cleanup_tasks.add(task)
        def finished(done):
            self.cleanup_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    async def start(self):
        async with self.start_lock:
            if sum(item['status'] == 'pending' for item in self.attempts.values()) >= 4:
                raise ChatGPTAuthError('最多同时进行 4 个 Codex 授权，请先完成或取消已有授权')
            for identifier in list(self.attempts):
                if len(self.attempts) < 16:
                    break
                if self.attempts[identifier]['status'] != 'pending':
                    if self.attempts[identifier]['status'] == 'completed' and not self.attempts[identifier].get('claimed'):
                        await self.cancel(identifier)
                    self.attempts.pop(identifier)
            self.accounts.store.prepare()
            profile, identifier = uuid.uuid4().hex, uuid.uuid4().hex
            bridge = CodexAccount(self.accounts.store.directory / ('codex-' + profile), '')
            attempt = dict(id=identifier, profile=profile, status='pending', error='',
                           expires_at=time.time() + 900, bridge=bridge, lock=asyncio.Lock())
            self.attempts[identifier] = attempt
            try:
                login = await bridge.begin_login()
                attempt.update(user_code=login['user_code'], verification_url=login['verification_url'])
            except BaseException:
                self.attempts.pop(identifier, None)
                await bridge.close()
                self._remove_profile(profile)
                raise
            if attempt['status'] == 'pending':
                attempt['timer'] = asyncio.get_running_loop().call_later(
                    900, lambda: self._background(self.cancel(identifier, expired=True)))
            return self._view(attempt)

    def _view(self, attempt):
        fields = ('id', 'status', 'expires_at', 'error')
        result = {key: attempt[key] for key in fields}
        if attempt['status'] == 'pending':
            result.update(user_code=attempt['user_code'], verification_url=attempt['verification_url'])
        elif attempt['status'] == 'completed':
            result.update(profile=attempt['profile'], account=self.accounts.profile_view(
                attempt['profile'], 'codex_relay'))
        return result

    def _get(self, identifier):
        attempt = self.attempts.get(identifier)
        if not attempt:
            raise ChatGPTAuthError('Codex 授权不存在或已结束，请重新发起绑定')
        return attempt

    @staticmethod
    def _stop_timer(attempt):
        timer = attempt.pop('timer', None)
        if timer:
            timer.cancel()

    def claim_profile(self, profile):
        for attempt in self.attempts.values():
            if attempt['profile'] == profile and attempt['status'] == 'completed':
                attempt['claimed'] = True
                self._stop_timer(attempt)

    def _remove_profile(self, profile):
        root = self.accounts.store.directory.resolve()
        directory = (root / ('codex-' + profile)).resolve()
        if directory.parent != root or not directory.name.startswith('codex-'):
            return
        try:
            directory.relative_to(root)
        except ValueError:
            return
        shutil.rmtree(directory, ignore_errors=True)
        self.accounts.store.path(profile).unlink(missing_ok=True)

    async def status(self, identifier):
        attempt = self._get(identifier)
        if attempt['status'] == 'pending' and time.time() >= attempt['expires_at']:
            return await self.cancel(identifier, expired=True)
        async with attempt['lock']:
            if attempt['status'] != 'pending':
                return self._view(attempt)
            bridge = attempt['bridge']
            login = bridge.login or {}
            if login.get('status') == 'completed':
                try:
                    async with bridge.operation_lock:
                        account = await bridge._account()
                    email = account['email'].strip()
                    if not email:
                        raise ChatGPTAuthError('官方未返回账号邮箱，请重新绑定')
                    if not (bridge.directory / 'auth.json').is_file():
                        raise ChatGPTAuthError('官方登录尚未保存，请重新绑定 Codex 账号')
                    bridge.expected_email = email.casefold()
                    # Only metadata is ours; native tokens remain owned by Codex.
                    self.accounts.store.write(attempt['profile'], {
                        'auth_type': 'codex', 'email': email, 'plan': account.get('planType'),
                        'status': 'connected', 'created_at': time.time()})
                    self.accounts.codex[attempt['profile']] = bridge
                    bridge.cache.update(email=email, plan=account.get('planType'))
                    attempt['status'] = 'completed'
                    self._stop_timer(attempt)
                    attempt['timer'] = asyncio.get_running_loop().call_later(
                        3600, lambda: self._background(self.cancel(identifier, expired=True)))
                except (ChatGPTAuthError, OSError) as exc:
                    attempt.update(status='failed', error=str(exc) if isinstance(exc, ChatGPTAuthError)
                                   else '账号资料保存失败，请重新绑定')
            elif login.get('status') in ('failed', 'cancelled'):
                attempt.update(status='failed', error='官方授权未完成，请重新获取验证码后再试')
            if attempt['status'] == 'failed':
                self._stop_timer(attempt)
                await bridge.close()
                self._remove_profile(attempt['profile'])
            return self._view(attempt)

    async def cancel(self, identifier, *, expired=False):
        attempt = self._get(identifier)
        async with attempt['lock']:
            if attempt['status'] == 'completed' and not attempt.get('claimed'):
                attempt['status'] = 'expired' if expired else 'cancelled'
                self._stop_timer(attempt)
                self.accounts.store.path(attempt['profile']).unlink(missing_ok=True)
                if self.accounts.codex.get(attempt['profile']) is attempt['bridge']:
                    self.accounts.codex.pop(attempt['profile'], None)
                await attempt['bridge'].close()
                self._remove_profile(attempt['profile'])
                return self._view(attempt)
            if attempt['status'] != 'pending':
                return self._view(attempt)
            attempt.update(status='expired' if expired else 'cancelled')
            self._stop_timer(attempt)
            await attempt['bridge'].cancel_login()
            self._remove_profile(attempt['profile'])
            return self._view(attempt)

    def pause(self):
        for identifier, attempt in list(self.attempts.items()):
            if attempt['status'] == 'pending' or attempt['status'] == 'completed' and not attempt.get('claimed'):
                self._background(self.cancel(identifier))

    async def close(self):
        self.pause()
        while self.cleanup_tasks:
            await asyncio.gather(*list(self.cleanup_tasks), return_exceptions=True)
