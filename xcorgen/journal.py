"""Durable bounded-run plans and delivery receipts. No credentials are stored here.

An interrupted in-flight POST cannot be distinguished from an accepted POST whose
acknowledgement was lost. Such deliveries block resume instead of risking duplicates.
"""
import hashlib
import json
import os
import sqlite3
from .security import SecurityError


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def tuples(value):
    return tuple(tuples(x) for x in value) if isinstance(value, list) else value


class RunJournal:
    def __init__(self, directory):
        os.makedirs(directory, mode=0o700, exist_ok=True)
        self.path = os.path.join(directory, "run.sqlite3")
        if os.path.islink(self.path):
            raise SecurityError("Run journal must not be a symbolic link.")
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        os.close(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise SecurityError("Run journal must be owner-only.")
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sessions (idx INTEGER PRIMARY KEY, record TEXT NOT NULL, delivered INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS deliveries (id TEXT PRIMARY KEY, session_idx INTEGER NOT NULL,
            hash TEXT NOT NULL, status TEXT NOT NULL, receipt TEXT);
        """)

    def get(self, key):
        row = self.db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value)))

    def plan_session(self, index, record, deliveries):
        row = self.db.execute("SELECT record FROM sessions WHERE idx=?", (index,)).fetchone()
        if row and digest(json.loads(row[0])) != digest(record):
            raise ValueError("Regenerated session differs from the frozen plan; resume refused.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO sessions (idx,record) VALUES (?,?)", (index, json.dumps(record)))
            for ordinal, delivery in enumerate(deliveries):
                ident = f"{index}:{ordinal}"
                fingerprint = digest(delivery)
                existing = self.db.execute("SELECT hash FROM deliveries WHERE id=?", (ident,)).fetchone()
                if existing and existing[0] != fingerprint:
                    raise ValueError("Regenerated payload differs from the frozen plan; resume refused.")
                self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?,'ready',NULL)", (ident, index, fingerprint))
                delivery["_delivery_id"] = ident

    def assert_resumable(self):
        count = self.db.execute("SELECT count(*) FROM deliveries WHERE status IN ('inflight','uncertain','partial')").fetchone()[0]
        if count:
            raise ValueError(f"Resume blocked: {count} deliveries have uncertain or partial acceptance. Verify them before retrying; no payloads were resent.")

    def begin(self, ident):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT status FROM deliveries WHERE id=?", (ident,)).fetchone()
            if row[0] in ("accepted", "expired"):
                return False
            if row[0] not in ("ready", "rejected"):
                raise ValueError("Delivery acceptance is uncertain; automatic resend is prohibited.")
            self.db.execute("UPDATE deliveries SET status='inflight' WHERE id=?", (ident,))
        return True

    def receipt(self, ident, receipt):
        with self.db:
            self.db.execute("UPDATE deliveries SET status=?, receipt=? WHERE id=?",
                            (receipt["status"], json.dumps(receipt), ident))

    def completed(self):
        rows = self.db.execute("""SELECT idx,record FROM sessions s WHERE delivered=0 AND NOT EXISTS
          (SELECT 1 FROM deliveries d WHERE d.session_idx=s.idx AND status NOT IN ('accepted','expired'))""").fetchall()
        return [(i, json.loads(record)) for i, record in rows]

    def acknowledge_session(self, index):
        with self.db:
            self.db.execute("UPDATE sessions SET delivered=1 WHERE idx=?", (index,))

    def close(self):
        self.db.close()
