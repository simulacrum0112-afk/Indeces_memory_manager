from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from indeces import knowledge_directory as directory
from indeces.config import load_config


class KnowledgeDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        baseline = load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
        self.config = replace(baseline, knowledge_dir=self.root / "knowledge",
                              state_dir=self.root / "state", scratch_dir=self.root / "scratch")
        self.guide = self.config.knowledge_dir / ".indeces" / "README.md"

    def test_prepare_creates_only_root_staging_and_fixed_guide(self):
        root = directory.prepare_knowledge_directory(self.config)
        self.assertEqual(root, self.config.knowledge_dir)
        self.assertTrue(root.is_absolute())
        self.assertEqual({item.relative_to(root).as_posix() for item in root.rglob("*")},
                         {"_staging", ".indeces", ".indeces/README.md"})
        self.assertEqual(self.guide.read_bytes(), directory.GUIDE.encode("utf-8"))
        self.assertLessEqual(self.guide.stat().st_size, directory.MAX_GUIDE_BYTES)
        self.assertFalse(self.config.state_dir.exists())
        self.assertFalse(self.config.scratch_dir.exists())

    def test_existing_raw_staging_and_custom_guide_are_never_opened_or_changed(self):
        root = self.config.knowledge_dir
        (root / "topic" / "2026").mkdir(parents=True)
        (root / "_staging" / "unreviewed").mkdir(parents=True)
        self.guide.parent.mkdir()
        contents = {root / "topic" / "2026" / "paper.pdf": b"synthetic raw PDF bytes",
                    root / "notes.md": b"synthetic private raw notes",
                    root / "_staging" / "unreviewed" / "paper.txt": b"synthetic staged raw",
                    self.guide: b"custom user-owned guide\x00" + b"x" * (directory.MAX_GUIDE_BYTES + 1)}
        for path, data in contents.items():
            path.write_bytes(data)
        before = {path: path.stat().st_mtime_ns for path in contents}
        with patch.object(Path, "read_bytes", side_effect=AssertionError("must not read materials or guide")), \
                patch.object(Path, "read_text", side_effect=AssertionError("must not read materials or guide")), \
                patch.object(directory.os, "open", side_effect=AssertionError("no existing file should open")):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual({path: path.read_bytes() for path in contents}, contents)
        self.assertEqual({path: path.stat().st_mtime_ns for path in contents}, before)
        self.assertFalse((root / "_staging" / "README.md").exists())

    def test_prepare_is_idempotent_without_rewriting_the_managed_guide(self):
        directory.prepare_knowledge_directory(self.config)
        before = self.guide.stat().st_mtime_ns
        with patch.object(directory.os, "open", side_effect=AssertionError("existing guide cannot open")):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual(self.guide.stat().st_mtime_ns, before)

    def test_relative_alias_is_resolved_without_moving_materials(self):
        original = self.config.knowledge_dir
        original.mkdir()
        material = original / "preserved.txt"
        material.write_bytes(b"synthetic raw remains in place")
        alias = self.root / "missing-name" / ".." / "knowledge"
        root = directory.prepare_knowledge_directory(replace(self.config, knowledge_dir=alias))
        self.assertEqual(root, original)
        self.assertEqual(material.read_bytes(), b"synthetic raw remains in place")
        self.assertFalse((self.root / "missing-name").exists())

    def test_explicit_root_symlink_alias_is_refused_before_writing_actual_directory(self):
        actual = self.root / "actual"
        actual.mkdir()
        alias = self.root / "alias"
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation unavailable on this host")
        with self.assertRaises(directory.KnowledgeDirectoryError):
            directory.prepare_knowledge_directory(replace(self.config, knowledge_dir=alias))
        self.assertEqual(list(actual.iterdir()), [])
        self.assertTrue(alias.is_symlink())

    def test_unsafe_types_are_refused_without_overwriting_user_data(self):
        for relative in ("", "_staging", ".indeces", ".indeces/README.md"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "knowledge"
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if relative == ".indeces/README.md":
                    target.mkdir()
                    preserved = target / "notes.txt"
                    preserved.write_bytes(b"user-owned directory")
                else:
                    target.write_bytes(b"user-owned file")
                    preserved = target
                with self.assertRaises(directory.KnowledgeDirectoryError):
                    directory.prepare_knowledge_directory(replace(self.config, knowledge_dir=root))
                self.assertEqual(preserved.read_bytes(), b"user-owned directory" if relative.endswith("README.md") else b"user-owned file")

    def test_reparse_components_are_refused_before_creating_or_opening_files(self):
        directory.prepare_knowledge_directory(self.config)
        original_lstat = Path.lstat
        for target in (self.root, self.config.knowledge_dir, self.guide.parent,
                       self.config.knowledge_dir / "_staging", self.guide):
            def reparse(path, *args, **kwargs):
                info = original_lstat(path, *args, **kwargs)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            with self.subTest(target=target.name), patch.object(Path, "lstat", new=reparse), \
                    patch.object(directory.os, "open", side_effect=AssertionError("unsafe path cannot open")), \
                    self.assertRaises(directory.KnowledgeDirectoryError):
                directory.prepare_knowledge_directory(self.config)

    def test_management_symlink_is_refused_without_following_it(self):
        self.config.knowledge_dir.mkdir()
        outside = self.root / "preserved"
        outside.mkdir()
        link = self.config.knowledge_dir / ".indeces"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation unavailable on this host")
        with self.assertRaises(directory.KnowledgeDirectoryError):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue(link.is_symlink())

    def test_atomic_publication_preserves_a_concurrently_created_custom_guide(self):
        original_link = os.link
        custom = b"user guide created while preparing"
        def concurrent(source, target, *args, **kwargs):
            self.assertEqual(Path(target), self.guide)
            self.guide.write_bytes(custom)
            return original_link(source, target, *args, **kwargs)
        with patch.object(directory.os, "link", side_effect=concurrent) as publish, \
                patch.object(Path, "read_bytes", side_effect=AssertionError("existing guide cannot read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("existing guide cannot read")):
            directory.prepare_knowledge_directory(self.config)
        publish.assert_called_once()
        self.assertEqual(self.guide.read_bytes(), custom)
        self.assertEqual(list(self.guide.parent.iterdir()), [self.guide])

    def test_publication_exposes_only_the_complete_synced_guide(self):
        original_fsync = os.fsync
        original_link = os.link
        synced = []
        def sync(descriptor):
            self.assertFalse(self.guide.exists())
            synced.append(descriptor)
            return original_fsync(descriptor)
        def publish(source, target, *args, **kwargs):
            self.assertEqual(len(synced), 1)
            self.assertEqual(Path(source).parent, self.guide.parent)
            self.assertEqual(Path(source).read_bytes(), directory.GUIDE.encode("utf-8"))
            self.assertFalse(self.guide.exists())
            return original_link(source, target, *args, **kwargs)
        with patch.object(directory.os, "fsync", side_effect=sync), \
                patch.object(directory.os, "link", side_effect=publish):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual(self.guide.read_bytes(), directory.GUIDE.encode("utf-8"))
        self.assertEqual(list(self.guide.parent.iterdir()), [self.guide])

    def test_generated_guide_limit_is_checked_before_creating_directories(self):
        with patch.object(directory, "MAX_GUIDE_BYTES", 1), self.assertRaisesRegex(directory.KnowledgeDirectoryError, "size_limit"):
            directory.prepare_knowledge_directory(self.config)
        self.assertFalse(self.config.knowledge_dir.exists())

    def test_private_temporary_is_removed_after_fsync_failure(self):
        with patch.object(directory.os, "fsync", side_effect=OSError("synthetic write failure")), self.assertRaises(OSError):
            directory.prepare_knowledge_directory(self.config)
        self.assertFalse(self.guide.exists())
        self.assertEqual(list(self.guide.parent.iterdir()), [])
        directory.prepare_knowledge_directory(self.config)
        self.assertEqual(self.guide.read_bytes(), directory.GUIDE.encode("utf-8"))

    def test_fsync_failure_does_not_remove_a_user_note_created_during_sync(self):
        custom = b"custom note created while fsync is running"
        def failed_sync(descriptor):
            self.assertFalse(self.guide.exists())
            self.guide.write_bytes(custom)
            raise OSError("synthetic fsync failure after user note")
        with patch.object(directory.os, "fsync", side_effect=failed_sync), \
                patch.object(directory.os, "link") as publish, \
                self.assertRaisesRegex(OSError, "fsync failure"):
            directory.prepare_knowledge_directory(self.config)
        publish.assert_not_called()
        self.assertEqual(self.guide.read_bytes(), custom)
        self.assertEqual(list(self.guide.parent.iterdir()), [self.guide])

    def test_fdopen_failure_closes_descriptor_and_removes_only_private_temporary(self):
        descriptors = []
        original_open = os.open
        def capture(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        with patch.object(directory.os, "open", side_effect=capture), \
                patch.object(directory.os, "fdopen", side_effect=OSError("synthetic fdopen failure")), self.assertRaises(OSError):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertFalse(self.guide.exists())
        self.assertEqual(list(self.guide.parent.iterdir()), [])

    def test_fstat_failure_closes_descriptor_and_removes_only_private_temporary(self):
        descriptors = []
        original_open = os.open
        def capture(path, flags, *args, **kwargs):
            descriptor = original_open(path, flags, *args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        with patch.object(directory.os, "open", side_effect=capture), \
                patch.object(directory.os, "fstat", side_effect=OSError("synthetic fstat failure")), \
                self.assertRaisesRegex(OSError, "fstat failure"):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertFalse(self.guide.exists())
        self.assertEqual(list(self.guide.parent.iterdir()), [])

    def test_concurrently_created_unsafe_target_is_refused_and_never_deleted(self):
        original_link = os.link
        def concurrent(source, target, *args, **kwargs):
            self.guide.mkdir()
            (self.guide / "custom.txt").write_bytes(b"preserved user-owned directory")
            return original_link(source, target, *args, **kwargs)
        with patch.object(directory.os, "link", side_effect=concurrent), \
                self.assertRaises(directory.KnowledgeDirectoryError):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual((self.guide / "custom.txt").read_bytes(), b"preserved user-owned directory")
        self.assertEqual(list(self.guide.parent.iterdir()), [self.guide])

    def test_unsupported_atomic_publication_uses_readonly_directory_fallback(self):
        output = io.StringIO()
        with redirect_stdout(output), \
                patch.object(directory.os, "link", side_effect=OSError("synthetic unsupported filesystem")) as publish, \
                patch.object(directory.os, "replace", side_effect=AssertionError("cannot overwrite as fallback")), \
                patch.object(directory.os, "startfile", create=True) as open_folder:
            returned = directory.show_knowledge_directory(self.config, open_directory=False)
        self.assertEqual(returned, self.config.knowledge_dir)
        publish.assert_called_once()
        open_folder.assert_not_called()
        self.assertFalse(self.guide.exists())
        self.assertEqual(list(self.guide.parent.iterdir()), [])
        self.assertIn("管理说明未准备：OSError", output.getvalue())
        self.assertIn("说明目标（本次未准备，可能不存在）", output.getvalue())
        self.assertNotIn("synthetic unsupported filesystem", output.getvalue())

    def test_substituted_descriptor_cannot_write_to_another_file(self):
        other = self.root / "preserved.data"
        other.write_bytes(b"preserved synthetic file")
        original_open = os.open
        def substitute(path, flags, *args, **kwargs):
            return original_open(other, os.O_WRONLY)
        with patch.object(directory.os, "open", side_effect=substitute), \
                self.assertRaisesRegex(directory.KnowledgeDirectoryError, "path_changed"):
            directory.prepare_knowledge_directory(self.config)
        self.assertEqual(other.read_bytes(), b"preserved synthetic file")
        self.assertFalse(self.guide.exists())

    def test_show_reports_configured_bounds_without_material_scan_or_gui(self):
        config = replace(self.config, knowledge=replace(self.config.knowledge, max_files=91, max_file_bytes=4567),
                         pdf=replace(self.config.pdf, max_pages=73))
        output = io.StringIO()
        with redirect_stdout(output), patch.object(directory.os, "startfile", create=True) as open_folder, \
                patch.object(Path, "rglob", side_effect=AssertionError("directory entry cannot scan materials")):
            root = directory.show_knowledge_directory(config, open_directory=False)
        self.assertEqual(root, config.knowledge_dir)
        self.assertIn(str(root), output.getvalue())
        self.assertIn("91", output.getvalue())
        self.assertIn("4567 bytes", output.getvalue())
        self.assertIn("73 页", output.getvalue())
        self.assertIn("撤下并归档", output.getvalue())
        self.assertIn("start", output.getvalue())
        open_folder.assert_not_called()

    def test_windows_opens_only_the_absolute_directory_and_nonwindows_never_launches_gui(self):
        root = directory.prepare_knowledge_directory(self.config)
        for platform in ("nt", "posix"):
            with self.subTest(platform=platform), redirect_stdout(io.StringIO()), \
                    patch.object(directory, "prepare_knowledge_directory", return_value=root), \
                    patch.object(directory.os, "name", platform), \
                    patch.object(directory.os, "startfile", create=True) as open_folder:
                directory.show_knowledge_directory(self.config)
                if platform == "nt":
                    open_folder.assert_called_once_with(str(root))
                else:
                    open_folder.assert_not_called()

    def test_failed_folder_open_keeps_accessible_path_and_scrubs_arbitrary_error_text(self):
        root = directory.prepare_knowledge_directory(self.config)
        output = io.StringIO()
        with redirect_stdout(output), patch.object(directory, "prepare_knowledge_directory", return_value=root), \
                patch.object(directory.os, "name", "nt"), \
                patch.object(directory.os, "startfile", side_effect=OSError("synthetic private diagnostic"), create=True):
            returned = directory.show_knowledge_directory(self.config)
        self.assertEqual(returned, root)
        self.assertIn(str(root), output.getvalue())
        self.assertIn("OSError", output.getvalue())
        self.assertNotIn("synthetic private diagnostic", output.getvalue())

    def test_readonly_existing_root_still_opens_without_guide_claim_or_write_retry(self):
        root = self.config.knowledge_dir
        root.mkdir()
        (root / "_staging").mkdir()
        material = root / "preserved.txt"
        material.write_bytes(b"synthetic raw remains readable")
        original_mkdir = Path.mkdir
        attempts = []
        def denied_guide_directory(path, *args, **kwargs):
            if path == self.guide.parent:
                attempts.append(path)
                raise PermissionError("synthetic private permission diagnostic")
            return original_mkdir(path, *args, **kwargs)
        open_folder = Mock()
        output = io.StringIO()
        with redirect_stdout(output), patch.object(Path, "mkdir", new=denied_guide_directory), \
                patch.object(directory, "os", SimpleNamespace(name="nt", startfile=open_folder)), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("must not read raw")), \
                patch.object(Path, "read_text", side_effect=AssertionError("must not read raw")), \
                patch.object(Path, "open", side_effect=AssertionError("fallback cannot open any file")), \
                patch.object(Path, "rglob", side_effect=AssertionError("fallback cannot scan raw")):
            returned = directory.show_knowledge_directory(self.config)
        self.assertEqual(returned, root)
        self.assertEqual(attempts, [self.guide.parent])
        open_folder.assert_called_once_with(str(root))
        self.assertIn("管理说明未准备：PermissionError", output.getvalue())
        self.assertIn("可能不存在", output.getvalue())
        self.assertIn("暂存区目标（本次未准备，可能不存在）", output.getvalue())
        self.assertNotIn("synthetic private permission diagnostic", output.getvalue())
        self.assertFalse(self.guide.exists())
        self.assertEqual(material.read_bytes(), b"synthetic raw remains readable")

    def test_fallback_does_not_read_or_rewrite_existing_custom_guide_and_staged_raw(self):
        root = directory.prepare_knowledge_directory(self.config)
        self.guide.write_bytes(b"custom private directory notes")
        staged = root / "_staging" / "private.pdf"
        staged.write_bytes(b"synthetic staged raw content")
        before = self.guide.stat().st_mtime_ns
        with redirect_stdout(io.StringIO()), \
                patch.object(directory, "prepare_knowledge_directory", side_effect=OSError("synthetic disk error")) as prepare, \
                patch.object(Path, "read_bytes", side_effect=AssertionError("existing guide and raw cannot read")), \
                patch.object(Path, "read_text", side_effect=AssertionError("existing guide and raw cannot read")), \
                patch.object(Path, "open", side_effect=AssertionError("fallback cannot open files")), \
                patch.object(Path, "mkdir", side_effect=AssertionError("fallback cannot create directories")), \
                patch.object(Path, "rglob", side_effect=AssertionError("fallback cannot scan raw")):
            returned = directory.show_knowledge_directory(self.config, open_directory=False)
        self.assertEqual(returned, root)
        prepare.assert_called_once_with(self.config)
        self.assertEqual(self.guide.read_bytes(), b"custom private directory notes")
        self.assertEqual(staged.read_bytes(), b"synthetic staged raw content")
        self.assertEqual(self.guide.stat().st_mtime_ns, before)

    def test_fallback_refuses_missing_root_and_unsafe_reserved_types_before_gui(self):
        for relative in (None, "", "_staging", ".indeces", ".indeces/README.md"):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve() / "knowledge"
                if relative is not None:
                    target = root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if relative.endswith("README.md"):
                        target.mkdir()
                    else:
                        target.write_bytes(b"preserved synthetic unsafe type")
                config = replace(self.config, knowledge_dir=root)
                open_folder = Mock()
                output = io.StringIO()
                with redirect_stdout(output), \
                        patch.object(directory, "prepare_knowledge_directory", side_effect=PermissionError()), \
                        patch.object(directory, "os", SimpleNamespace(name="nt", startfile=open_folder)), \
                        self.assertRaises(directory.KnowledgeDirectoryError):
                    directory.show_knowledge_directory(config)
                open_folder.assert_not_called()
                self.assertEqual(output.getvalue(), "")

    def test_fallback_rechecks_reparse_parent_and_reserved_paths_before_gui(self):
        root = directory.prepare_knowledge_directory(self.config)
        original_lstat = Path.lstat
        for target in (self.root, root / "_staging", self.guide.parent, self.guide):
            def unsafe(path, *args, **kwargs):
                info = original_lstat(path, *args, **kwargs)
                if path == target:
                    return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
                return info
            open_folder = Mock()
            with self.subTest(target=target.name), redirect_stdout(io.StringIO()), \
                    patch.object(directory, "prepare_knowledge_directory", side_effect=PermissionError()), \
                    patch.object(Path, "lstat", new=unsafe), \
                    patch.object(directory, "os", SimpleNamespace(name="nt", startfile=open_folder)), \
                    self.assertRaises(directory.KnowledgeDirectoryError):
                directory.show_knowledge_directory(self.config)
            open_folder.assert_not_called()

    def test_show_does_not_fallback_for_unsafe_path_or_other_non_io_errors(self):
        for error in (directory.KnowledgeDirectoryError("knowledge_directory_unsafe_path"),
                      ValueError("invalid configuration"), RuntimeError("programming error")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(directory, "prepare_knowledge_directory", side_effect=error), \
                    patch.object(directory, "_existing_safe_root") as fallback, \
                    patch.object(directory.os, "startfile", create=True) as open_folder, \
                    self.assertRaises(type(error)):
                directory.show_knowledge_directory(self.config)
            fallback.assert_not_called()
            open_folder.assert_not_called()

    def test_exports_and_fixed_guide_explain_reserved_roots_and_no_unlimited_index(self):
        self.assertEqual(directory.SUPPORTED_SUFFIXES, frozenset({".pdf", ".md", ".markdown", ".txt"}))
        self.assertEqual(directory.RESERVED_ROOT_DIRECTORIES, frozenset({".indeces", "_staging"}))
        self.assertIn("256", directory.GUIDE)
        self.assertIn("撤下并归档", directory.GUIDE)
        self.assertIn("主题子目录中的同名目录", directory.GUIDE)
        self.assertIn("暂存能力不等于无限扫描", directory.GUIDE)
        self.assertNotIn(str(self.root), directory.GUIDE)


if __name__ == "__main__":
    unittest.main()
