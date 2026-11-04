"""Rename name-keyed account data together, with crash recovery before startup."""
from __future__ import annotations

import copy
import dataclasses
import json
import sqlite3
from contextlib import closing
from pathlib import Path

FILES = ("stations.json", "model-mappings.json", "stats.sqlite3", "visual-tests.sqlite3", "juice.json")
LOG_FIELDS = ("events", "station_counters", "daily", "daily_models", "daily_hours",
              "hourly_cache", "_runtime", "_pending", "_reported_usage")


def _backup(source, target):
    if source.suffix == ".sqlite3":
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
    else:
        target.write_bytes(source.read_bytes())


def _clean(directory):
    # Fixed children only: no recursive delete or path from a journal document.
    for name in (*FILES, "prepared.json", "committed"):
        (directory / name).unlink(missing_ok=True)
    directory.rmdir()


def recover_rename(root):
    root = Path(root).resolve()
    journal = root / ".station-rename"
    if not journal.exists():
        return
    if journal.is_symlink() or journal.resolve().parent != root:
        raise OSError("Invalid station rename journal")
    manifest = journal / "prepared.json"
    if manifest.exists() and not (journal / "committed").exists():
        existing = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(existing, list) or any(name not in FILES for name in existing):
            raise ValueError("Invalid station rename recovery record")
        for name in FILES:
            target = root / name
            if name in existing:
                _backup(journal / name, target)
            else:
                target.unlink(missing_ok=True)
                if target.suffix == ".sqlite3":
                    for suffix in ("-wal", "-shm"):
                        Path(str(target) + suffix).unlink(missing_ok=True)
        (journal / "committed").write_text("rollback-complete", encoding="ascii")
    _clean(journal)


def _prepare(root):
    root = Path(root).resolve()
    recover_rename(root)
    journal = root / ".station-rename"
    journal.mkdir()
    existing = []
    try:
        for name in FILES:
            source = root / name
            if source.is_file():
                _backup(source, journal / name)
                existing.append(name)
        (journal / "prepared.json").write_text(json.dumps(existing), encoding="utf-8")
    except BaseException:
        _clean(journal)
        raise
    return journal


def rename_account_station(registry, eventlog, station_store, visual_store, runner, old, candidate):
    """No await: other account requests cannot observe a partial rename."""
    new = candidate.name
    st = registry.get(old)
    eventlog.load()
    if eventlog._read_only:
        raise ValueError("历史统计处于保护状态，恢复存储后再修改渠道名称")
    keyed = [eventlog.station_counters, eventlog._runtime]
    keyed.extend(values for field in ("daily", "daily_models", "daily_hours", "hourly_cache")
                 for values in getattr(eventlog, field).values())
    if any(new in table for table in keyed):
        raise ValueError("新名称已有历史统计，请使用其他名称以免覆盖历史数据")
    with visual_store.connect() as db:
        if any(db.execute("SELECT 1 FROM " + table + " WHERE station=?", (new,)).fetchone()
               for table in ("results", "schedules")):
            raise ValueError("新名称已有测试记录，请使用其他名称")
    mappings = registry.model_mappings
    if mappings.error:
        raise ValueError("模型映射配置需要先修复，再修改渠道名称")
    juice = getattr(registry, "juice_registry", None)
    if juice and any(fp.station == new for fp in juice.fingerprints.values()):
        raise ValueError("新名称已有模型指纹记录，请使用其他名称")
    # Flush current counters before creating the rollback snapshot.
    if eventlog.durable and not eventlog.persist():
        raise OSError("统计保存失败，渠道名称未修改")
    saved_log = {field: copy.deepcopy(getattr(eventlog, field)) for field in LOG_FIELDS}
    saved_rules = mappings.view()["rules"]
    saved_fingerprints = copy.deepcopy(juice.fingerprints) if juice else None
    journal = _prepare(registry.gateway.data_dir)
    try:
        for table in keyed:
            if old in table:
                table[new] = table.pop(old)
        for record in [*eventlog.events, *eventlog._pending.values()]:
            if record.get("station") == old:
                record["station"] = new
        eventlog._reported_usage = {
            ((new, *key[1:]) if key[0] == old else key): value
            for key, value in eventlog._reported_usage.items()
        }
        if eventlog.durable and not eventlog.persist():
            raise OSError("重命名统计保存失败")
        rules = copy.deepcopy(saved_rules)
        for rule in rules:
            for target in rule["targets"]:
                if target["station"] == old:
                    target["station"] = new
        mappings.save(rules, None)
        with visual_store.connect() as db, db:
            row = db.execute("SELECT summary,result FROM results WHERE station=?", (old,)).fetchone()
            if row:
                summary, result = map(json.loads, row)
                summary["station"] = result["station"] = new
                db.execute("UPDATE results SET station=?,summary=?,result=? WHERE station=?",
                           (new, json.dumps(summary), json.dumps(result), old))
            db.execute("UPDATE schedules SET station=? WHERE station=?", (new, old))
        if juice:
            juice.fingerprints = {
                (juice._key(new, fp.model) if fp.station == old else key): fp
                for key, fp in juice.fingerprints.items()
            }
            for fp in juice.fingerprints.values():
                if fp.station == old:
                    fp.station = new
            if juice.persist_path:
                payload = {"threshold": juice.threshold, "fingerprints": {
                    key: {"station": fp.station, "model": fp.model, "counts": fp.counts,
                          "samples": fp.samples, "updated_at": fp.updated_at}
                    for key, fp in juice.fingerprints.items()}}
                path = Path(juice.persist_path)
                temp = path.with_suffix(".tmp")
                temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                temp.replace(path)
        if station_store:
            station_store.save([candidate if cfg.name == old else cfg for cfg in registry.stations])
        (journal / "committed").write_text("1", encoding="ascii")
    except BaseException:
        for field, value in saved_log.items():
            setattr(eventlog, field, value)
        mappings.rules = saved_rules
        if juice:
            juice.fingerprints = saved_fingerprints
        recover_rename(registry.gateway.data_dir)
        eventlog.store.read()  # Restore the optimistic SQLite revision, too.
        eventlog._storage_error = ""
        eventlog._retry_at = 0
        raise
    # The durable commit is complete. Rekey the existing state, preserving
    # clients, discovered models, health samples, OAuth references and cooldowns.
    registry.states = {new if key == old else key: value for key, value in registry.states.items()}
    st.cfg = dataclasses.replace(st.cfg, name=new)
    with registry._activity_lock:
        for record in (registry._last_attempt, registry.last_routed):
            if record and record.get("station") == old:
                record["station"] = new
    for table in (runner.jobs, runner.next_allowed):
        if old in table:
            table[new] = table.pop(old)
    try:
        _clean(journal)
    except OSError:
        pass  # A committed leftover is cleaned on the next startup.
    registry._activity_changed()
    return st
