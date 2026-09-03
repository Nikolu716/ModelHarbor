"""Cache-hit usage parsing and daily aggregation."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from app.proxy import _StreamUsageParser, _extract_usage, _parse_usage
from app.stats import EventLog


class CacheUsageParseTests(unittest.TestCase):
    def test_openai_chat_cached_tokens(self) -> None:
        usage = _parse_usage({
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 80},
        })
        self.assertIsNotNone(usage)
        self.assertEqual(usage.tokens, 120)
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.cache_read, 80)
        self.assertIsNone(usage.cache_write)

    def test_responses_input_details(self) -> None:
        usage = _extract_usage(json.dumps({
            "response": {
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "input_tokens_details": {"cached_tokens": 0},
                },
            },
        }).encode())
        self.assertIsNotNone(usage)
        self.assertEqual(usage.tokens, 60)
        self.assertEqual(usage.cache_read, 0)

    def test_anthropic_compat_fields(self) -> None:
        usage = _parse_usage({
            "input_tokens": 200,
            "output_tokens": 15,
            "cache_read_input_tokens": 160,
            "cache_creation_input_tokens": 12,
        })
        self.assertEqual(usage.cache_read, 160)
        self.assertEqual(usage.cache_write, 12)
        self.assertEqual(usage.tokens, 215)

    def test_stream_usage_keeps_cache_fields(self) -> None:
        parser = _StreamUsageParser()
        parser.feed(
            b'data: {"usage":{"prompt_tokens":40,"completion_tokens":5,'
            b'"total_tokens":45,"prompt_tokens_details":{"cached_tokens":30}}}\n\n'
        )
        self.assertIsNotNone(parser.last)
        self.assertEqual(parser.last.tokens, 45)
        self.assertEqual(parser.last.cache_read, 30)

    def test_missing_cache_fields_stay_unknown(self) -> None:
        usage = _parse_usage({"total_tokens": 9})
        self.assertEqual(usage.tokens, 9)
        self.assertIsNone(usage.cache_read)
        self.assertIsNone(usage.input_tokens)


class CacheStatsAggregateTests(unittest.TestCase):
    def test_hit_rate_uses_only_cache_aware_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            eventlog.add("alpha", "gpt", True, 10.0, tokens=120,
                         input_tokens=100, cache_read=80)
            eventlog.add("alpha", "gpt", True, 12.0, tokens=30)
            stats = eventlog.daily_stats()
            station = stats["stations"][0]
            self.assertEqual(stats["cache_read"], 80)
            self.assertEqual(stats["cache_input"], 100)
            self.assertEqual(stats["cache_hit_rate"], 0.8)
            self.assertTrue(stats["cache_known"])
            self.assertEqual(station["cache_hit_rate"], 0.8)
            self.assertEqual(station["models"][0]["cache_read"], 80)

    def test_zero_cache_is_a_known_miss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            eventlog.add("alpha", "gpt", True, 10.0, tokens=20,
                         input_tokens=16, cache_read=0)
            stats = eventlog.daily_stats()
            self.assertEqual(stats["cache_hit_rate"], 0.0)
            self.assertTrue(stats["cache_known"])

    def test_legacy_days_have_unknown_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            eventlog.add("alpha", "gpt", True, 10.0, tokens=20)
            stats = eventlog.daily_stats()
            self.assertFalse(stats["cache_known"])
            self.assertIsNone(stats["cache_hit_rate"])


class CacheCompareWindowTests(unittest.TestCase):
    def test_hour_best_hour_and_recent_days(self) -> None:
        now = datetime.now().replace(minute=20, second=0, microsecond=0)
        current = now.timestamp()
        previous = (now - timedelta(hours=1)).timestamp()
        yesterday = (now - timedelta(days=1)).replace(
            hour=15, minute=0, second=0, microsecond=0).timestamp()

        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            with patch("app.stats.time.time", return_value=previous):
                eventlog.add("alpha", "gpt", True, 10.0, tokens=200,
                             input_tokens=160, cache_read=80)
                eventlog.add("beta", "gpt", True, 10.0, tokens=200,
                             input_tokens=100, cache_read=95)
            with patch("app.stats.time.time", return_value=current):
                eventlog.add("alpha", "gpt", True, 10.0, tokens=200,
                             input_tokens=100, cache_read=90)
                eventlog.add("beta", "gpt", True, 10.0, tokens=200,
                             input_tokens=100, cache_read=10)
            with patch("app.stats.time.time", return_value=yesterday):
                eventlog.add("gamma", "gpt", True, 10.0, tokens=200,
                             input_tokens=200, cache_read=180)

            compare = eventlog.cache_compare(now)
            hour = {row["name"]: row for row in compare["windows"]["hour"]}
            best = {row["name"]: row for row in compare["windows"]["best_hour"]}
            week = {row["name"]: row for row in compare["windows"]["d7"]}

            self.assertEqual(compare["best"]["hour"], "alpha")
            self.assertEqual(hour["alpha"]["cache_hit_rate"], 0.9)
            self.assertEqual(hour["beta"]["cache_hit_rate"], 0.1)
            self.assertEqual(compare["best"]["best_hour"], "beta")
            self.assertEqual(best["beta"]["cache_hit_rate"], 0.95)
            self.assertEqual(week["gamma"]["cache_hit_rate"], 0.9)
            self.assertTrue(week["gamma"]["cache_known"])

            eventlog.persist()
            reloaded = EventLog(directory)
            reloaded.load()
            reloaded_compare = reloaded.cache_compare(now)
            self.assertEqual(reloaded_compare["best"]["best_hour"], "beta")
            self.assertEqual(
                reloaded.daily_stats()["cache_compare"]["best"]["hour"], "alpha")


if __name__ == "__main__":
    unittest.main()
