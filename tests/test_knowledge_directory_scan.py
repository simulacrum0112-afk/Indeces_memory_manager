from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
import stat
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.config import AdapterConfig, Budget, DiscordConfig, KnowledgeConfig
from indeces.contracts import GovernedError
from indeces.knowledge import KnowledgeService
from indeces.memory import MemoryGraph
from indeces.store import Store
from tests.test_knowledge import LabelAdapter, Scratch


class DirectoryFixture:
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir()
        self.originals = {}
        self.config = SimpleNamespace(name="Indeces", knowledge_dir=self.knowledge,
            state_dir=self.root / "state", discord=DiscordConfig("10"),
            knowledge=KnowledgeConfig(max_files=256),
            adapter=AdapterConfig("gpt-6-luna", "https://api.openai.com/v1", {"label": Budget(4096, 512, 15)}))
        self.scanner = object.__new__(KnowledgeService)
        self.scanner.root, self.scanner.config = self.knowledge, self.config

    def file(self, name, raw=b"alpha topic synthetic source"):
        path = self.knowledge / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        self.originals[path] = raw
        return path

    def relative_files(self):
        return [path.relative_to(self.knowledge).as_posix() for path in self.scanner._scan_files()]

    def assert_originals_unchanged(self):
        for path, raw in self.originals.items():
            self.assertEqual(path.read_bytes(), raw)


