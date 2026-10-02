"""Limited publication audit of main history and the exact Git index.

No worktree source, untracked attachment, runtime data, or tool ref is read.
Findings contain locations/classifications only, never matched content.
This complements human review; it is not an exhaustive secret detector.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATTERN = re.compile(r"verification/REPOSITORY_AUDIT_[A-Za-z0-9_]+\.json\Z")
RULES = {
    "provider_key_shape": r"(?<![A-Za-z0-9_-])sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}",
    "github_token_shape": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b",
    "private_key_header": r"-----BEGIN(?: RSA| DSA| EC| OPENSSH| ENCRYPTED)? PRIVATE KEY-----",
    "discord_token_shape": r"(?:mfa\.[A-Za-z0-9_-]{80,}|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,})",
}
COMPILED = {name: re.compile(pattern) for name, pattern in RULES.items()}
QUALIFIED = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){2,})(?![A-Za-z0-9_.])")


class AuditError(Exception):
    pass


def git(*arguments, data=None):
    result = subprocess.run(["git", "-C", str(ROOT), *arguments], input=data,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise AuditError("git_public_scope_read_failed")
    return result.stdout


def tree_entries(commit):
    for item in git("ls-tree", "-r", "-z", commit).split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, kind, blob = metadata.decode("ascii").split()
        yield path.decode("utf-8", "surrogateescape"), blob, mode, kind


def index_entries():
    for item in git("ls-files", "--stage", "-z").split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, blob, stage = metadata.decode("ascii").split()
        if stage != "0":
            raise AuditError("unmerged_public_index")
        yield path.decode("utf-8", "surrogateescape"), blob, mode, "commit" if mode == "160000" else "blob"


def object_sizes(blobs):
    ordered = sorted(blobs)
    if not ordered:
        return {}
    output = git("cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                 data=("\n".join(ordered) + "\n").encode("ascii"))
    sizes = {}
    for line in output.decode("ascii").splitlines():
        blob, kind, size = line.split()
        if kind != "blob":
            raise AuditError("unexpected_public_object_type")
        sizes[blob] = int(size)
    return sizes


def read_blobs(blobs):
    ordered = sorted(blobs)
    if not ordered:
        return {}
    output = git("cat-file", "--batch", data=("\n".join(ordered) + "\n").encode("ascii"))
    contents, offset = {}, 0
    for expected in ordered:
        end = output.index(b"\n", offset)
        blob, kind, size = output[offset:end].decode("ascii").split()
        if blob != expected or kind != "blob":
            raise AuditError("unexpected_public_batch_object")
        offset = end + 1
        size = int(size)
        contents[blob] = output[offset:offset + size]
        offset += size + 1
    return contents


def private_path(path):
    parts = [part.casefold() for part in PurePosixPath(path).parts]
    name = parts[-1] if parts else ""
    if any(part in {"assets", "state", "scratch", "knowledge", "_staging", "secret", "secrets"} for part in parts):
        return True
    if (name == ".env" or (name.startswith(".env.") and name != ".env.example")
            or name.startswith("config.local.") or name == "config.local"):
        return True
    return PurePosixPath(name).suffix in {".sqlite3", ".sqlite", ".db", ".secret", ".jsonl"}


def prior_records(value):
    if isinstance(value, dict):
        if all(key in value for key in ("blob", "path", "line", "rule", "classification")):
            yield value
        for child in value.values():
            yield from prior_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from prior_records(child)


def normalized_digest(content):
    return hashlib.sha256(content.replace(b"\r\n", b"\n")).hexdigest()


def prior_exceptions(entries, contents):
    exact, equivalent = {}, {}
    public_pairs = {(path, blob) for path, blob, _, _ in entries}
    for path, blob, _, _ in sorted(entries):
        if not REPORT_PATTERN.fullmatch(path) or blob not in contents:
            continue
        try:
            metadata = json.loads(contents[blob])
        except (ValueError, UnicodeError):
            raise AuditError("invalid_prior_public_audit_metadata") from None
        for item in prior_records(metadata):
            classification = item["classification"]
            if not isinstance(classification, str) or not classification.startswith("confirmed_"):
                continue
            candidate = (item["path"], item["blob"])
            if candidate not in public_pairs or item["blob"] not in contents:
                continue
            if type(item["line"]) is not int or item["line"] < 1 or item["rule"] not in RULES:
                continue
            key = (item["blob"], item["path"], item["line"], item["rule"])
            exact[key] = classification
            norm_key = (normalized_digest(contents[item["blob"]]), item["path"], item["line"], item["rule"])
            equivalent[norm_key] = classification
    return exact, equivalent


def unittest_identifiers(index, contents):
    identifiers = set()
    for path, blob, _, kind in index:
        if kind != "blob" or not path.startswith("tests/") or not path.endswith(".py") or blob not in contents:
            continue
        try:
            tree = ast.parse(contents[blob], filename=path)
        except (SyntaxError, ValueError, UnicodeError):
            continue
        module_aliases, case_aliases = set(), set()
        case_types = {"TestCase", "IsolatedAsyncioTestCase"}
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "unittest")
            elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
                case_aliases.update(alias.asname or alias.name for alias in node.names if alias.name in case_types)
        classes = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        proven = set()
        while True:
            before = len(proven)
            for name, node in classes.items():
                imported_base = any(isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
                                    and base.value.id in module_aliases and base.attr in case_types for base in node.bases)
                inherited_base = any(isinstance(base, ast.Name) and base.id in case_aliases | proven for base in node.bases)
                if imported_base or inherited_base:
                    proven.add(name)
            if len(proven) == before:
                break
        module = path[:-3].replace("/", ".")
        # unittest discover -s tests imports modules without the tests. prefix;
        # package-based invocation retains it. Both refer to this exact AST.
        module_spellings = {module, module.removeprefix("tests.")}
        for name in proven:
            for node in classes[name].body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                    identifiers.update(f"{spelling}.{name}.{node.name}" for spelling in module_spellings)
    return identifiers


def audit():
    main = git("rev-parse", "--verify", "refs/heads/main^{commit}").decode("ascii").strip()
    commits = git("rev-list", main).decode("ascii").splitlines()
    history = set()
    for commit in commits:
        history.update(tree_entries(commit))
    index = set(index_entries())
    entries = history | index
    sizes = object_sizes({blob for _, blob, _, kind in entries if kind == "blob"})
    prohibited, sentinels, permitted = [], [], set()
    for path, blob, mode, kind in sorted(entries):
        if path == "knowledge/.gitkeep" and kind == "blob" and mode in {"100644", "100755"} and sizes[blob] == 0:
            sentinels.append({"path": path, "blob": blob, "bytes": 0})
        elif private_path(path):
            prohibited.append({"rule": "private_runtime_path", "path": path, "line": 0, "blob": blob,
                               "classification": "prohibited_public_path"})
        elif kind != "blob":
            prohibited.append({"rule": "unsupported_public_object", "path": path, "line": 0, "blob": blob,
                               "classification": "requires_publication_review"})
        else:
            permitted.add((path, blob, mode, kind))
    # A forbidden object's bytes are never read, even if another path aliases it.
    denied_blobs = {item["blob"] for item in prohibited}
    permitted = {entry for entry in permitted if entry[1] not in denied_blobs}
    contents = read_blobs({blob for _, blob, _, _ in permitted})
    exact, equivalent = prior_exceptions(permitted, contents)
    identifiers = unittest_identifiers(index, contents)
    findings, metadata_excluded = [], []
    for path, blob, _, _ in sorted(permitted):
        if REPORT_PATTERN.fullmatch(path):
            metadata_excluded.append({"path": path, "blob": blob})
            continue
        text = contents[blob].decode("utf-8", "replace")
        norm_digest = normalized_digest(contents[blob])
        known_identifiers = [(match.start(), match.end()) for match in QUALIFIED.finditer(text)
                             if match.group() in identifiers]
        for rule, pattern in COMPILED.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                classification = exact.get((blob, path, line, rule))
                if classification is None:
                    prior = equivalent.get((norm_digest, path, line, rule))
                    if prior is not None:
                        classification = prior + "_lf_crlf_equivalent"
                if (classification is None and rule == "discord_token_shape"
                        and any(start <= match.start() and match.end() <= end for start, end in known_identifiers)):
                    classification = "confirmed_unittest_case_identifier"
                findings.append({"rule": rule, "path": path, "line": line, "blob": blob,
                                 "classification": classification or "unknown_requires_review"})
    unknown = [item for item in findings if item["classification"] == "unknown_requires_review"]
    return {
        "version": "0.12.5",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_main": main,
        "main_reachable_commits": len(commits),
        "main_reachable_unique_blobs_excluding_empty_sentinel": len({blob for _, blob, _, _ in history if sizes.get(blob, 0)}),
        "current_public_paths_enumerated": len(index),
        "current_public_files_scanned": sum(blob in contents and not REPORT_PATTERN.fullmatch(path)
                                             for path, blob, _, _ in index),
        "scope": "Reachable main trees and exact public Git index blobs only. No worktree or untracked content, tool refs, runtime data, release assets, CI logs, commit messages or tag annotations.",
        "rule_definitions": {**RULES, "private_runtime_path": "assets/state/scratch/knowledge/_staging/secret/secrets path components; config.local and .env variants; .sqlite3/.sqlite/.db/.secret/.jsonl. Only empty knowledge/.gitkeep allowed without content reads."},
        "classification_policy": "Exact previously confirmed blob/path/line/rule; verified LF/CRLF-equivalent bytes; or exact qualified current unittest AST method identifier. No blanket tests exemption.",
        "scanned_files": [{"path": path, "blob": blob} for path, blob, _, _ in sorted(index) if blob in contents],
        "findings": findings,
        "unknown_findings": unknown,
        "prohibited_paths": prohibited,
        "empty_runtime_sentinels": sentinels,
        "metadata_files_excluded_from_recursive_scan": metadata_excluded,
        "unknown_findings_count": len(unknown),
        "prohibited_paths_count": len(prohibited),
        "passed": not unknown and not prohibited,
        "limitations": "Finite token shapes cannot establish absence of all credentials or personal information; publication review remains necessary. Audit metadata is excluded from recursive pattern matching.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="verification/REPOSITORY_AUDIT_0125.json")
    parser.add_argument("--no-write", action="store_true", help="report counts and findings without saving a new report")
    arguments = parser.parse_args()
    if not REPORT_PATTERN.fullmatch(arguments.output):
        parser.error("output must be a new verification/REPOSITORY_AUDIT_*.json metadata file")
    destination = ROOT / arguments.output
    try:
        report = audit()
        if not arguments.no_write:
            # Never overwrite an existing evidence report or follow its link.
            with destination.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(report, stream, ensure_ascii=True, indent=2)
                stream.write("\n")
        print(json.dumps({"passed": report["passed"], "main_reachable_commits": report["main_reachable_commits"],
                          "current_public_paths_enumerated": report["current_public_paths_enumerated"],
                          "current_public_files_scanned": report["current_public_files_scanned"],
                          "unknown_findings_count": report["unknown_findings_count"],
                          "prohibited_paths_count": report["prohibited_paths_count"],
                          "report": None if arguments.no_write else arguments.output}, ensure_ascii=True))
        for finding in report["unknown_findings"] + report["prohibited_paths"]:
            print(json.dumps(finding, ensure_ascii=True))
        return 0 if report["passed"] else 1
    except (AuditError, OSError, ValueError, KeyError) as error:
        print(json.dumps({"passed": False, "error": str(error) if isinstance(error, AuditError) else type(error).__name__}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
