from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .path_policy import validate_managed_path


class Store:
    def __init__(self, directory: Path):
        directory = validate_managed_path(directory, "state_dir")
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("memory.sqlite3", "memory.sqlite3-wal", "memory.sqlite3-shm", "memory.sqlite3-journal"):
            validate_managed_path(directory / name, "knowledge database")
        self.db = sqlite3.connect(directory / "memory.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS turns (
                message_id TEXT PRIMARY KEY, scope TEXT NOT NULL, author_id TEXT NOT NULL,
                author_name TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                status TEXT NOT NULL, reply TEXT, delivered TEXT, receipt TEXT);
            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                turn_id TEXT NOT NULL, role TEXT NOT NULL, author_id TEXT NOT NULL,
                content TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS messages_scope_seq ON messages(scope,seq);
            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                through_seq INTEGER NOT NULL, summary TEXT NOT NULL,
                previous_id INTEGER, trace_id TEXT NOT NULL, source_ids TEXT NOT NULL);
        """)

    def begin(self, message) -> bool:
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO turns(message_id,scope,author_id,author_name,content,created_at,status) VALUES(?,?,?,?,?,?,?)",
                (message.message_id, message.scope, message.author_id, message.author_name, message.text, message.created_at, "processing"))
            if not cursor.rowcount:
                return False
            self.db.execute("INSERT INTO messages(scope,turn_id,role,author_id,content,created_at) VALUES(?,?,?,?,?,?)",
                (message.scope, message.message_id, "user", message.author_id, message.text, message.created_at))
        return True

    def checkpoint(self, scope):
        row = self.db.execute("SELECT * FROM checkpoints WHERE scope=? ORDER BY id DESC LIMIT 1", (scope,)).fetchone()
        return dict(row) if row else {"id": None, "through_seq": 0, "summary": ""}

    def history(self, scope, through_seq=0, exclude_turn=None):
        rows = self.db.execute("SELECT * FROM messages WHERE scope=? AND seq>? ORDER BY seq", (scope, through_seq)).fetchall()
        return [dict(r) for r in rows if r["turn_id"] != exclude_turn]

    def save_checkpoint(self, scope, rows, summary, trace_id):
        previous = self.checkpoint(scope)
        if not rows or rows[0]["seq"] <= previous["through_seq"]:
            raise ValueError("checkpoint must advance contiguous history")
        expected = self.db.execute("SELECT seq FROM messages WHERE scope=? AND seq>? ORDER BY seq LIMIT ?", (scope, previous["through_seq"], len(rows))).fetchall()
        if [r["seq"] for r in rows] != [r[0] for r in expected] or any(r["scope"] != scope for r in rows):
            raise ValueError("checkpoint source coverage is not the contiguous scope prefix")
        with self.db:
            self.db.execute("INSERT INTO checkpoints(scope,through_seq,summary,previous_id,trace_id,source_ids) VALUES(?,?,?,?,?,?)",
                (scope, rows[-1]["seq"], summary, previous["id"], trace_id, json.dumps([r["seq"] for r in rows])))
        return self.checkpoint(scope)

    def generated(self, message_id, reply):
        with self.db:
            self.db.execute("UPDATE turns SET status='generated',reply=? WHERE message_id=?", (reply, message_id))

    def finish(self, message, receipt):
        with self.db:
            self.db.execute("UPDATE turns SET status='delivered',delivered=?,receipt=? WHERE message_id=?",
                (receipt.text, json.dumps(receipt.message_ids), message.message_id))
            self.db.execute("INSERT INTO messages(scope,turn_id,role,author_id,content,created_at) VALUES(?,?,?,?,?,?)",
                (message.scope, message.message_id, "assistant", "bot", receipt.text, message.created_at))

    def fail(self, message_id, code):
        with self.db:
            self.db.execute("UPDATE turns SET status=? WHERE message_id=?", (code, message_id))

    def recover(self):
        rows = self.db.execute("SELECT message_id,status FROM turns WHERE status IN ('processing','generated')").fetchall()
        with self.db:
            self.db.execute("UPDATE turns SET status='interrupted_no_replay' WHERE status IN ('processing','generated')")
        return [dict(r) for r in rows]

    def close(self):
        self.db.close()