class DirectoryScanTests(DirectoryFixture, unittest.TestCase):
    def test_normal_subject_year_hierarchy_counts_pdf_and_text_recursively(self):
        names = ["chemistry/2026/paper.PDF", "chemistry/2026/notes.MD",
                 "physics/2025/paper.pdf", "physics/2025/data.txt", "topics/notes.markdown"]
        for name in names:
            self.file(name)
        for name in ("datasets/original.csv", "figures/plot.png", "notes/source.docx", "raw/archive.zip"):
            self.file(name, b"unindexed synthetic raw material")
        self.assertEqual(self.relative_files(), sorted(names))
        self.assert_originals_unchanged()

    def test_root_management_guide_and_staging_do_not_use_any_candidate_slots(self):
        expected = []
        for number in range(256):
            name = f"papers/{number:03}.pdf"
            self.file(name, b"%PDF-synthetic-not-parsed-in-directory-test")
            expected.append(name)
        self.file(".indeces/README.md", b"fixed management guide is not knowledge")
        self.file(".indeces/index.html", b"fixed management entry")
        for number in range(300):
            self.file(f"_staging/raw/{number:03}.pdf", b"unindexed synthetic temporary material")
        candidates = self.scanner._scan_files()
        self.assertEqual([p.relative_to(self.knowledge).as_posix() for p in candidates], expected)
        self.scanner._check_file_count(candidates)
        self.assert_originals_unchanged()

    def test_reserved_root_names_are_case_insensitive(self):
        self.file(".InDeCeS/README.md")
        self.file("_StAgInG/inbox/paper.pdf")
        self.file("papers/ready.pdf")
        self.assertEqual(self.relative_files(), ["papers/ready.pdf"])

    def test_same_reserved_names_nested_in_normal_topics_are_scanned(self):
        names = ["topic/.indeces/note.md", "topic/_staging/paper.pdf",
                 "year/2026/.INDECES/source.txt", "year/2026/_STAGING/source.markdown"]
        for name in names:
            self.file(name)
        self.assertEqual(self.relative_files(), sorted(names))

    def test_names_merely_starting_with_reserved_names_remain_ordinary_topics(self):
        names = [".indeces-notes/paper.pdf", "_staging-reviewed/notes.md"]
        for name in names:
            self.file(name)
        self.assertEqual(self.relative_files(), sorted(names))

    def test_reserved_roots_are_not_traversed_or_opened(self):
        reserved = [self.knowledge / ".indeces", self.knowledge / "_staging"]
        self.file(".indeces/nested/README.md")
        self.file("_staging/nested/paper.pdf")
        self.file("topic/paper.pdf")
        real_scandir = os.scandir

        def guarded_scandir(path):
            candidate = Path(path)
            if any(candidate == base or candidate.is_relative_to(base) for base in reserved):
                raise AssertionError("reserved storage must not be traversed")
            return real_scandir(path)

        with patch.object(os, "scandir", side_effect=guarded_scandir), \
                patch.object(Path, "open", side_effect=AssertionError("scan must not open raw contents")):
            self.assertEqual(self.relative_files(), ["topic/paper.pdf"])

    def test_directory_marked_as_junction_is_pruned_before_traversal(self):
        excluded = self.knowledge / "linked-materials"
        self.file("linked-materials/foreign/paper.pdf")
        self.file("legitimate/paper.pdf")
        real_junction = Path.is_junction
        real_scandir = os.scandir

        def is_junction(path):
            return path == excluded or real_junction(path)

        def guarded_scandir(path):
            candidate = Path(path)
            if candidate == excluded or candidate.is_relative_to(excluded):
                raise AssertionError("junction must not be traversed")
            return real_scandir(path)

        with patch.object(Path, "is_junction", is_junction), \
                patch.object(os, "scandir", side_effect=guarded_scandir):
            self.assertEqual(self.relative_files(), ["legitimate/paper.pdf"])

    def test_external_directory_symlink_is_not_traversed(self):
        outside = self.root / "external-synthetic-materials"
        outside.mkdir()
        original = outside / "foreign.pdf"
        original.write_bytes(b"synthetic outside raw material")
        link = self.knowledge / "external-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink unavailable on this host")
        self.file("legitimate.pdf")
        real_scandir = os.scandir

        def guarded_scandir(path):
            candidate = Path(path)
            if candidate == link or candidate.is_relative_to(link) or candidate.resolve().is_relative_to(outside):
                raise AssertionError("outside symlink target must not be traversed")
            return real_scandir(path)

        with patch.object(os, "scandir", side_effect=guarded_scandir):
            self.assertEqual(self.relative_files(), ["legitimate.pdf"])
        self.assertEqual(original.read_bytes(), b"synthetic outside raw material")

    @unittest.skipUnless(os.name == "nt", "Windows junction behavior")
    def test_real_windows_external_junction_is_not_traversed(self):
        outside = self.root / "external-junction-fixture"
        outside.mkdir()
        original = outside / "foreign.pdf"
        original.write_bytes(b"synthetic external junction material")
        link = self.knowledge / "junction-link"
        self.assertTrue(outside.resolve().is_relative_to(self.root))
        self.assertTrue(link.parent.resolve().is_relative_to(self.root))
        created = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=0x08000000, timeout=10)
        if created.returncode != 0:
            self.skipTest("junction creation unavailable on this host")
        self.assertTrue(link.is_junction())
        self.file("legitimate.pdf")
        real_scandir = os.scandir

        def guarded_scandir(path):
            candidate = Path(path)
            if candidate == link or candidate.is_relative_to(link) or candidate.resolve().is_relative_to(outside):
                raise AssertionError("outside junction target must not be traversed")
            return real_scandir(path)

        try:
            with patch.object(os, "scandir", side_effect=guarded_scandir):
                self.assertEqual(self.relative_files(), ["legitimate.pdf"])
        finally:
            # Remove only the owned junction entry before TemporaryDirectory
            # recursively cleans its ordinary children. The target is also
            # within this same synthetic temporary root, never user storage.
            if link.is_junction():
                link.rmdir()
        self.assertEqual(original.read_bytes(), b"synthetic external junction material")

    def test_supported_candidates_are_bounded_at_one_past_256(self):
        for number in range(300):
            self.file(f"papers/{number:03}.txt")
        candidates = self.scanner._scan_files()
        self.assertEqual(len(candidates), 257)
        self.assertEqual(len(set(candidates)), 257)
        self.assert_originals_unchanged()

    def test_file_symlinks_reparse_points_and_nonregular_entries_do_not_use_candidate_slots(self):
        self.config.knowledge = replace(self.config.knowledge, max_files=1)
        self.file("ordinary.txt")
        alias = self.file("file-alias.pdf")
        real_lstat = Path.lstat
        for mode, attributes in ((stat.S_IFLNK | 0o777, 0),
                                 (stat.S_IFREG | 0o600, 0x400),
                                 (stat.S_IFIFO | 0o600, 0)):
            with self.subTest(mode=mode, attributes=attributes):
                def synthetic_lstat(path):
                    if path == alias:
                        return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
                    return real_lstat(path)

                with patch.object(Path, "lstat", synthetic_lstat):
                    candidates = self.scanner._scan_files()
                    self.assertEqual(candidates, [self.knowledge / "ordinary.txt"])
                    self.scanner._check_file_count(candidates)
        self.assert_originals_unchanged()

    def test_subdirectory_permission_error_aborts_census_instead_of_returning_partial_sources(self):
        self.file("a-readable/source.md")
        self.file("z-inaccessible/source.pdf")
        blocked = self.knowledge / "z-inaccessible"
        real_scandir = os.scandir

        def inaccessible(path):
            if Path(path) == blocked:
                raise PermissionError("synthetic private directory diagnostic")
            return real_scandir(path)

        with patch.object(os, "scandir", side_effect=inaccessible):
            with self.assertRaises(GovernedError) as caught:
                self.scanner._scan_files()
        self.assertEqual(caught.exception.code, "knowledge_directory_scan_failed")
        self.assertNotIn("synthetic private directory diagnostic", str(caught.exception))
        self.assert_originals_unchanged()


