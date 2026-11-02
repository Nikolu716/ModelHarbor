"""Embedded statistics storage. No daemon, driver package, or shared connection."""
from __future__ import annotations

import json
import sqlite3
import time
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path


class StaleStatsWriter(RuntimeError):
    """Another process advanced this account's statistics. Never overwrite it."""


class StatsStore:
    VERSION = 1

    def __init__(self, directory):
        self.path = Path(directory) / 'stats.sqlite3'
        self.revision = 0

    @contextmanager
    def connect(self, *, create=False):
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        # mode=rw avoids silently recreating a missing live database.
        uri = self.path.resolve().as_uri() + ('?mode=rwc' if create else '?mode=rw')
        db = sqlite3.connect(uri, uri=True, timeout=0.25)
        try:
            # One writer in a local gateway: rollback journal keeps deployment simple.
            # FULL commits are durable without a 30-second memory-only window.
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    def read(self):
        if not self.path.exists():
            return None
        with self.connect() as db:
            db.execute('BEGIN')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Statistics database integrity check failed')
            if db.execute('PRAGMA user_version').fetchone()[0] != self.VERSION:
                raise ValueError('Unsupported statistics database version')
            meta = dict(db.execute('SELECT key,value FROM metadata'))
            self.revision = int(meta['revision'])
            result = json.loads(meta['summary'])
            result['events'] = [json.loads(row[0]) for row in db.execute('SELECT payload FROM events ORDER BY sequence')]
            for date, payload in db.execute('SELECT day,payload FROM days'):
                for key, values in json.loads(payload).items():
                    result.setdefault(key, {})[date] = values
            result['runtime'] = {name: json.loads(payload) for name, payload in db.execute('SELECT name,payload FROM runtime')}
            result['pending'] = {key: json.loads(payload) for key, payload in db.execute('SELECT id,payload FROM pending')}
            result['saved_at'] = float(meta.get('saved_at', 0))
            return result

    def initialise(self, summary, days, events, maxlen):
        # Create the schema and import history in one atomic transaction.
        with self.connect(create=True) as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('PRAGMA user_version').fetchone()[0]:
                raise StaleStatsWriter('Statistics already initialised by another process')
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE days (day TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE runtime (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE pending (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('PRAGMA user_version=1')
            db.executemany('INSERT INTO metadata VALUES (?,?)', [('revision','0'),('summary',self.encode(summary)),('saved_at',str(time.time()))])
            db.executemany('INSERT INTO days VALUES (?,?)', [(k,self.encode(v)) for k,v in days.items()])
            db.executemany('INSERT INTO events(id,payload) VALUES (?,?)', [(e['event_id'],self.encode(e)) for e in events[-maxlen:]])

    @staticmethod
    def encode(value):
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)

    def write(self, *, events=(), days=None, summary=None, maxlen=500, replace=False, runtime=None, pending=None, remove_pending=()):
        now = time.time()
        with self.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            actual = int(db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0])
            if actual != self.revision:
                raise StaleStatsWriter('Another gateway process changed the statistics')
            if replace:
                db.execute('DELETE FROM events')
                db.execute('DELETE FROM days')
                db.execute('DELETE FROM runtime')
                db.execute('DELETE FROM pending')
            db.executemany('INSERT INTO events(id,payload) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                           [(e['event_id'],self.encode(e)) for e in events])
            db.execute('DELETE FROM events WHERE sequence IN (SELECT sequence FROM events ORDER BY sequence DESC LIMIT -1 OFFSET ?)', (maxlen,))
            if days:
                db.executemany('INSERT INTO days VALUES (?,?) ON CONFLICT(day) DO UPDATE SET payload=excluded.payload',
                               [(k,self.encode(v)) for k,v in days.items()])
            if summary is not None:
                db.execute("UPDATE metadata SET value=? WHERE key='summary'", (self.encode(summary),))
            if runtime:
                db.executemany('INSERT INTO runtime VALUES (?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
                               [(k,self.encode(v)) for k,v in runtime.items()])
            if pending:
                db.executemany('INSERT INTO pending VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                               [(k,self.encode(v)) for k,v in pending.items()])
            db.executemany('DELETE FROM pending WHERE id=?', [(k,) for k in remove_pending])
            db.execute("UPDATE metadata SET value=? WHERE key='revision'", (str(actual+1),))
            db.execute("UPDATE metadata SET value=? WHERE key='saved_at'", (str(now),))
        self.revision += 1
        return now

    def backup(self, destination):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.stats-backup-', dir=destination.parent)
        os.close(fd)
        try:
            with self.connect() as source:
                target = sqlite3.connect(temporary)
                try:
                    source.backup(target)
                finally:
                    target.close()
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
