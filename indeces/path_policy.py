"""Local path boundaries, checked before opening or creating managed files.

Only filesystem metadata is inspected here.  In particular, this module never
opens source documents, creates directories, or resolves a link before checking
its original spelling.  Callers must revalidate before subsequent filesystem
operations; these checks do not provide an operating-system atomic path lease.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat


class PathPolicyError(ValueError):
    """A stable path-policy code with a readable, non-content diagnostic."""

    def __init__(self, code: str, message: str, *, expected: Path | None = None):
        self.code = code
        self.expected = expected
        diagnostic = f"{code}: {message}"
        if expected is not None:
            diagnostic += f"; expected={expected}"
        super().__init__(diagnostic)


def _absolute(path: Path) -> Path:
    # abspath normalizes lexical dot segments, without following filesystem links.
    return Path(os.path.abspath(os.fspath(path)))


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path)))


def _onedrive_roots():
    names = {"onedrive", "onedriveconsumer", "onedrivecommercial"}
    for name, value in os.environ.items():
        if name.casefold() in names and value:
            yield _absolute(Path(value))


def _reject_onedrive(path: Path, role: str) -> None:
    # Also recognize Windows spellings when tests/audits run on another platform.
    # Prefix matching covers OneDrive, OneDrive - Organization and OneDriveBusiness.
    if any(part.casefold().startswith("onedrive")
           for part in re.split(r"[\\/]", os.fspath(path)) if part):
        raise PathPolicyError("onedrive_path_forbidden", f"{role} cannot use a OneDrive path")
    candidate = _path_key(_absolute(path))
    for root in _onedrive_roots():
        root_key = _path_key(root)
        try:
            inside = os.path.commonpath((candidate, root_key)) == root_key
        except ValueError:
            inside = False  # Different Windows drives cannot overlap.
        if inside:
            raise PathPolicyError("onedrive_path_forbidden", f"{role} cannot use a OneDrive environment root")


def _reject_link_ancestors(path: Path, role: str) -> None:
    current = Path(path.anchor)
    parts = path.parts[1:] if path.anchor else path.parts
    for part in (None, *parts):
        if part is not None:
            current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue  # Missing managed directories are allowed; nothing is created.
        except OSError as exc:
            raise PathPolicyError("path_inspection_failed", f"cannot inspect {role} path metadata") from exc
        if (stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise PathPolicyError("linked_path_forbidden", f"{role} must not be a link or have a symlink, junction or reparse-point ancestor")


def _validate_path(value, role: str) -> Path:
    try:
        path = Path(value)
    except (TypeError, ValueError) as exc:
        raise PathPolicyError("invalid_managed_path", f"{role} must be a filesystem path") from exc
    if "\0" in os.fspath(path):
        raise PathPolicyError("invalid_managed_path", f"{role} must be a filesystem path")
    # Preserve raw '..' traversal for checks before lexical normalization.
    raw_absolute = path if path.is_absolute() else Path.cwd() / path
    normalized = _absolute(path)
    for candidate in (raw_absolute, normalized):
        _reject_onedrive(candidate, role)
        _reject_link_ancestors(candidate, role)
    return normalized


def validate_config_path(path: Path) -> Path:
    """Validate the supplied configuration spelling before resolve/open."""
    return _validate_path(path, "config")


def validate_managed_path(path: Path, role: str) -> Path:
    """Apply the same boundary to low-level managed-file entry points."""
    return _validate_path(path, role)


def validate_knowledge_root(config, *, expected: Path | None = None) -> Path:
    """Return a local, unlinked knowledge directory; enforce loader anchoring.

    The configuration loader supplies its exact parent/knowledge expectation.
    Direct runtime/test construction still requires the dedicated knowledge name.
    """
    root = _validate_path(getattr(config, "knowledge_dir", None), "knowledge_dir")
    if root.name.casefold() != "knowledge":
        raise PathPolicyError("knowledge_root_mismatch", "knowledge_dir must be the dedicated knowledge subdirectory",
                              expected=expected or root.parent / "knowledge")
    if expected is not None:
        expected = _validate_path(expected, "expected knowledge_dir")
        if _path_key(root) != _path_key(expected):
            raise PathPolicyError("knowledge_root_mismatch", "knowledge_dir must be beside the project configuration",
                                  expected=expected)
    return root


def validate_runtime_paths(config) -> None:
    """Reject OneDrive and linked ancestors for all managed runtime roots."""
    for role in ("state_dir", "scratch_dir", "knowledge_dir"):
        _validate_path(getattr(config, role, None), role)
    validate_knowledge_root(config)
