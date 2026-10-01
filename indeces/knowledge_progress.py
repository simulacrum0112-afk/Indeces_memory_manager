"""Metadata-only knowledge progress snapshots and a read-only terminal view.

No service constructors, source text, PDF archives, mark contents, graph or
scratch are read here. Persisted states do not establish that a service is live.
"""
from __future__ import annotations

from collections import Counter
import math
import sqlite3
import time
import unicodedata

from .observer_data import _database, _scope, _tables


MAX_FILES = 256
_MESSAGES = {
    "scope_unconfigured": "尚未配置 Discord Guild；此视图未打开数据库。",
    "database_missing": "尚无知识库数据库；此视图不创建数据库。",
    "schema_unavailable": "现有数据库缺少进度视图所需表或字段；未执行迁移。",
    "snapshot_unavailable": "知识库当前版本状态暂不可读；请稍后刷新。",
}
_REQUIRED = {
    "knowledge_versions": {"source_id", "path", "digest", "status", "error", "scope"},
    "knowledge_chunks": {"source_id", "marks_json"},
    "knowledge_desired": {"scope", "path", "source_id"},
    "knowledge_published": {"scope", "path", "source_id"},
}
_PATHS = """SELECT path FROM knowledge_desired WHERE scope=:scope
    UNION SELECT path FROM knowledge_published WHERE scope=:scope"""


def _empty(scope, status="ok"):
    return {"schema_version": 1, "scope": scope, "status": status,
            "files": [], "total_files": 0, "displayed_files": 0,
            "file_limit": MAX_FILES, "truncated": False, "warnings": []}


def _compatible(db):
    if not _REQUIRED.keys() <= _tables(db):
        return False
    for table, required in _REQUIRED.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not required <= columns:
            return False
    return True


def _version_fields(alias, prefix):
    # All returned strings are bounded metadata; raw_text/text/PDF/mark contents
    # are deliberately absent. COUNT(marks_json) counts non-NULL cells only.
    return f"""
        substr({alias}.source_id,1,256) AS {prefix}_source_id,
        substr({alias}.digest,1,128) AS {prefix}_digest,
        substr({alias}.status,1,64) AS {prefix}_status,
        substr({alias}.error,1,256) AS {prefix}_error,
        (SELECT COUNT(*) FROM knowledge_chunks c
         WHERE c.source_id={alias}.source_id) AS {prefix}_total_chunks,
        (SELECT COUNT(c.marks_json) FROM knowledge_chunks c
         WHERE c.source_id={alias}.source_id) AS {prefix}_labelled_chunks"""


_ROWS = f"""WITH paths AS ({_PATHS}), selected AS (
    SELECT path FROM paths ORDER BY path LIMIT :limit)
    SELECT substr(s.path,1,2048) AS path,length(s.path)>2048 AS path_truncated,
        d.source_id IS NOT NULL AS desired_present,
        p.source_id IS NOT NULL AS published_present,
        dv.source_id=pv.source_id AS same_version,
        {_version_fields("dv", "desired")},
        {_version_fields("pv", "published")}
    FROM selected s
    LEFT JOIN knowledge_desired d ON d.scope=:scope AND d.path=s.path
    LEFT JOIN knowledge_published p ON p.scope=:scope AND p.path=s.path
    LEFT JOIN knowledge_versions dv
        ON dv.source_id=d.source_id AND dv.scope=d.scope AND dv.path=d.path
    LEFT JOIN knowledge_versions pv
        ON pv.source_id=p.source_id AND pv.scope=p.scope AND pv.path=p.path
    ORDER BY s.path"""


def _version(row, prefix):
    if row[f"{prefix}_source_id"] is None:
        return None
    return {key: row[f"{prefix}_{key}"] for key in
            ("source_id", "digest", "status", "error", "total_chunks", "labelled_chunks")}


def _file(row):
    desired, published = _version(row, "desired"), _version(row, "published")
    warnings = []
    for name, version in (("desired", desired), ("published", published)):
        if row[f"{name}_present"] and version is None:
            warnings.append(f"{name}_pointer_invalid")
    retrievable = published is not None and published["status"] == "ready"
    if published is not None and not retrievable:
        warnings.append("published_not_ready")
    complete = (desired is not None and desired["status"] == "ready"
                and retrievable and bool(row["same_version"]))
    state = desired["status"] if desired else "published_only" if retrievable else "unavailable"
    if desired is not None and desired["status"] == "ready" and not complete:
        state = "ready_unpublished"
        warnings.append("ready_not_published")
    if warnings and desired is None and row["desired_present"]:
        state = "pointer_invalid"
    return {"path": row["path"], "path_truncated": bool(row["path_truncated"]),
            "desired": desired, "published": published, "state": state,
            "complete": bool(complete), "published_retrievable": retrievable,
            "warnings": warnings}


