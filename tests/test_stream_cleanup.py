"""Regression tests for stream cleanup and wedged connection pools."""
from __future__ import annotations

import asyncio
import tempfile
import time
import unittest

import anyio
import httpx

from app.config import GatewayConfig, StationConfig
from app.proxy import _StreamRouteLease, OpenAIProxy
from app.state import Registry
from app.stats import EventLog
from app.router import Router, RoutePermit
from app.errors import UpstreamError
from app.health import HealthMonitor
from app.state import STATUS_DOWN


class _AsyncCloser:
    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.started = False
        self.closed = False

    async def aclose(self) -> None:
        self.started = True
        await asyncio.sleep(self.delay)
        self.closed = True


class _RegistryStub:
    def __init__(self) -> None:
        self.ended: list[tuple[int, str, str]] = []

    def start_cleanup(self, coroutine):
        return asyncio.create_task(coroutine)

    def end_route(self, route_id: int, outcome: str, error: str) -> None:
        self.ended.append((route_id, outcome, error))


class _RouterStub:
    def __init__(self) -> None:
        self.released = 0

    def release(self, station, permit) -> None:
        self.released += 1


class StreamCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lease_close_completes_when_caller_is_cancelled(self) -> None:
        """A cancelled ASGI task must not abort the upstream socket close.

        httpcore marks the stream closed before releasing the socket. When the
        close runs inside Starlette's cancelled task group its first await is
        interrupted, the socket is never released, and every later request hits
        PoolTimeout.
        """
        registry = _RegistryStub()
        router = _RouterStub()
        resp = _AsyncCloser()
        stream_iter = _AsyncCloser()
        lease = _StreamRouteLease(registry, router, object(), 7, resp, stream_iter, RoutePermit(asyncio.Semaphore(0)))

        async with anyio.create_task_group() as task_group:
            async def worker() -> None:
                await lease.close("cancelled", "client_stream_closed")

            task_group.start_soon(worker)
            await asyncio.sleep(0.02)
            task_group.cancel_scope.cancel()

        for _ in range(50):
            if resp.closed and stream_iter.closed and registry.ended:
                break
            await asyncio.sleep(0.02)

        self.assertTrue(resp.started)
        self.assertTrue(resp.closed)
        self.assertTrue(stream_iter.closed)
        self.assertEqual(registry.ended, [(7, "cancelled", "client_stream_closed")])
        self.assertEqual(router.released, 1)

    async def test_lease_close_is_idempotent(self) -> None:
        registry = _RegistryStub()
        router = _RouterStub()
        resp = _AsyncCloser(delay=0)
        stream_iter = _AsyncCloser(delay=0)
        lease = _StreamRouteLease(registry, router, object(), 3, resp, stream_iter, RoutePermit(asyncio.Semaphore(0)))

        await lease.close("success", "")
        await lease.close("cancelled", "late")

        self.assertEqual(registry.ended, [(3, "success", "")])
        self.assertEqual(router.released, 1)


class ClientResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_closes_retired_clients_and_ignores_stale_timeouts(self):
        registry = Registry(GatewayConfig(), [StationConfig(name="a", base_url="https://a.invalid", api_key="k")])
        st = registry.get("a")
        old = registry.ensure_client(st)
        registry.reset_client(st, grace=3600)
        new = registry.ensure_client(st)
        self.assertFalse(registry.reset_client(st, expected_client=old))
        self.assertIs(st.client, new)
        await asyncio.wait_for(registry.close_clients(), 2)
        self.assertTrue(old.is_closed and new.is_closed)
        self.assertFalse(registry._retired_clients)
        self.assertFalse(registry._cleanup_tasks)

    async def test_reset_client_swaps_wedged_client_and_drains_old(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gateway = GatewayConfig(data_dir=directory)
            cfg = StationConfig(
                name="alpha",
                base_url="https://alpha.invalid/v1",
                api_key="k",
                models=["m"],
            )
            registry = Registry(gateway, [cfg])
            station = registry.get("alpha")
            assert station is not None

            old = registry.ensure_client(station)
            self.assertTrue(registry.reset_client(station, grace=0.01))
            self.assertFalse(registry.reset_client(station, grace=0.01))

            replacement = registry.ensure_client(station)
            self.assertIsNot(replacement, old)
            await asyncio.sleep(0.05)
            self.assertTrue(old.is_closed)
            await registry.close_clients()


class ForwardCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.gateway = GatewayConfig(data_dir=self.directory.name, max_attempts=2,
                                     max_rounds=1, retry_backoff=0, sse_keepalive_interval=.01)
        self.registry = Registry(self.gateway, [StationConfig(
            name="a", base_url="https://a.invalid/v1", api_key="k", models=["m"], max_concurrency=1)])
        self.st = self.registry.get("a")
        self.proxy = OpenAIProxy(self.registry, self.gateway, EventLog(self.directory.name))

    async def asyncTearDown(self):
        await self.registry.close_clients()
        self.directory.cleanup()

    async def test_cancel_buffer_and_error_body_releases_response(self):
        for status in (200, 503):
            with self.subTest(status=status):
                reading, closed = asyncio.Event(), asyncio.Event()
                class Stream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'{'
                        reading.set()
                        await asyncio.Event().wait()
                    async def aclose(self):
                        await asyncio.sleep(.02)
                        closed.set()
                self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda req: httpx.Response(status, stream=Stream())))
                task = asyncio.create_task(self.proxy._forward_json('/chat/completions', {'model': 'm'}, 'm'))
                await asyncio.wait_for(reading.wait(), 1)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await asyncio.wait_for(closed.wait(), 1)
                self.assertFalse(self.registry.route_activity_snapshot()['active'])
                self.assertFalse(self.st.semaphore.locked())
                self.assertEqual(self.st.failures, 0)
                await self.st.client.aclose()

    async def test_cancel_during_retry_wait_does_not_acquire_next_station(self):
        other = self.registry.add_station(StationConfig(
            name="b", base_url="https://b.invalid/v1", api_key="k", models=["m"], max_concurrency=1))
        for method in (self.proxy._forward_json, self.proxy._forward_stream):
            self.gateway.retry_backoff = 1
            waiting = asyncio.Event()
            async def fail(req):
                waiting.set()
                return httpx.Response(503, json={'error': {'message': 'unavailable'}})
            self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
            self.proxy.router.pick = lambda *args: [self.st, other]
            task = asyncio.create_task(method('/chat/completions', {'model': 'm'}, 'm'))
            await asyncio.wait_for(waiting.wait(), 1)
            await asyncio.sleep(.03)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.assertFalse(self.registry.ensure_semaphore(other).locked())
            self.assertFalse(self.registry.route_activity_snapshot()['active'])
            await self.st.client.aclose()

    async def test_edit_concurrency_releases_original_permit(self):
        permit = await self.proxy.router.acquire(self.st)
        old = permit.semaphore
        self.registry.update_station('a', {'max_concurrency': 2})
        self.proxy.router.release(self.st, permit)
        self.assertFalse(old.locked())
        one = await self.proxy.router.acquire(self.st)
        two = await self.proxy.router.acquire(self.st)
        self.assertIsNone(await self.proxy.router.acquire(self.st))
        self.proxy.router.release(self.st, one)
        self.proxy.router.release(self.st, two)

    async def test_cancel_at_implicit_eof_close_still_releases_transport(self):
        started, closed = asyncio.Event(), asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{}'
            async def aclose(self):
                started.set()
                await asyncio.sleep(.04)
                closed.set()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, stream=Stream())),
            event_hooks={'response': [self.registry._protect_response]})
        task = asyncio.create_task(self.proxy._forward_json('/chat/completions', {'model': 'm'}, 'm'))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(closed.wait(), 1)
        self.assertFalse(self.registry.route_activity_snapshot()['active'])
        self.assertFalse(self.st.semaphore.locked())

    async def test_stream_recovery_waits_for_completion_and_failure_reopens(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                class Stream(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                        if fail:
                            raise httpx.ReadError('connection reset')
                        yield b'data: [DONE]\n\n'
                self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda req: httpx.Response(200, stream=Stream())))
                self.st.status = STATUS_DOWN
                self.st.consecutive_failures = 216
                self.st.down_since = time.time() - self.gateway.cool_down - 1
                self.st.recovery_attempt_at = 0
                response, _ = await self.proxy._forward_stream('/chat/completions', {'model': 'm'}, 'm')
                self.assertEqual(self.st.status, STATUS_DOWN)
                self.assertIsNone(await self.proxy.router.acquire(self.st))
                payload = b''.join([chunk async for chunk in response.body_iterator])
                self.assertFalse(self.st.recovery_inflight)
                if fail:
                    self.assertIn(b'error', payload)
                    self.assertEqual(self.st.status, STATUS_DOWN)
                    self.assertGreater(self.st.down_since, time.time() - 2)
                    self.assertEqual(self.proxy.router.pick('m'), [])
                else:
                    self.assertEqual(self.st.status, 'up')
                    self.assertEqual(self.st.consecutive_failures, 0)
                await self.st.client.aclose()

    async def test_broken_error_body_is_retried_and_released(self):
        closed = asyncio.Event()
        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{'
                raise httpx.ReadError('error body interrupted')
            async def aclose(self):
                closed.set()
        self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(503, stream=Stream())))
        response, transient = await self.proxy._forward_stream('/chat/completions', {'model': 'm'}, 'm')
        self.assertEqual(response.status_code, 502)
        self.assertTrue(transient)
        self.assertTrue(closed.is_set())
        self.assertEqual(self.st.consecutive_failures, 1)
        self.assertFalse(self.st.semaphore.locked())

    async def test_pool_timeout_does_not_trip_breaker_for_stream_or_probes(self):
        async def fail(req):
            raise httpx.PoolTimeout('pool full', request=req)
        for action in ('stream', 'generation', 'connection', 'catalog'):
            with self.subTest(action=action):
                self.st.client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
                monitor = HealthMonitor(self.registry, self.gateway)
                if action == 'stream':
                    response, _ = await self.proxy._forward_stream('/chat/completions', {'model': 'm'}, 'm')
                    self.assertEqual(response.status_code, 502)
                elif action == 'generation':
                    result = await monitor.test_generation(self.st)
                    self.assertTrue(result['skipped'])
                elif action == 'connection':
                    self.st.cfg.probe_models = False
                    await monitor.probe(self.st)
                else:
                    self.st.cfg.probe_models = True
                    await monitor.discover_models(self.st, manual=True)
                self.assertEqual(self.st.consecutive_failures, 0)
                self.assertEqual(self.st.calls, 0)
                self.assertEqual(self.st.failures, 0)
                self.assertIsNone(self.st.client)

    async def test_cancelled_asgi_streams_do_not_exhaust_real_pool(self):
        """Repeated downstream disconnects must leave a one-slot TCP pool usable."""
        writers = set()
        handlers = set()
        async def upstream(reader, writer):
            writers.add(writer)
            handlers.add(asyncio.current_task())
            try:
                while True:
                    header = await reader.readuntil(b'\r\n\r\n')
                    length = next((int(line.split(b':', 1)[1]) for line in header.splitlines()
                                   if line.lower().startswith(b'content-length:')), 0)
                    await reader.readexactly(length)
                    if header.startswith(b'GET /ok '):
                        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
                        await writer.drain()
                        continue
                    chunk = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n'
                                 + format(len(chunk), 'x').encode() + b'\r\n' + chunk + b'\r\n')
                    await writer.drain()
                    await reader.read()  # Keep the body open until gateway closes the socket.
                    break
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                writers.discard(writer)
                handlers.discard(asyncio.current_task())
        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        self.st.cfg.base_url = f'http://127.0.0.1:{port}/v1'
        self.st.client = httpx.AsyncClient(limits=httpx.Limits(max_connections=1),
                                          event_hooks={'response': [self.registry._protect_response]})
        try:
            for index in range(12):
                response, _ = await self.proxy._forward_stream('/chat/completions', {'model': 'm', 'stream': True}, 'm')
                sent = asyncio.Event()
                mode = index % 3
                async def send(message):
                    if mode == 1 and message['type'] == 'http.response.start':
                        raise OSError('downstream gone before body')
                    if message['type'] == 'http.response.body':
                        sent.set()
                        if mode == 2:
                            raise OSError('downstream write failed')
                async def receive():
                    await sent.wait()
                    await asyncio.sleep(.025)  # Exercise a pending upstream read / keepalive.
                    return {'type': 'http.disconnect'}
                try:
                    await response({'type': 'http', 'asgi': {'spec_version': '2.3' if mode == 0 else '2.4'}}, receive, send)
                except Exception:
                    if mode == 0:
                        raise
                for _ in range(100):
                    if not self.registry.route_activity_snapshot()['active']:
                        break
                    await asyncio.sleep(.005)
                self.assertFalse(self.registry.route_activity_snapshot()['active'])
                self.assertFalse(self.st.semaphore.locked())
                check = await self.st.client.get(f'http://127.0.0.1:{port}/ok', timeout=.5)
                self.assertEqual(check.status_code, 200)
            self.assertEqual(self.st.consecutive_failures, 0)
        finally:
            await self.registry.close_clients()
            server.close()
            await server.wait_closed()
            for writer in list(writers):
                writer.close()
            await asyncio.gather(*list(handlers), return_exceptions=True)


class BreakerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_breaker_gets_one_trial_without_synthetic_generation(self):
        gateway = GatewayConfig(cool_down=60)
        registry = Registry(gateway, [StationConfig(name=name, base_url='https://a.invalid/v1', api_key='k', models=['m'])
                                     for name in ('healthy', 'recovering')])
        router = Router(registry, gateway)
        st = registry.get('recovering')
        st.status, st.consecutive_failures = STATUS_DOWN, 216
        st.down_since = time.time() - 61
        st._update_score()
        self.assertIs(router.pick('m')[0], st)
        permit = await router.acquire(st)
        self.assertIsNotNone(permit)
        self.assertIsNone(await router.acquire(st))
        self.assertEqual(st.consecutive_failures, 216)
        router.release(st, permit)
        self.assertIsNone(await router.acquire(st))  # Cancellation / 4xx cannot trigger a storm.
        st.recovery_attempt_at -= 61
        permit = await router.acquire(st)
        self.assertIsNotNone(permit)
        st.record_success(.2)
        router.release(st, permit)
        self.assertEqual(st.consecutive_failures, 0)
        self.assertEqual(st.status, 'up')
        self.assertEqual(st.probe_chat_at, 0)

    async def test_trial_respects_cooldown_quota_and_failure_reopens_breaker(self):
        gateway = GatewayConfig(cool_down=60)
        registry = Registry(gateway, [StationConfig(name='a', base_url='https://a.invalid/v1', api_key='k', models=['m'])])
        router, st = Router(registry, gateway), registry.get('a')
        st.status, st.consecutive_failures, st.down_since = STATUS_DOWN, 216, time.time()
        self.assertEqual(router.pick('m'), [])
        self.assertIsNone(await router.acquire(st))
        st.down_since -= 61
        st.quota_exhausted = True
        self.assertEqual(router.pick('m'), [])
        st.quota_exhausted = False
        st.rate_limit_until = time.time() + 60
        self.assertIsNone(await router.acquire(st))
        st.rate_limit_until = 0
        permit = await router.acquire(st)
        st.mark_transient_failure(UpstreamError('a', 503, 'unavailable', retryable=True), gateway)
        router.release(st, permit)
        self.assertEqual(router.pick('m'), [])
        self.assertIsNone(await router.acquire(st))

    async def test_pool_timeout_is_local_and_not_charged_to_station(self) -> None:
        """A PoolTimeout never reached upstream, so it must not trip the breaker."""
        with tempfile.TemporaryDirectory() as directory:
            gateway = GatewayConfig(
                data_dir=directory, max_attempts=1, max_rounds=1)
            cfg = StationConfig(
                name="alpha",
                base_url="https://alpha.invalid/v1",
                api_key="k",
                models=["m"],
            )
            registry = Registry(gateway, [cfg])
            station = registry.get("alpha")
            assert station is not None

            async def pool_full(request: httpx.Request) -> httpx.Response:
                raise httpx.PoolTimeout("pool full", request=request)

            station.client = httpx.AsyncClient(
                transport=httpx.MockTransport(pool_full))
            proxy = OpenAIProxy(registry, gateway, EventLog(directory))

            response, transient = await proxy._forward_json(
                "/chat/completions", {"model": "m"}, "m")

            self.assertEqual(response.status_code, 502)
            self.assertTrue(transient)
            self.assertEqual(station.consecutive_failures, 0)
            self.assertEqual(station.failures, 0)
            self.assertEqual(station.calls, 0)
            self.assertEqual(station.status, "up")
            self.assertIsNone(station.client)
            await registry.close_clients()
