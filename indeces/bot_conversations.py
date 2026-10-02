"""Durable intake limits for explicitly mentioned Discord bot conversations.

An accepted bot input consumes one attempt, including skipped/failed replies.
Only a new accepted human input resets the peers in its channel scope. Reading
the gate never changes a counter; the bridge commits only after auditing intake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import uuid


ROUND_LIMIT = 5
_TABLES = frozenset({"bot_conversation_scopes", "bot_conversation_peers", "bot_conversation_accepted"})


@dataclass(frozen=True)
class BotAdmission:
    allowed: bool
    reason: str | None
    round: int | None
    epoch: str | None
    _identity: tuple[str, str, str, bool] = field(repr=False)
    _revision: int = field(repr=False)


class BotConversationGate:
    """Use indexed SQLite state without an in-memory peer or replay cache."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.backup_path: Path | None = None
        if db.in_transaction:
            raise RuntimeError("bot conversation migration requires a committed database")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not _TABLES.issubset(tables) and tables:
            self.backup_path = self._backup_existing_database()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.execute("""
                CREATE TABLE IF NOT EXISTS bot_conversation_scopes (
                    scope TEXT PRIMARY KEY, epoch TEXT, revision INTEGER NOT NULL)
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS bot_conversation_peers (
                    scope TEXT NOT NULL, author_id TEXT NOT NULL,
                    rounds INTEGER NOT NULL CHECK(rounds BETWEEN 0 AND 5),
                    PRIMARY KEY(scope, author_id))
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS bot_conversation_accepted (
                    message_id TEXT PRIMARY KEY, scope TEXT NOT NULL,
                    author_id TEXT NOT NULL, author_is_bot INTEGER NOT NULL,
                    round INTEGER, epoch TEXT)
            """)
            db.commit()
        except BaseException:
            db.rollback()
            raise

    def _backup_existing_database(self) -> Path | None:
        filename = next(row[2] for row in self.db.execute("PRAGMA database_list") if row[1] == "main")
        if not filename:
            return None
        source = Path(filename)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        destination = source.with_name(
            f"{source.stem}.before-bot-conversations-{timestamp}-{uuid.uuid4().hex}.sqlite3"
        )
        # Exclusive creation means an existing result is never overwritten.
        with destination.open("xb"):
            pass
        backup = sqlite3.connect(destination)
        try:
            self.db.backup(backup)
        finally:
            backup.close()
        return destination

    def _duplicate(self, message_id: str) -> bool:
        if self.db.execute(
            "SELECT 1 FROM bot_conversation_accepted WHERE message_id=?", (message_id,)
        ).fetchone():
            return True
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='turns'").fetchone():
            return self.db.execute("SELECT 1 FROM turns WHERE message_id=?", (message_id,)).fetchone() is not None
        return False

    def prepare(self, message) -> BotAdmission:
        """Inspect an input without reserving or resetting any conversation."""
        identity = (message.message_id, message.scope, message.author_id, bool(message.author_is_bot))
        row = self.db.execute(
            "SELECT epoch,revision FROM bot_conversation_scopes WHERE scope=?", (message.scope,)
        ).fetchone()
        epoch, revision = (row[0], row[1]) if row is not None else (None, 0)
        if self._duplicate(message.message_id):
            return BotAdmission(False, "duplicate", None, epoch, identity, revision)
        if not message.author_is_bot:
            return BotAdmission(True, None, None, message.message_id, identity, revision)
        peer = self.db.execute(
            "SELECT rounds FROM bot_conversation_peers WHERE scope=? AND author_id=?",
            (message.scope, message.author_id),
        ).fetchone()
        rounds = peer[0] if peer is not None else 0
        if rounds >= ROUND_LIMIT:
            return BotAdmission(False, "round_limit", None, epoch, identity, revision)
        return BotAdmission(True, None, rounds + 1, epoch, identity, revision)

    def commit(self, message, admission: BotAdmission) -> None:
        """Atomically reserve the inspected input; reject any stale inspection.

        The bridge must call this synchronously after its queued-input audit,
        with no await between preparation and committing the reservation.
        """
        if not admission.allowed:
            raise ValueError("cannot commit a rejected bot conversation admission")
        if self.db.in_transaction:
            raise RuntimeError("bot conversation admission requires a committed database")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.prepare(message) != admission:
                raise RuntimeError("stale bot conversation admission")
            self.db.execute("""
                INSERT INTO bot_conversation_scopes(scope,epoch,revision) VALUES(?,?,?)
                ON CONFLICT(scope) DO UPDATE SET epoch=excluded.epoch,revision=excluded.revision
            """, (message.scope, admission.epoch, admission._revision + 1))
            if message.author_is_bot:
                self.db.execute("""
                    INSERT INTO bot_conversation_peers(scope,author_id,rounds) VALUES(?,?,?)
                    ON CONFLICT(scope,author_id) DO UPDATE SET rounds=excluded.rounds
                """, (message.scope, message.author_id, admission.round))
            else:
                self.db.execute("UPDATE bot_conversation_peers SET rounds=0 WHERE scope=?", (message.scope,))
            self.db.execute("""
                INSERT INTO bot_conversation_accepted(message_id,scope,author_id,author_is_bot,round,epoch)
                VALUES(?,?,?,?,?,?)
            """, (message.message_id, message.scope, message.author_id, int(message.author_is_bot),
                  admission.round, admission.epoch))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
