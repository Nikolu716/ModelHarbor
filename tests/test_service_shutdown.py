"""Exercise the real gateway entry point with an unfinished local SSE request."""
from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

import httpx
import yaml


ROOT = Path(__file__).resolve().parents[1]


class ServiceShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_cancels_long_stream_closes_socket_and_preserves_stats(self):
        closed = asyncio.Event()
        handlers = set()

        async def upstream(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            streaming = False
            try:
                headers = await reader.readuntil(b'\r\n\r\n')
                length = next((int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                               if line.lower().startswith(b'content-length:')), 0)
                if length:
                    await reader.readexactly(length)
                streaming = headers.startswith(b'POST ')
                if streaming:
                    first = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n'
                                 b'Transfer-Encoding: chunked\r\n\r\n'
                                 + f'{len(first):x}\r\n'.encode() + first + b'\r\n')
                    await writer.drain()
                    await reader.read()  # No EOF until the gateway closes its upstream.
                else:
                    payload = b'{"data":[{"id":"m"}]}'
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                                 + f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode()
                                 + payload)
                    await writer.drain()
            finally:
                if streaming:
                    closed.set()
                writer.close()
                await writer.wait_closed()
                handlers.discard(task)

        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        upstream_port = server.sockets[0].getsockname()[1]
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        try:
            with tempfile.TemporaryDirectory(prefix='modelharbor-shutdown-') as directory:
                root = Path(directory)
                config = root / 'config.yaml'
                config.write_text(yaml.safe_dump({
                    'gateway': {'host': '127.0.0.1', 'port': port,
                                'data_dir': str(root / 'data'), 'allowed_ips': ['127.0.0.1'],
                                'generation_probe_enabled': False, 'probe_interval': 86400},
                    'stations': [{'name': 'local-mock', 'base_url': f'http://127.0.0.1:{upstream_port}/v1',
                                  'api_key': 'fixture-only', 'models': ['m'],
                                  'probe_models': False, 'probe_chat': False}],
                }), encoding='utf-8')
                with (root / 'server.log').open('wb') as logfile:
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, str(ROOT / 'gateway.py'), '--config', str(config),
                        cwd=ROOT, stdout=logfile, stderr=logfile)
                    try:
                        async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                            for _ in range(100):
                                try:
                                    if (await client.get(base + '/healthz')).status_code == 200:
                                        break
                                except httpx.TransportError:
                                    pass
                                self.assertIsNone(process.returncode, 'Gateway exited during startup')
                                await asyncio.sleep(.1)
                            else:
                                self.fail('Gateway did not start')
                            async with client.stream('POST', base + '/v1/chat/completions',
                                                     json={'model': 'm', 'stream': True,
                                                           'messages': [{'role': 'user', 'content': 'fixture'}]},
                                                     timeout=None) as response:
                                self.assertEqual(response.status_code, 200)
                                chunks = response.aiter_raw()
                                first = await asyncio.wait_for(chunks.__anext__(), 5)
                                self.assertIn(b'hello', first)
                                self.assertFalse(closed.is_set())
                                status = (await client.get(base + '/admin/status')).json()
                                self.assertEqual(len(status['route_activity']['active_routes']), 1)
                                start = time.monotonic()
                                restart = await client.post(base + '/admin/service/restart')
                                self.assertEqual(restart.status_code, 202)
                                await asyncio.wait_for(process.wait(), 18)
                                self.assertGreater(time.monotonic() - start, 9)
                                self.assertLess(time.monotonic() - start, 18)
                                self.assertEqual(process.returncode, 0)
                                await asyncio.wait_for(closed.wait(), 2)
                        with closing(sqlite3.connect(root / 'data/stats.sqlite3')) as database:
                            events = [json.loads(row[0]) for row in database.execute('SELECT payload FROM events')]
                            self.assertEqual(len(events), 1)
                            self.assertEqual(events[0]['model'], 'm')
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
        finally:
            server.close()
            await server.wait_closed()
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)
