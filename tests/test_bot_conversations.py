from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from indeces.bot_conversations import BotConversationGate, ROUND_LIMIT


def message(identity, *, bot=True, channel="20", guild="10", author="30"):
    return SimpleNamespace(message_id=str(identity), scope=f"{guild}:{channel}",
                           author_id=author, author_is_bot=bot)


class BotConversationGateTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.gate = BotConversationGate(self.db)

    def tearDown(self):
        self.db.close()

    def accept(self, incoming):
        admission = self.gate.prepare(incoming)
        self.assertTrue(admission.allowed)
        self.gate.commit(incoming, admission)
        return admission

    def test_five_admitted_attempts_then_stop_without_ttl_reset(self):
        for index in range(1, ROUND_LIMIT + 1):
            admission = self.accept(message(index))
            self.assertEqual(admission.round, index)
            self.assertIsNone(admission.epoch)
        rejected = self.gate.prepare(message(6))
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.reason, "round_limit")
        self.assertIsNone(rejected.round)
        self.assertFalse(self.gate.prepare(message("much-later")).allowed)

    def test_prepare_alone_does_not_consume_round_or_reset_peers(self):
        bot = message(1)
        for _ in range(10):
            self.assertEqual(self.gate.prepare(bot).round, 1)
        self.accept(bot)
        self.gate.prepare(message("human", bot=False))
        self.assertEqual(self.gate.prepare(message(2)).round, 2)
        self.assertIsNone(self.gate.prepare(message(2)).epoch)

    def test_synchronous_burst_can_reserve_only_five(self):
        accepted = []
        for index in range(20):
            incoming = message(index)
            admission = self.gate.prepare(incoming)
            if admission.allowed:
                self.gate.commit(incoming, admission)
                accepted.append(admission.round)
        self.assertEqual(accepted, [1, 2, 3, 4, 5])

    def test_duplicate_bot_does_not_consume_another_round(self):
        self.accept(message(1))
        rejected = self.gate.prepare(message(1))
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.reason, "duplicate")
        self.assertEqual(self.gate.prepare(message(2)).round, 2)

    def test_existing_runtime_turn_is_duplicate(self):
        self.db.execute("CREATE TABLE turns(message_id TEXT PRIMARY KEY,status TEXT)")
        self.db.execute("INSERT INTO turns VALUES('old','delivered')")
        self.db.commit()
        rejected = self.gate.prepare(message("old"))
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.reason, "duplicate")
        self.assertEqual(self.gate.prepare(message("new")).round, 1)

    def test_human_resets_all_peers_only_in_its_scope(self):
        self.accept(message("peer-30"))
        self.accept(message("peer-40", author="40"))
        self.accept(message("other-channel", channel="21"))
        self.accept(message("other-guild", guild="11"))
        human = self.accept(message("human-1", bot=False, author="50"))
        self.assertIsNone(human.round)
        self.assertEqual(human.epoch, "human-1")
        for author in ("30", "40", "new-peer"):
            next_turn = self.gate.prepare(message("new-" + author, author=author))
            self.assertEqual(next_turn.round, 1)
            self.assertEqual(next_turn.epoch, "human-1")
        self.assertEqual(self.gate.prepare(message("next-channel", channel="21")).round, 2)
        self.assertEqual(self.gate.prepare(message("next-guild", guild="11")).round, 2)

    def test_old_human_replay_cannot_reset_a_new_epoch(self):
        self.accept(message("human-1", bot=False))
        self.accept(message("human-2", bot=False))
        self.accept(message("bot"))
        rejected = self.gate.prepare(message("human-1", bot=False))
        self.assertFalse(rejected.allowed)
        self.assertEqual(rejected.reason, "duplicate")
        next_turn = self.gate.prepare(message("next-bot"))
        self.assertEqual(next_turn.round, 2)
        self.assertEqual(next_turn.epoch, "human-2")

    def test_peer_and_channel_limits_are_independent(self):
        for index in range(5):
            self.accept(message(index))
        self.assertEqual(self.gate.prepare(message("peer", author="40")).round, 1)
        self.assertEqual(self.gate.prepare(message("channel", channel="21")).round, 1)

    def test_stale_admissions_roll_back_without_modifying_counters(self):
        earlier = message("earlier")
        prepared = self.gate.prepare(earlier)
        self.accept(message("other"))
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.gate.commit(earlier, prepared)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.gate.prepare(earlier).round, 2)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bot_conversation_accepted").fetchone()[0], 1)

    def test_human_reset_invalidates_prepared_bot_and_human_inputs(self):
        bot = message("bot")
        bot_admission = self.gate.prepare(bot)
        human = message("later-human", bot=False)
        human_admission = self.gate.prepare(human)
        self.accept(message("human", bot=False))
        for incoming, admission in ((bot, bot_admission), (human, human_admission)):
            with self.assertRaisesRegex(RuntimeError, "stale"):
                self.gate.commit(incoming, admission)
        self.assertEqual(self.gate.prepare(bot).epoch, "human")

    def test_cannot_commit_rejected_or_a_different_message(self):
        original = message("original")
        admission = self.gate.prepare(original)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.gate.commit(message("different"), admission)
        self.gate.commit(original, admission)
        with self.assertRaises(ValueError):
            self.gate.commit(original, self.gate.prepare(original))

    def test_existing_open_transaction_is_preserved_and_rejected(self):
        self.db.execute("INSERT INTO bot_conversation_scopes VALUES('other',NULL,0)")
        with self.assertRaisesRegex(RuntimeError, "committed"):
            self.gate.commit(message(1), self.gate.prepare(message(1)))
        self.assertTrue(self.db.in_transaction)
        with self.assertRaisesRegex(RuntimeError, "committed"):
            BotConversationGate(self.db)
        self.db.rollback()

    def test_failed_commit_does_not_leave_partial_reservation(self):
        self.db.execute("""CREATE TRIGGER synthetic_admission_failure
            BEFORE INSERT ON bot_conversation_accepted
            BEGIN SELECT RAISE(ABORT, 'synthetic admission failure'); END""")
        incoming = message("blocked")
        admission = self.gate.prepare(incoming)
        with self.assertRaises(sqlite3.IntegrityError):
            self.gate.commit(incoming, admission)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.gate.prepare(incoming).round, 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bot_conversation_scopes").fetchone()[0], 0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM bot_conversation_peers").fetchone()[0], 0)

    def test_memory_database_has_no_backup(self):
        self.assertIsNone(self.gate.backup_path)
        self.assertIsNone(BotConversationGate(self.db).backup_path)


