"""Tests for bounded event retention and the paginated admin log API."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.stats import (
    EVENT_DETAIL_MAXLEN,
    EVENT_MAXLEN,
    EVENT_NAME_MAXLEN,
    MODEL_BUCKET_MAXLEN,
    MODEL_CANDIDATE_MAXLEN,
    STATS_RETENTION_DAYS,
    EventLog,
)
from app.web.admin import create_admin_router


def event(index: int) -> dict:
    return {
        "ts": float(index),
        "station": f"station-{index % 2}",
        "model": "model-a",
        "ok": True,
        "latency_ms": index,
        "detail": "",
        "attempts": 1,
        "tokens": index,
    }


class EventRetentionTests(unittest.TestCase):
    def test_legacy_load_rebuilds_unattributed_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.json"
            today = datetime.now().strftime("%Y-%m-%d")
            failed = event(3)
            failed.update({"station": "-", "ok": False, "ts": datetime.now().timestamp()})
            path.write_text(json.dumps({
                "events": [event(1), event(2), failed],
                "stations": {},
                "daily": {
                    today: {
                        "alpha": {"calls": 2, "failures": 0, "ms": 2.0},
                    },
                },
                "daily_models": {},
                # Pre-schema builds could persist a partial value after an
                # upgrade; migration must not trust it.
                "daily_totals": {today: {"calls": 2, "failures": 0}},
            }), encoding="utf-8")

            eventlog = EventLog(directory)
            eventlog.load()
            stats = eventlog.daily_stats()
            self.assertEqual(stats["total_calls"], 3)
            self.assertEqual(stats["total_failures"], 1)

    def test_legacy_migration_does_not_downgrade_existing_totals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.json"
            today = datetime.now().strftime("%Y-%m-%d")
            path.write_text(json.dumps({
                "events": [],
                "stations": {},
                "daily": {today: {"alpha": {"calls": 90, "failures": 0, "ms": 0}}},
                "daily_models": {},
                "daily_totals": {today: {"calls": 100, "failures": 10}},
            }), encoding="utf-8")
            eventlog = EventLog(directory)
            eventlog.load()
            stats = eventlog.daily_stats()
            self.assertEqual(stats["total_calls"], 100)
            self.assertEqual(stats["total_failures"], 10)

    def test_event_text_and_aggregate_storage_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            eventlog.add(
                "s" * (EVENT_NAME_MAXLEN + 20),
                "m" * (EVENT_NAME_MAXLEN + 20),
                False,
                1.0,
                "d" * (EVENT_DETAIL_MAXLEN + 20),
            )
            stored = eventlog.events[-1]
            self.assertEqual(len(stored["station"]), EVENT_NAME_MAXLEN)
            self.assertEqual(len(stored["model"]), EVENT_NAME_MAXLEN)
            self.assertEqual(len(stored["detail"]), EVENT_DETAIL_MAXLEN)

            start = datetime(2026, 1, 1)
            for offset in range(STATS_RETENTION_DAYS + 4):
                date = (start + timedelta(days=offset)).strftime("%Y-%m-%d")
                eventlog.daily[date] = {}
                eventlog.daily_models[date] = {}
                eventlog.daily_totals[date] = {"calls": 0, "failures": 0}
                eventlog.daily_hours[date] = {}
            eventlog.persist()
            self.assertEqual(len(eventlog.daily), STATS_RETENTION_DAYS)
            self.assertNotIn("2026-01-01", eventlog.daily)

    def test_hourly_totals_and_model_buckets_do_not_depend_on_event_ring(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            count = EVENT_MAXLEN + 25
            for index in range(count):
                eventlog.add("alpha", f"model-{index}", True, 1.0, tokens=1)

            stats = eventlog.daily_stats()
            station = stats["stations"][0]
            hour = datetime.now().hour
            self.assertEqual(len(eventlog.events), EVENT_MAXLEN)
            self.assertEqual(stats["total_calls"], count)
            self.assertEqual(stats["timeline"]["alpha"][hour], count)
            self.assertLessEqual(len(station["models"]), MODEL_BUCKET_MAXLEN)
            self.assertIn("其他模型", {model["model"] for model in station["models"]})
            raw_models = eventlog.daily_models[datetime.now().strftime("%Y-%m-%d")]["alpha"]
            self.assertLessEqual(len(raw_models), MODEL_CANDIDATE_MAXLEN)

    def test_late_high_usage_model_can_enter_the_retained_breakdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            for index in range(MODEL_BUCKET_MAXLEN):
                eventlog.add("alpha", f"early-{index}", True, 1.0, tokens=1)
            eventlog.persist()
            eventlog.add("alpha", "late-heavy", True, 1.0, tokens=1000)

            station = eventlog.daily_stats()["stations"][0]
            models = {model["model"] for model in station["models"]}
            self.assertIn("late-heavy", models)
            self.assertLessEqual(len(models), MODEL_BUCKET_MAXLEN)

    def test_unattributed_total_failure_is_counted_globally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory)
            eventlog.add("-", "model-a", False, 0, "all upstreams failed")
            stats = eventlog.daily_stats()
            self.assertEqual(stats["total_calls"], 1)
            self.assertEqual(stats["total_failures"], 1)
            self.assertEqual(stats["stations"], [])

    def test_load_keeps_newest_events_from_oversized_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.json"
            path.write_text(json.dumps({
                "events": [event(i) for i in range(8)],
                "stations": {},
                "daily": {},
                "daily_models": {},
            }), encoding="utf-8")

            eventlog = EventLog(directory, maxlen=3)
            eventlog.load()

            self.assertEqual([e["ts"] for e in eventlog.events], [5.0, 6.0, 7.0])

    def test_retention_and_persistence_are_hard_capped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory, maxlen=EVENT_MAXLEN + 100)
            self.assertEqual(eventlog.maxlen, EVENT_MAXLEN)
            for i in range(EVENT_MAXLEN + 25):
                eventlog.events.append(event(i))
            eventlog.persist()

            stored = json.loads(eventlog.path.read_text(encoding="utf-8"))
            self.assertEqual(len(stored["events"]), EVENT_MAXLEN)
            self.assertEqual(stored["events"][0]["ts"], 25.0)
            self.assertEqual(stored["events"][-1]["ts"], 524.0)

    def test_paginated_is_newest_first_and_clamps_stale_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            eventlog = EventLog(directory, maxlen=10)
            eventlog.events.extend(event(i) for i in range(7))

            first = eventlog.paginated(page=1, page_size=3)
            second = eventlog.paginated(page=2, page_size=3)
            stale = eventlog.paginated(page=99, page_size=3)

            self.assertEqual([e["ts"] for e in first["events"]], [6.0, 5.0, 4.0])
            self.assertEqual([e["ts"] for e in second["events"]], [3.0, 2.0, 1.0])
            self.assertEqual(first | {"events": []}, {
                "events": [], "total": 7, "page": 1, "page_size": 3,
                "pages": 3, "max_events": 10,
            })
            self.assertEqual(stale["page"], 3)
            self.assertEqual([e["ts"] for e in stale["events"]], [0.0])


class AdminLogPaginationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.eventlog = EventLog(self.tempdir.name, maxlen=10)
        self.eventlog.events.extend(event(i) for i in range(7))
        app = FastAPI()
        # The logs endpoint only needs EventLog; the remaining collaborators
        # are captured by other routes and are intentionally not exercised.
        app.include_router(create_admin_router(
            registry=None, gateway=None, monitor=None,
            eventlog=self.eventlog, notifier=None,
        ))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.tempdir.cleanup()

    def test_page_contract(self) -> None:
        response = self.client.get("/admin/logs", params={
            "page": 2, "page_size": 3,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([e["ts"] for e in body["events"]], [3.0, 2.0, 1.0])
        self.assertEqual({k: body[k] for k in (
            "total", "page", "page_size", "pages", "max_events",
        )}, {
            "total": 7, "page": 2, "page_size": 3,
            "pages": 3, "max_events": 10,
        })

    def test_tail_remains_a_clamped_compatibility_alias(self) -> None:
        response = self.client.get("/admin/logs", params={"tail": 2})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([e["ts"] for e in body["events"]], [6.0, 5.0])
        self.assertEqual(body["page"], 1)
        self.assertEqual(body["page_size"], 2)

        oversized = self.client.get("/admin/logs", params={"tail": 999})
        self.assertEqual(oversized.status_code, 200)
        self.assertEqual(oversized.json()["page_size"], EVENT_MAXLEN)

    def test_query_validation_rejects_invalid_page_values(self) -> None:
        self.assertEqual(
            self.client.get("/admin/logs", params={"page": 0}).status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/admin/logs", params={"page_size": 0}).status_code,
            422,
        )
        self.assertEqual(
            self.client.get("/admin/logs", params={"page_size": 501}).status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()