def progress_snapshot(config):
    """Return one short, consistent, scope-isolated, read-only SQLite snapshot."""
    guild_id = getattr(getattr(config, "discord", None), "guild_id", None)
    if not isinstance(guild_id, str) or not guild_id.strip():
        return _empty(None, "scope_unconfigured")
    scope = _scope(config)
    try:
        with _database(config) as db:
            if db is None:
                return _empty(scope, "database_missing")
            if not _compatible(db):
                return _empty(scope, "schema_unavailable")
            result = _empty(scope)
            parameters = {"scope": scope, "limit": MAX_FILES}
            result["total_files"] = db.execute(
                "SELECT COUNT(*) FROM (" + _PATHS + ")", parameters).fetchone()[0]
            result["files"] = [_file(row) for row in db.execute(_ROWS, parameters)]
            result["displayed_files"] = len(result["files"])
            result["truncated"] = result["total_files"] > result["displayed_files"]
            if any(item["warnings"] for item in result["files"]):
                result["warnings"].append("version_pointer_inconsistency")
            return result
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
        # Never disclose exception messages, database paths or partial results.
        return _empty(scope, "snapshot_unavailable")


def _terminal_text(value):
    """Escape terminal and directional controls, keeping every metadata field on one line."""
    text = str(value)
    escaped = []
    for char in text:
        if char in "\n\r\t":
            escaped.append({"\n": "\\n", "\r": "\\r", "\t": "\\t"}[char])
        elif unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
            escaped.append(f"\\u{ord(char):04x}" if ord(char) <= 0xffff else f"\\U{ord(char):08x}")
        else:
            escaped.append(char)
    return "".join(escaped)


_STATES = {
    "ready": "标词已完成",
    "ready_unpublished": "标词已完成，尚未发布",
    "failed": "失败",
    "pending": "待标词",
    "labelling": "标词中",
    "conversion_pending": "待转换",
    "converting": "待转换/转换中",
    "published_only": "仅有已发布版本",
    "pointer_invalid": "版本指针无效",
    "unavailable": "版本不可用",
    "archived": "已归档",
    "superseded": "已替代",
}
_WARNING_MESSAGES = {
    "desired_pointer_invalid": "最新版本指针与当前 Guild/路径不一致或缺失；未展示该版本。",
    "published_pointer_invalid": "发布指针与当前 Guild/路径不一致或缺失；未展示该版本。",
    "published_not_ready": "发布指针未指向 ready 版本，未确认可检索。",
    "ready_not_published": "最新 ready 版本尚未与发布指针一致。",
}


def _version_line(label, version):
    if version is None:
        return f"  {label}：无"
    status = _STATES.get(version["status"], "未知状态") if version["status"] else "未知状态"
    line = (f"  {label}：{_terminal_text(version['source_id'])}；"
            f"状态={status}；标词块={version['labelled_chunks']}/{version['total_chunks']}；"
            f"digest={_terminal_text(version['digest'])}")
    if version["error"] is not None:
        line += f"；失败/中断 code={_terminal_text(version['error'])}"
    return line


def _render(snapshot):
    lines = ["Indeces 知识库当前版本状态（只读）",
             "停服后仍显示最近保存的状态；分块完成不等于整篇发布完成。",
             f"Guild scope：{_terminal_text(snapshot['scope']) if snapshot['scope'] else '未配置'}"]
    if snapshot["status"] != "ok":
        lines.append(_MESSAGES[snapshot["status"]])
        return "\n".join(lines)
    lines.append(f"记录总文件数：{snapshot['total_files']}；显示：{snapshot['displayed_files']}；"
                 f"视图上限：{snapshot['file_limit']}")
    if snapshot["truncated"]:
        lines.append(f"视图已截断：另有 {snapshot['total_files'] - snapshot['displayed_files']} 个路径未显示。")
    states = Counter("已发布完成" if item["complete"] else _STATES.get(item["state"], "未知状态")
                     for item in snapshot["files"])
    if states:
        lines.append("显示范围状态：" + "；".join(f"{name}={count}" for name, count in sorted(states.items())))
    else:
        lines.append("当前 Guild 尚无待处理或已发布文件记录。")
    for item in snapshot["files"]:
        path = _terminal_text(item["path"]) + (" [路径已截断]" if item["path_truncated"] else "")
        state = "已发布完成" if item["complete"] else _STATES.get(item["state"], "未知状态")
        lines.append(f"- {path} | {state}")
        lines.append(_version_line("最新版本", item["desired"]))
        label = "可检索发布版本" if item["published_retrievable"] else "发布指针（未确认可检索）"
        lines.append(_version_line(label, item["published"]))
        for warning in item["warnings"]:
            lines.append("  提示：" + _WARNING_MESSAGES[warning])
    return "\n".join(lines)


def show_knowledge_progress(config, *, watch=True, refresh_seconds=2.0):
    """Print a current metadata view, polling on changes until Ctrl+C or once."""
    if (type(refresh_seconds) not in (int, float) or not math.isfinite(refresh_seconds)
            or refresh_seconds <= 0):
        raise ValueError("刷新间隔必须是大于零的有限秒数。")
    previous = None
    try:
        while True:
            current = progress_snapshot(config)
            if current != previous:
                print(_render(current), flush=True)
                previous = current
            if not watch:
                return
            # progress_snapshot has left its context and closed SQLite already.
            time.sleep(refresh_seconds)
    except KeyboardInterrupt:
        print("已退出只读进度视图；未向服务发送停止请求。", flush=True)
