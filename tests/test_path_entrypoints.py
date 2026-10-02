from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from indeces import console, ingest, knowledge_directory
from indeces.api_key_wizard import configure_api_key
from indeces.config import load_config
from indeces.discord_wizard import configure_discord
from indeces.path_policy import PathPolicyError
from indeces.lock import InstanceLock
from indeces.observer import ObserverServer, prepare_scratch_directory, show_logs
from indeces.scratch import ScratchLog
from indeces.store import Store
from indeces.pdf_import import publish_markdown
from indeces.contracts import GovernedError


class PathEntrypointTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "config.toml"
        self.path.write_bytes((Path(__file__).resolve().parents[1] / "config.example.toml").read_bytes())
        base = load_config(self.path)
        self.config = replace(base, discord=replace(base.discord, guild_id="10"))

    def test_serve_refuses_onedrive_before_lease_logs_store_or_adapter(self):
        for field in ("knowledge_dir", "state_dir", "scratch_dir"):
            with self.subTest(field=field):
                bad = replace(self.config, **{field: self.root / "OneDrive" / field.removesuffix("_dir")})
                with patch.object(console, "InstanceLock") as lease, \
                        patch.object(console, "ScratchLog") as scratch, \
                        patch.object(console, "Store") as store, \
                        patch.object(console, "OpenAIAdapter") as adapter, \
                        self.assertRaises(PathPolicyError):
                    asyncio.run(console.serve(bad, "unused", "unused"))
                for resource in (lease, scratch, store, adapter):
                    resource.assert_not_called()
        self.assertFalse((self.root / "OneDrive").exists())

    def test_rejected_guide_path_does_not_continue_to_ingestion(self):
        with patch.object(console, "InstanceLock"), \
                patch.object(console, "ScratchLog"), \
                patch.object(console, "prepare_knowledge_directory", side_effect=knowledge_directory.KnowledgeDirectoryError("rejected")), \
                patch.object(console, "Store") as store, \
                patch.object(console, "OpenAIAdapter") as adapter, \
                redirect_stdout(io.StringIO()), self.assertRaises(knowledge_directory.KnowledgeDirectoryError):
            asyncio.run(console.serve(self.config, "unused", "unused"))
        store.assert_not_called()
        adapter.assert_not_called()

    def test_headless_and_retry_refuse_before_lease_or_credentials(self):
        bad = replace(self.config, knowledge_dir=self.root / "foreign-literature")
        with patch.object(console, "InstanceLock") as lease, self.assertRaises(PathPolicyError):
            console.run(bad, self.path)
        lease.assert_not_called()
        with patch.object(ingest, "InstanceLock") as lease, \
                patch.object(ingest, "load_openai_key") as key, self.assertRaises(PathPolicyError):
            asyncio.run(ingest.retry_ingestion(bad, self.path))
        lease.assert_not_called()
        key.assert_not_called()

    def test_knowledge_entrypoint_does_not_create_or_open_foreign_directory(self):
        target = self.root / "OneDrive" / "literature"
        bad = replace(self.config, knowledge_dir=target)
        with patch.object(knowledge_directory.os, "startfile", create=True) as opened, \
                self.assertRaises(knowledge_directory.KnowledgeDirectoryError):
            knowledge_directory.show_knowledge_directory(bad)
        opened.assert_not_called()
        self.assertFalse(target.exists())

    def test_init_and_both_wizards_reject_onedrive_before_read_or_prompt(self):
        path = self.root / "OneDrive" / "config.toml"
        with self.assertRaises(PathPolicyError):
            console.initialize(path)
        self.assertFalse(path.parent.exists())
        for wizard in (configure_api_key, configure_discord):
            with self.subTest(wizard=wizard.__name__), \
                    patch.object(Path, "read_bytes", side_effect=AssertionError("must reject before reading")), \
                    patch("builtins.input", side_effect=AssertionError("must reject before prompting")):
                self.assertFalse(wizard(path, output=lambda _: None))

    def test_low_level_managed_entries_refuse_onedrive_before_creation(self):
        for entrypoint in (InstanceLock, ScratchLog, Store, prepare_scratch_directory):
            with self.subTest(entrypoint=entrypoint.__name__), self.assertRaises(PathPolicyError):
                entrypoint(self.root / "OneDrive" / "managed")
        bad = replace(self.config, scratch_dir=self.root / "OneDrive" / "scratch")
        for entrypoint in (ObserverServer, show_logs):
            with self.subTest(entrypoint=entrypoint.__name__), self.assertRaises(PathPolicyError):
                entrypoint(bad)
        self.assertFalse((self.root / "OneDrive").exists())

    def test_pdf_export_refuses_onedrive_before_creating_review_files(self):
        with self.assertRaises(GovernedError) as error:
            publish_markdown(self.root / "OneDrive" / "state", "kb:" + "a" * 32, "synthetic")
        self.assertEqual(error.exception.code, "pdf_export_path_invalid")
        self.assertFalse((self.root / "OneDrive").exists())


if __name__ == "__main__":
    unittest.main()
