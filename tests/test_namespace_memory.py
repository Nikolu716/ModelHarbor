"""call_id -> namespace 学习与回放补回（cockpit-tools #1856 网关侧兜底）。"""
from __future__ import annotations

import json
import unittest

from app.proxy import _NamespaceStreamScanner
from app.wire import NamespaceMemory


class NamespaceMemoryTests(unittest.TestCase):
    def test_learn_from_response_and_restore(self) -> None:
        mem = NamespaceMemory()
        mem.remember_response({
            "id": "resp_1",
            "output": [
                {"type": "function_call", "call_id": "call_1",
                 "name": "open", "namespace": "spine", "arguments": "{}"},
                {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "ok"}]},
            ],
        })
        body = {
            "model": "gpt-5.4",
            "input": [
                {"type": "function_call", "call_id": "call_1", "name": "open",
                 "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_1",
                 "output": "result"},
            ],
        }
        self.assertTrue(mem.restore(body))
        self.assertEqual(body["input"][0]["namespace"], "spine")
        # function_call_output 不补 namespace
        self.assertNotIn("namespace", body["input"][1])

    def test_no_restore_for_unknown_call(self) -> None:
        mem = NamespaceMemory()
        body = {"input": [{"type": "function_call", "call_id": "call_x",
                           "name": "open", "arguments": "{}"}]}
        self.assertFalse(mem.restore(body))
        self.assertNotIn("namespace", body["input"][0])

    def test_keeps_existing_namespace(self) -> None:
        mem = NamespaceMemory()
        mem.remember("call_1", "spine")
        body = {"input": [{"type": "function_call", "call_id": "call_1",
                           "name": "open", "namespace": "other", "arguments": "{}"}]}
        self.assertFalse(mem.restore(body))
        self.assertEqual(body["input"][0]["namespace"], "other")

    def test_learns_from_sse_events(self) -> None:
        mem = NamespaceMemory()
        scanner = _NamespaceStreamScanner(mem)
        sse = (
            'event: response.output_item.done\n'
            'data: {"type":"response.output_item.done","item":'
            '{"type":"function_call","call_id":"call_2","name":"open",'
            '"namespace":"mcp__example","arguments":"{}"}}\n\n'
        ).encode("utf-8")
        scanner.feed(sse)
        body = {"input": [{"type": "function_call", "call_id": "call_2",
                           "name": "open", "arguments": "{}"}]}
        self.assertTrue(mem.restore(body))
        self.assertEqual(body["input"][0]["namespace"], "mcp__example")

    def test_learns_from_completed_event(self) -> None:
        mem = NamespaceMemory()
        scanner = _NamespaceStreamScanner(mem)
        sse = (
            'data: {"type":"response.completed","response":{"output":['
            '{"type":"custom_tool_call","call_id":"c3","name":"x",'
            '"namespace":"ns3","arguments":"{}"}]}}\n\n'
        ).encode("utf-8")
        scanner.feed(sse)
        body = {"input": [{"type": "custom_tool_call", "call_id": "c3",
                           "name": "x", "arguments": "{}"}]}
        self.assertTrue(mem.restore(body))
        self.assertEqual(body["input"][0]["namespace"], "ns3")

    def test_scanner_skips_irrelevant_chunks(self) -> None:
        mem = NamespaceMemory()
        scanner = _NamespaceStreamScanner(mem)
        scanner.feed(b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n')
        self.assertEqual(mem.namespace_for("anything"), None)

    def test_evicts_oldest_when_full(self) -> None:
        mem = NamespaceMemory(max_entries=2)
        mem.remember("a", "ns_a")
        mem.remember("b", "ns_b")
        mem.remember("c", "ns_c")
        self.assertEqual(mem.namespace_for("a"), None)
        self.assertEqual(mem.namespace_for("c"), "ns_c")


if __name__ == "__main__":
    unittest.main()