class DirectoryLifecycleTests(DirectoryFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.config.state_dir)
        self.addCleanup(self.store.close)
        self.graph = MemoryGraph(self.store.db)
        self.scratch, self.adapter = Scratch(), LabelAdapter()
        self.service = KnowledgeService(self.config, self.store, self.graph, self.adapter, self.scratch)
        self.quiet = patch("builtins.print")
        self.quiet.start()
        self.addCleanup(self.quiet.stop)

    async def asyncTearDown(self):
        await self.service.close()

    async def test_management_guide_and_staged_raws_are_never_snapshotted_or_labelled(self):
        self.file(".indeces/README.md", b"fixed guide that must not become model input")
        self.file("_staging/raw/paper.pdf", b"%PDF-unindexed-staged-source")
        source = self.file("subjects/active.txt")
        original_snapshot = self.service._snapshot_file
        snapshotted = []

        def guarded_snapshot(path):
            self.assertEqual(path, source)
            snapshotted.append(path)
            return original_snapshot(path)

        with patch.object(self.service, "_snapshot_file", side_effect=guarded_snapshot):
            self.service.scan_once()
        self.assertEqual(snapshotted, [source])
        self.assertEqual([r[0] for r in self.store.db.execute("SELECT path FROM knowledge_versions")], ["subjects/active.txt"])
        self.assertTrue(await self.service.label_next())
        self.assertFalse(await self.service.convert_next())
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertNotIn("fixed guide", str(self.adapter.calls))
        self.assertNotIn("staged-source", str(self.adapter.calls))
        self.assert_originals_unchanged()

    async def test_257_candidates_retire_existing_published_source_without_modifying_raws(self):
        original_path = self.file("previously-published.txt")
        self.service.scan_once()
        await self.service.label_next()
        old = self.store.db.execute("SELECT source_id FROM knowledge_published WHERE scope=?", (self.service.scope,)).fetchone()[0]
        for number in range(256):
            self.file(f"papers/{number:03}.pdf", b"%PDF-synthetic-never-converted")
        with self.assertRaises(GovernedError) as caught:
            self.service.scan_once()
        self.assertEqual(caught.exception.code, "knowledge_file_count_limit")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_published WHERE scope=?", (self.service.scope,)).fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_desired WHERE scope=?", (self.service.scope,)).fetchone()[0], 0)
        version = self.store.db.execute("SELECT status,error,raw_text FROM knowledge_versions WHERE source_id=?", (old,)).fetchone()
        self.assertEqual(tuple(version), ("superseded", "knowledge_file_count_limit", self.originals[original_path].decode()))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM memory_records WHERE source_id=? AND active=0", (old,)).fetchone()[0], 1)
        self.assertEqual(self.graph.retrieve(self.service.scope, [], "alpha", 2, event_id="directory-overflow"), [])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertFalse(await self.service.convert_next())
        self.assert_originals_unchanged()

    async def test_file_reparse_entry_cannot_falsely_overflow_and_retire_published_source(self):
        self.config.knowledge = replace(self.config.knowledge, max_files=1)
        self.file("ordinary.txt")
        self.service.scan_once()
        await self.service.label_next()
        old = self.store.db.execute("SELECT source_id FROM knowledge_published WHERE scope=?", (self.service.scope,)).fetchone()[0]
        alias = self.file("file-alias.pdf")
        real_lstat = Path.lstat

        def synthetic_lstat(path):
            if path == alias:
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_file_attributes=0x400)
            return real_lstat(path)

        with patch.object(Path, "lstat", synthetic_lstat):
            self.service.scan_once()
        self.assertEqual(self.store.db.execute("SELECT source_id FROM knowledge_published WHERE scope=?", (self.service.scope,)).fetchone()[0], old)
        self.assertEqual(self.store.db.execute("SELECT status FROM knowledge_versions WHERE source_id=?", (old,)).fetchone()[0], "ready")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], 1)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertFalse(any(event == "knowledge_retired" for event, _ in self.scratch.events))
        self.assert_originals_unchanged()

    async def test_partial_census_error_preserves_all_published_sources_without_queuing_new_labels(self):
        self.file("a-readable/old.txt")
        self.file("z-inaccessible/old.txt")
        self.service.scan_once()
        while await self.service.label_next():
            pass
        published = [tuple(row) for row in self.store.db.execute("SELECT * FROM knowledge_published ORDER BY path")]
        calls_before = len(self.adapter.calls)
        versions_before = self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0]
        self.file("a-readable/new.txt")
        blocked = self.knowledge / "z-inaccessible"
        real_scandir = os.scandir

        def inaccessible(path):
            if Path(path) == blocked:
                raise PermissionError("synthetic private directory diagnostic")
            return real_scandir(path)

        with patch.object(os, "scandir", side_effect=inaccessible):
            with self.assertRaises(GovernedError) as caught:
                self.service.scan_once()
        self.assertEqual(caught.exception.code, "knowledge_directory_scan_failed")
        self.assertNotIn("synthetic private directory diagnostic", str(caught.exception))
        self.assertEqual([tuple(row) for row in self.store.db.execute("SELECT * FROM knowledge_published ORDER BY path")], published)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0], versions_before)
        self.assertEqual(len(self.adapter.calls), calls_before)
        self.assertFalse(await self.service.label_next())
        self.assertFalse(any(event == "knowledge_retired" for event, _ in self.scratch.events))
        self.assert_originals_unchanged()


if __name__ == "__main__":
    unittest.main()
