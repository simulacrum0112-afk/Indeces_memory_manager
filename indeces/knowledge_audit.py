"""Read-only file census, independent of bounded graph/progress displays.

No service, model, migration, scratch reader or source-text export is used.
Persisted errors and current validation predictions are deliberately separate.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .config import PdfConfig
from .knowledge_directory import RESERVED_ROOT_DIRECTORIES, SUPPORTED_SUFFIXES
from .knowledge_progress import _terminal_text
from .observer_data import _database, _scope, _tables


def _stage(error, status):
    if error:
        if error.startswith("pdf_") or error.startswith("knowledge_pdf_"):
            return "conversion"
        if error.startswith("knowledge_file_") or error in {
                "UnicodeDecodeError", "knowledge_invalid_utf8", "knowledge_path_outside_root"}:
            return "validation"
        if "publication" in error or "publish" in error:
            return "publication"
        return "labelling"
    return {"ready": "published", "conversion_pending": "conversion",
            "converting": "conversion", "pending": "labelling",
            "labelling": "labelling"}.get(status, "discovery")


def _census(config):
    root = Path(config.knowledge_dir)
    rows, warnings = {}, []
    if root.is_symlink() or root.is_junction():
        return rows, ["knowledge_root_link_skipped"]
    if not root.exists():
        return rows, ["knowledge_directory_missing"]
    def failed(error):
        warnings.append("knowledge_directory_scan_failed")
    for directory, dirs, names in os.walk(root, topdown=True, followlinks=False, onerror=failed):
        parent = Path(directory)
        retained = []
        for name in sorted(dirs):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or path.is_junction():
                rows[relative + "/"] = {"path": relative + "/", "suffix": "",
                    "byte_count": None, "state": "skipped", "stage": "discovery",
                    "skip_reason": "directory_link", "digest": None}
            else:
                retained.append(name)
        dirs[:] = retained
        for name in sorted(names):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            row = {"path": relative, "suffix": path.suffix.lower(), "byte_count": None,
                   "state": "unseen", "stage": "discovery", "digest": None}
            rows[relative] = row
            try:
                info = path.lstat()
                row["byte_count"] = info.st_size
                reserved = relative.split("/", 1)[0].casefold() in RESERVED_ROOT_DIRECTORIES
                reparse = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if reserved or path.is_symlink() or reparse or not stat.S_ISREG(info.st_mode) or row["suffix"] not in SUPPORTED_SUFFIXES:
                    row.update(state="skipped", skip_reason="reserved_directory" if reserved else
                               "file_link_or_nonregular" if path.is_symlink() or reparse or not stat.S_ISREG(info.st_mode)
                               else "unsupported_format")
                    continue
                pdf = row["suffix"] == ".pdf"
                limits = getattr(config, "pdf", None) or PdfConfig()
                limit, config_key = config.knowledge.file_limit(row["suffix"], limits)
                row.update(limit_bytes=limit, limit_config_key=config_key)
                if info.st_size > limit:
                    row.update(state="rejected", stage="validation", validation_error="knowledge_file_size_limit")
                    continue
                with path.open("rb") as stream:
                    raw = stream.read(limit + 1)
                after = path.stat()
                if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size):
                    row.update(state="pending", stage="snapshot", validation_error="file_changed_during_audit")
                elif len(raw) > limit:
                    row.update(state="rejected", stage="validation", validation_error="knowledge_file_size_limit")
                elif pdf and not raw:
                    row.update(state="rejected", stage="validation", validation_error="knowledge_pdf_empty")
                else:
                    if not pdf:
                        raw.decode("utf-8-sig")
                    row["digest"] = hashlib.sha256(raw).hexdigest()
            except UnicodeDecodeError:
                row.update(state="rejected", stage="validation", validation_error="knowledge_invalid_utf8")
            except OSError as error:
                row.update(state="unavailable", stage="snapshot", validation_error=type(error).__name__)
    supported = sum(row["suffix"] in SUPPORTED_SUFFIXES and "skip_reason" not in row for row in rows.values())
    if supported > config.knowledge.max_files:
        warnings.append("knowledge_file_count_limit")
        for row in rows.values():
            if row["suffix"] in SUPPORTED_SUFFIXES and "skip_reason" not in row:
                row.update(state="rejected", stage="discovery", validation_error="knowledge_file_count_limit")
    return rows, warnings


def audit_snapshot(config):
    """Return all discovered paths and saved current pointers, with no display cap."""
    files, warnings = _census(config)
    result = {"schema_version": 1, "status": "ok", "files": [], "warnings": warnings,
              "truncated": False, "statistics": None,
              "limits": {"text_bytes": config.knowledge.max_file_bytes,
                         "pdf_bytes": (getattr(config, "pdf", None) or PdfConfig()).max_file_bytes,
                         "candidate_files": config.knowledge.max_files},
              "note": "Persisted state is not proof that a service is running; validation_error is a current read-only prediction."}
    if not config.discord.guild_id:
        result["status"] = "scope_unconfigured"
    else:
        try:
            with _database(config) as db:
                if db is None:
                    result["status"] = "database_missing"
                else:
                    tables = _tables(db)
                    if {"knowledge_versions", "knowledge_desired", "knowledge_published", "knowledge_chunks"} <= tables:
                        parameters = {"scope": _scope(config)}
                        versions = {row["source_id"]: dict(row) for row in db.execute("""SELECT
                            v.source_id,v.path,v.digest,v.status,v.error,v.input_tokens,v.output_tokens,v.elapsed_seconds,
                            (SELECT COUNT(*) FROM knowledge_chunks c WHERE c.source_id=v.source_id) AS total_chunks,
                            (SELECT COUNT(marks_json) FROM knowledge_chunks c WHERE c.source_id=v.source_id) AS labelled_chunks,
                            (SELECT COUNT(*) FROM knowledge_chunks c,json_each(c.marks_json) j
                             WHERE c.source_id=v.source_id) AS chunk_mark_occurrences
                            FROM knowledge_versions v WHERE v.scope=:scope AND v.source_id IN (
                              SELECT source_id FROM knowledge_desired WHERE scope=:scope UNION
                              SELECT source_id FROM knowledge_published WHERE scope=:scope)""", parameters)}
                        pointers = {}
                        for kind, table in (("desired", "knowledge_desired"), ("published", "knowledge_published")):
                            for row in db.execute(f"SELECT path,source_id FROM {table} WHERE scope=:scope", parameters):
                                version = versions.get(row["source_id"])
                                saved = pointers.setdefault(row["path"], {})
                                if version is None or version["path"] != row["path"]:
                                    saved.setdefault("pointer_warnings", []).append(kind + "_pointer_invalid")
                                    version = None
                                saved[kind] = version
                        for path, saved in pointers.items():
                            row = files.setdefault(path, {"path": path, "suffix": Path(path).suffix.lower(),
                                "byte_count": None, "state": "missing", "stage": "discovery", "digest": None})
                            row.update(saved)
                            desired, published = saved.get("desired"), saved.get("published")
                            row["published_retrievable"] = bool(published and published["status"] == "ready")
                            row["current_file_matches_desired"] = bool(desired and row["digest"] and row["digest"] == desired["digest"])
                            if desired:
                                row["stored_error"] = desired["error"]
                                row["stored_stage"] = _stage(desired["error"], desired["status"])
                                if row["state"] not in {"rejected", "skipped", "missing", "unavailable"}:
                                    if row["current_file_matches_desired"]:
                                        row.update(state=desired["status"], stage=row["stored_stage"])
                                        if desired["status"] == "ready" and (not published or published["source_id"] != desired["source_id"]):
                                            row.update(state="ready_unpublished", stage="publication")
                                    else:
                                        row.update(state="changed", stage="snapshot")
                    else:
                        result["status"] = "schema_unavailable"
                    if "knowledge_file_audit" in tables:
                        for item in db.execute("SELECT * FROM knowledge_file_audit WHERE scope=?", (_scope(config),)):
                            entry = dict(item)
                            path = entry.pop("path")
                            entry.pop("scope", None)
                            entry["details"] = json.loads(entry.pop("details_json"))
                            row = files.setdefault(path, {"path": path, "suffix": entry["suffix"],
                                "byte_count": None, "state": "missing", "stage": "discovery", "digest": None})
                            row["last_observation"] = entry
                            if row.get("desired") and entry.get("source_id") == row["desired"]["source_id"]:
                                row["stored_stage"] = entry["stage"]
                                if row["current_file_matches_desired"] and row["state"] not in {"rejected", "skipped", "missing", "unavailable"}:
                                    row["stage"] = entry["stage"]
                    if "memory_records" in tables:
                        counts = dict(db.execute("""SELECT COUNT(*) AS active_records,
                            COUNT(DISTINCT source_id) AS active_source_versions FROM memory_records
                            WHERE scope=? AND active=1""", (_scope(config),)).fetchone())
                        counts.update(dict(db.execute("""SELECT COUNT(*) AS annotation_occurrences,
                            COUNT(DISTINCT j.value) AS distinct_marks FROM memory_records r,
                            json_each(r.marks_json) j WHERE r.scope=? AND r.active=1""", (_scope(config),)).fetchone()))
                        result["statistics"] = counts
                        if {"knowledge_chunks", "knowledge_desired"} <= tables:
                            counts.update(dict(db.execute("""SELECT COUNT(*) AS desired_chunk_mark_occurrences,
                                COUNT(DISTINCT j.value) AS desired_distinct_marks_including_unpublished
                                FROM knowledge_desired d JOIN knowledge_chunks c ON c.source_id=d.source_id,
                                json_each(c.marks_json) j WHERE d.scope=?""", (_scope(config),)).fetchone()))
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
            result["status"] = "snapshot_unavailable"
            result["warnings"].append("saved_snapshot_not_fully_read")
    result["files"] = [files[path] for path in sorted(files)]
    result["total_paths"] = len(files)
    result["states"] = dict(Counter(row["state"] for row in result["files"]))
    return result


def render_audit(report):
    lines = ["Indeces 摄入审计（只读；完整文件清单）", f"保存快照={report['status']}；路径数={report['total_paths']}；状态={_terminal_text(report['states'])}",
             "stored_error 为已记录失败；validation_error 为本次校验预测；历史快照不表示服务在线。"]
    if report["statistics"] is not None:
        s = report["statistics"]
        lines.append(f"有效块={s['active_records']}；来源版本={s['active_source_versions']}；标注出现次数={s['annotation_occurrences']}；去重词节点={s['distinct_marks']}")
    for row in report["files"]:
        desired = row.get("desired") or {}
        line = (f"- {_terminal_text(row['path'])} | {_terminal_text(row['suffix'] or '(无扩展名)')} | bytes={row['byte_count']} | "
                f"{_terminal_text(row['state'])} | stage={_terminal_text(row['stage'])} | 标词块={desired.get('labelled_chunks', 0)}/{desired.get('total_chunks', 0)}")
        for key in ("stored_error", "validation_error", "skip_reason"):
            if row.get(key):
                line += f" | {key}={_terminal_text(row[key])}"
        if row.get("validation_error") == "knowledge_file_size_limit":
            line += f" | limit={row['limit_bytes']} ({row['limit_config_key']})"
        observation = row.get("last_observation")
        if observation and observation.get("error"):
            line += (f" | last_observation={_terminal_text(observation['state'])}"
                     f"/{_terminal_text(observation['stage'])}/{_terminal_text(observation['error'])}")
        if row.get("pointer_warnings"):
            line += " | " + _terminal_text(",".join(row["pointer_warnings"]))
        lines.append(line)
    if report["warnings"]:
        lines.append("提示：" + ", ".join(report["warnings"]))
    return "\n".join(lines)
