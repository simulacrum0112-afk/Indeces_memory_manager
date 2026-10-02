from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from indeces.config import load_config
from indeces.path_policy import (PathPolicyError, validate_config_path,
                                 validate_knowledge_root, validate_runtime_paths)


class PathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.example = (Path(__file__).resolve().parents[1] / "config.example.toml").read_text(encoding="utf-8")
        self.path = self.root / "config.toml"
        self.path.write_text(self.example, encoding="utf-8")

    def config(self, **updates):
        values = {name: self.root / name.removesuffix("_dir")
                  for name in ("state_dir", "scratch_dir", "knowledge_dir")}
        return SimpleNamespace(**{**values, **updates})

    def assert_code(self, code, operation):
        with self.assertRaises(PathPolicyError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertIn(code, str(caught.exception))
        if code == "linked_path_forbidden":
            self.assertIn("must not be a link", str(caught.exception))
        return caught.exception

    def symlink(self, name, target):
        alias = self.root / name
        try:
            alias.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink creation unavailable")
        return alias

    def test_defaults_anchor_knowledge_without_creating_managed_directories(self):
        config = load_config(self.path)
        self.assertEqual(config.knowledge_dir, self.root / "knowledge")
        self.assertFalse(config.state_dir.exists())
        self.assertFalse(config.scratch_dir.exists())
        self.assertFalse(config.knowledge_dir.exists())

    def test_loader_rejects_unrelated_directory_and_reports_expected(self):
        self.path.write_text(self.example.replace('knowledge_dir = "knowledge"', 'knowledge_dir = "literatures"'), encoding="utf-8")
        error = self.assert_code("knowledge_root_mismatch", lambda: load_config(self.path))
        self.assertEqual(error.expected, self.root / "knowledge")
        self.assertIn("expected=", str(error))
        self.assertFalse((self.root / "literatures").exists())

    def test_loader_rejects_external_directory_even_when_named_knowledge(self):
        self.path.write_text(self.example.replace('knowledge_dir = "knowledge"', 'knowledge_dir = "../knowledge"'), encoding="utf-8")
        error = self.assert_code("knowledge_root_mismatch", lambda: load_config(self.path))
        self.assertEqual(error.expected, self.root / "knowledge")

    def test_loader_accepts_same_exact_knowledge_root_with_dot_segments(self):
        self.path.write_text(self.example.replace('knowledge_dir = "knowledge"', 'knowledge_dir = "./knowledge"'), encoding="utf-8")
        self.assertEqual(load_config(self.path).knowledge_dir, self.root / "knowledge")

    def test_onedrive_component_variants_rejected_without_reads(self):
        for name in ("OneDrive", "onedrive", "ONEDRIVE - Lab", "OneDriveBusiness", "OneDriveConsumer"):
            with self.subTest(name=name), patch.object(Path, "open", side_effect=AssertionError("must not open OneDrive")):
                self.assert_code("onedrive_path_forbidden", lambda: load_config(self.root / name / "config.toml"))
                self.assert_code("onedrive_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=self.root / name / "knowledge")))
            self.assertFalse((self.root / name).exists())

    def test_windows_onedrive_spelling_recognized_on_any_platform(self):
        self.assert_code("onedrive_path_forbidden", lambda: validate_config_path(Path(r"C:\Users\example\OneDrive - Lab\config.toml")))

    def test_onedrive_environment_roots_reject_custom_folder_names(self):
        for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            with self.subTest(variable=variable), patch.dict(os.environ, {variable: str(self.root / "cloud")}, clear=True):
                for field in ("state_dir", "scratch_dir", "knowledge_dir"):
                    with self.subTest(field=field):
                        self.assert_code("onedrive_path_forbidden", lambda: validate_runtime_paths(self.config(**{field: self.root / "cloud" / field.removesuffix("_dir")})))
                with patch.object(Path, "open", side_effect=AssertionError("must not open cloud config")):
                    self.assert_code("onedrive_path_forbidden", lambda: load_config(self.root / "cloud" / "config.toml"))

    def test_onedrive_environment_root_uses_components_not_string_prefix(self):
        with patch.dict(os.environ, {"OneDrive": str(self.root / "cloud")}, clear=True):
            accepted = self.root / "cloud-elsewhere" / "knowledge"
            self.assertEqual(validate_knowledge_root(self.config(knowledge_dir=accepted)), accepted)

    def test_raw_onedrive_segment_cannot_be_hidden_with_parent_traversal(self):
        path = self.root / "OneDrive" / ".." / "knowledge"
        self.assert_code("onedrive_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=path)))

    def test_direct_construction_checks_dedicated_name_without_io(self):
        expected = self.root / "knowledge"
        with patch.object(Path, "open", side_effect=AssertionError("source files must not be read")):
            self.assertEqual(validate_knowledge_root(self.config()), expected)
            self.assert_code("knowledge_root_mismatch", lambda: validate_knowledge_root(self.config(knowledge_dir=self.root / "literatures")))
        self.assertFalse(expected.exists())

    def test_config_file_symlink_rejected_before_open(self):
        alias = self.root / "alias.toml"
        try:
            alias.symlink_to(self.path)
        except (OSError, NotImplementedError):
            self.skipTest("file symlink creation unavailable")
        with patch.object(Path, "open", side_effect=AssertionError("must not open linked config")):
            self.assert_code("linked_path_forbidden", lambda: load_config(alias))

    def test_directory_link_ancestor_rejected_before_resolving(self):
        target = self.root / "plain"
        target.mkdir()
        alias = self.symlink("alias", target)
        with patch.object(Path, "open", side_effect=AssertionError("must not read linked files")):
            self.assert_code("linked_path_forbidden", lambda: validate_config_path(alias / "config.toml"))
            self.assert_code("linked_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=alias / "knowledge")))

    def test_raw_link_ancestor_cannot_be_hidden_with_parent_traversal(self):
        target = self.root / "plain"
        target.mkdir()
        alias = self.symlink("alias", target)
        self.assert_code("linked_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=alias / ".." / "knowledge")))

    def test_dangling_link_is_rejected(self):
        alias = self.symlink("alias", self.root / "missing")
        self.assert_code("linked_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=alias / "knowledge")))

    def test_normalized_path_also_checks_ancestors_after_missing_dot_segments(self):
        target = self.root / "plain"
        target.mkdir()
        alias = self.symlink("alias", target)
        candidate = self.root / "missing" / ".." / alias.name / "knowledge"
        self.assert_code("linked_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=candidate)))

    def test_runtime_rejects_any_linked_managed_directory(self):
        target = self.root / "plain"
        target.mkdir()
        alias = self.symlink("alias", target)
        for field in ("state_dir", "scratch_dir", "knowledge_dir"):
            with self.subTest(field=field):
                self.assert_code("linked_path_forbidden", lambda: validate_runtime_paths(self.config(**{field: alias / field.removesuffix("_dir")})))

    def test_windows_reparse_attribute_rejected_even_without_symlink_mode(self):
        original_lstat = Path.lstat
        suspect = self.root / "junction"
        def metadata(path, *args, **kwargs):
            if path == suspect:
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return original_lstat(path, *args, **kwargs)
        with patch.object(Path, "lstat", metadata):
            self.assert_code("linked_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=suspect / "knowledge")))

    @unittest.skipUnless(os.name == "nt", "Windows junction creation only")
    def test_real_windows_junction_rejected_for_config_and_runtime_paths(self):
        target = self.root / "plain"
        target.mkdir()
        junction = self.root / "junction"
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = f"New-Item -ItemType Junction -Path {quote(junction)} -Target {quote(target)} -ErrorAction Stop | Out-Null"
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                       check=True, capture_output=True, text=True)
        # Removing the junction itself is non-recursive and leaves the target alone.
        self.addCleanup(junction.rmdir)
        self.assert_code("linked_path_forbidden", lambda: validate_config_path(junction / "config.toml"))
        self.assert_code("linked_path_forbidden", lambda: validate_knowledge_root(self.config(knowledge_dir=junction / "knowledge")))
        for field in ("state_dir", "scratch_dir", "knowledge_dir"):
            with self.subTest(field=field):
                self.assert_code("linked_path_forbidden", lambda: validate_runtime_paths(self.config(**{field: junction / field.removesuffix("_dir")})))
        self.assertEqual(list(target.iterdir()), [])

    def test_metadata_inspection_failure_fails_closed(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("private diagnostic")):
            error = self.assert_code("path_inspection_failed", lambda: validate_knowledge_root(self.config()))
        self.assertNotIn("private diagnostic", str(error))

    def test_nul_path_rejected_before_metadata(self):
        with patch.object(Path, "lstat", side_effect=AssertionError("invalid path must not be inspected")):
            self.assert_code("invalid_managed_path", lambda: validate_config_path(Path("bad\0config.toml")))

    def test_loader_rejects_onedrive_runtime_paths_without_creating_them(self):
        for field in ("state_dir", "scratch_dir", "knowledge_dir"):
            with self.subTest(field=field):
                text = self.example.replace(f'{field} = "{field.removesuffix("_dir")}"', f'{field} = "OneDrive/{field.removesuffix("_dir")}"')
                self.path.write_text(text, encoding="utf-8")
                self.assert_code("onedrive_path_forbidden", lambda: load_config(self.path))
                self.assertFalse((self.root / "OneDrive").exists())


if __name__ == "__main__":
    unittest.main()