class BotConversationMigrationTests(unittest.TestCase):
    def test_existing_database_is_backed_up_before_additive_migration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            db = sqlite3.connect(path)
            try:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE evidence(id TEXT PRIMARY KEY,value TEXT)")
                db.execute("INSERT INTO evidence VALUES('original','preserved')")
                db.commit()
                gate = BotConversationGate(db)
                self.assertIsNotNone(gate.backup_path)
                self.assertEqual(gate.backup_path.parent, path.parent)
                with closing(sqlite3.connect(gate.backup_path)) as backup:
                    self.assertEqual(backup.execute("SELECT * FROM evidence").fetchall(), [("original", "preserved")])
                    self.assertIsNone(backup.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='bot_conversation_scopes'"
                    ).fetchone())
                self.assertEqual(db.execute("SELECT * FROM evidence").fetchall(), [("original", "preserved")])
                BotConversationGate(db)
                self.assertEqual(len(list(path.parent.glob("*.before-bot-conversations-*.sqlite3"))), 1)
            finally:
                db.close()

    def test_fresh_disk_database_needs_no_backup(self):
        with TemporaryDirectory() as directory:
            with closing(sqlite3.connect(Path(directory) / "fresh.sqlite3")) as db:
                self.assertIsNone(BotConversationGate(db).backup_path)

    def test_limit_and_duplicate_human_survive_reopen(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            db = sqlite3.connect(path)
            gate = BotConversationGate(db)
            human = message("human", bot=False)
            gate.commit(human, gate.prepare(human))
            for index in range(5):
                incoming = message(index)
                gate.commit(incoming, gate.prepare(incoming))
            db.close()
            db = sqlite3.connect(path)
            try:
                reopened = BotConversationGate(db)
                self.assertEqual(reopened.prepare(message("next")).reason, "round_limit")
                self.assertEqual(reopened.prepare(human).reason, "duplicate")
                new_human = message("new-human", bot=False)
                reopened.commit(new_human, reopened.prepare(new_human))
                next_turn = reopened.prepare(message("next"))
                self.assertEqual(next_turn.round, 1)
                self.assertEqual(next_turn.epoch, "new-human")
                self.assertEqual(reopened.prepare(message(0)).reason, "duplicate")
            finally:
                db.close()

    def test_two_connections_cannot_commit_the_same_prepared_slot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "memory.sqlite3"
            with closing(sqlite3.connect(path)) as first, closing(sqlite3.connect(path)) as second:
                first_gate = BotConversationGate(first)
                second_gate = BotConversationGate(second)
                first_message, second_message = message("first"), message("second")
                first_admission = first_gate.prepare(first_message)
                second_admission = second_gate.prepare(second_message)
                first_gate.commit(first_message, first_admission)
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    second_gate.commit(second_message, second_admission)
                self.assertEqual(second_gate.prepare(second_message).round, 2)


if __name__ == "__main__":
    unittest.main()
