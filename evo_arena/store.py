from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(data) -> str:
    return hashlib.sha256(canonical(data).encode()).hexdigest()


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "arena.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                created TEXT NOT NULL, PRIMARY KEY(kind,id));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                kind TEXT NOT NULL, data TEXT NOT NULL, created TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS event_scope ON events(scope,seq);
            CREATE TABLE IF NOT EXISTS seats (
                season TEXT NOT NULL, slot INTEGER NOT NULL, version TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(season,slot));
            CREATE TRIGGER IF NOT EXISTS immutable_version BEFORE UPDATE ON objects
                WHEN OLD.kind='version' BEGIN SELECT RAISE(ABORT,'immutable version'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_evo_version BEFORE UPDATE ON objects
                WHEN OLD.kind='evo_version' BEGIN SELECT RAISE(ABORT,'immutable evo version'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_evo_task_pack BEFORE UPDATE ON objects
                WHEN OLD.kind='evo_task_pack' BEGIN SELECT RAISE(ABORT,'immutable task pack'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_evo_evidence BEFORE UPDATE ON objects
                WHEN OLD.kind IN ('evo_decision','evo_diagnosis','evo_proposal','evo_optimizer_output')
                BEGIN SELECT RAISE(ABORT,'immutable experiment evidence'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_evo_optimizer_output BEFORE UPDATE ON objects
                WHEN OLD.kind='evo_optimizer_output' BEGIN SELECT RAISE(ABORT,'immutable optimizer output'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_evo_checkpoint BEFORE UPDATE ON objects
                WHEN OLD.kind IN ('evo_intent','evo_checkpoint') BEGIN SELECT RAISE(ABORT,'immutable execution evidence'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_evo_delivery BEFORE UPDATE ON objects
                WHEN OLD.kind IN ('evo_import','evo_project_change') BEGIN SELECT RAISE(ABORT,'immutable delivery evidence'); END;
        """)

    def close(self):
        with self.lock:
            self.db.close()

    def create(self, kind: str, ident: str, data: dict) -> dict:
        with self.lock, self.db:
            existing = self.get(kind, ident)
            if existing is not None:
                if canonical(existing) != canonical(data):
                    raise ValueError("Idempotency conflict")
                return existing
            self.db.execute("INSERT INTO objects VALUES(?,?,?,?)", (kind, ident, canonical(data), now()))
        return data

    def update(self, kind: str, ident: str, data: dict) -> None:
        with self.lock, self.db:
            result = self.db.execute("UPDATE objects SET data=? WHERE kind=? AND id=?",
                                     (canonical(data), kind, ident))
            if not result.rowcount:
                raise KeyError(ident)

    def get(self, kind: str, ident: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT data FROM objects WHERE kind=? AND id=?", (kind, ident)).fetchone()
            return json.loads(row[0]) if row else None

    def list(self, kind: str) -> list[dict]:
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute(
                "SELECT data FROM objects WHERE kind=? ORDER BY created DESC,rowid DESC", (kind,))]

    def event(self, scope: str, kind: str, data: dict) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT INTO events(scope,kind,data,created) VALUES(?,?,?,?)",
                            (scope, kind, canonical(data), now()))

    def events(self, scope: str | None = None, after: int = 0, limit: int = 500) -> list[dict]:
        with self.lock:
            query = "SELECT * FROM events WHERE seq>?"
            args: list = [after]
            if scope:
                query += " AND scope=?"
                args.append(scope)
            args.append(max(1, min(limit, 1000)))
            rows = self.db.execute(query + " ORDER BY seq LIMIT ?", args)
            return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def version(self, name: str, policy: str, parent: str | None = None, hypothesis: str = "") -> dict:
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("Name must contain 1 to 80 characters")
        if not isinstance(policy, str) or not 10 <= len(policy.strip()) <= 8000:
            raise ValueError("Policy must contain 10 to 8000 characters")
        content = {"name": name.strip(), "policy": policy.strip(), "parent": parent,
                   "hypothesis": hypothesis[:2000], "protocol": "fixops-actions-v1"}
        if parent and not self.get("version", parent):
            raise ValueError("Unknown parent")
        ident = "v-" + digest(content)[:16]
        return self.create("version", ident, {"id": ident, **content, "content_sha256": digest(content)})

    def initialize_seats(self, season: str, versions: list[str]) -> None:
        with self.lock, self.db:
            for slot, version in enumerate(versions):
                self.db.execute("INSERT INTO seats(season,slot,version) VALUES(?,?,?)", (season, slot, version))

    def seats(self, season: str) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM seats WHERE season=? ORDER BY slot", (season,))]

    def promote(self, season: str, slot: int, expected: str, revision: int,
                child: str, decision: dict) -> bool:
        with self.lock, self.db:
            if self.get("decision", decision["id"]):
                return False
            if not self.get("version", child):
                raise ValueError("Unknown child")
            cursor = self.db.execute("UPDATE seats SET version=?,revision=revision+1 "
                "WHERE season=? AND slot=? AND version=? AND revision=?",
                (child, season, slot, expected, revision))
            if cursor.rowcount != 1:
                raise ValueError("Seat changed; stale promotion refused")
            self.db.execute("INSERT INTO objects VALUES('decision',?,?,?)",
                            (decision["id"], canonical(decision), now()))
            self.db.execute("INSERT INTO events(scope,kind,data,created) VALUES(?,?,?,?)",
                            (season, "child.promoted", canonical(decision), now()))
        return True
