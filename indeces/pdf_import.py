"""Bounded local PDF text extraction, isolated from Discord and model calls.

Markdown is an extraction draft, not a reconstruction of academic layout.
All offsets refer to Unicode characters and PDF physical page numbers.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile

from .config import PdfConfig
from .contracts import GovernedError


EXTRACTOR_VERSION = "6.19.0"
POLICY_VERSION = "indeces-pdf-v1"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_WARNINGS = {"pdf_text_reading_order_unverified", "pdf_math_tables_figures_not_reconstructed",
             "pdf_parser_reported_warnings", "pdf_replacement_characters_present"}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_conversion(markdown, metadata, pdf_digest=None):
    """Validate extraction provenance and page spans without parsing the PDF again."""
    def require(condition):
        if not condition:
            raise ValueError("invalid PDF conversion metadata")
    require(isinstance(markdown, str) and isinstance(metadata, dict))
    require(set(metadata) == {"schema_version", "original_pdf_sha256", "markdown_sha256",
                              "extractor", "page_count", "pages", "warnings"})
    require(type(metadata["schema_version"]) is int and metadata["schema_version"] == 1)
    require(isinstance(metadata["original_pdf_sha256"], str)
            and _HASH.fullmatch(metadata["original_pdf_sha256"]) is not None)
    require(pdf_digest is None or metadata["original_pdf_sha256"] == pdf_digest)
    require(metadata["markdown_sha256"] == _sha(markdown))
    require(metadata["extractor"] == {"name": "pypdf", "version": EXTRACTOR_VERSION,
                                       "policy_version": POLICY_VERSION, "mode": "plain"})
    count, pages, warnings = metadata["page_count"], metadata["pages"], metadata["warnings"]
    require(type(count) is int and 1 <= count <= 2000)
    require(isinstance(pages, list) and len(pages) == count)
    require(isinstance(warnings, list) and all(isinstance(w, str) for w in warnings))
    require(warnings == sorted(set(warnings)) and set(warnings) <= _WARNINGS)
    require({"pdf_text_reading_order_unverified", "pdf_math_tables_figures_not_reconstructed"} <= set(warnings))
    through = 0
    for number, page in enumerate(pages, 1):
        require(isinstance(page, dict) and set(page) == {
            "page_number", "start_character", "end_character", "text_sha256"})
        start, end = page["start_character"], page["end_character"]
        require(type(page["page_number"]) is int and page["page_number"] == number)
        require(type(start) is int and type(end) is int and start == through and start < end <= len(markdown))
        value = markdown[start:end]
        require(value.startswith(f"## PDF page {number}\n\n") and value.endswith("\n\n"))
        require(page["text_sha256"] == _sha(value))
        through = end
    require(through == len(markdown))


def page_occurrences(chunks, text, metadata):
    """Report all matching chunk locations; repeated text has no unique page claim."""
    result = []
    for chunk in chunks:
        if chunk["text"] != text:
            continue
        start, end = chunk["start_character"], chunk["start_character"] + len(text)
        pages = [page["page_number"] for page in metadata["pages"]
                 if start < page["end_character"] and end > page["start_character"]]
        result.append({"chunk_index": chunk["chunk_index"], "start_character": start,
                       "end_character": end, "page_numbers": pages})
    return result


class _WarningCounter(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record):
        # Arbitrary parser text can contain document contents; never retain it.
        self.count += 1


def _extract(raw, limits):
    """Only run in the supervised worker process, never in the event loop."""
    import pypdf
    if pypdf.__version__ != EXTRACTOR_VERSION:
        raise GovernedError("pdf_extractor_version_mismatch")
    if not raw.lstrip().startswith(b"%PDF-"):
        raise GovernedError("pdf_invalid")
    if len(raw) > limits.max_file_bytes:
        raise GovernedError("pdf_file_size_limit")
    counter = _WarningCounter()
    logger = logging.getLogger("pypdf")
    logger.addHandler(counter)
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise GovernedError("pdf_encrypted")
        count = len(reader.pages)
        if not 1 <= count <= limits.max_pages:
            raise GovernedError("pdf_page_limit")
        parts, spans, characters, output_bytes = [], [], 0, 0
        warnings = {"pdf_text_reading_order_unverified", "pdf_math_tables_figures_not_reconstructed"}
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text(extraction_mode="plain") or ""
            text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
            if not text or len("".join(text.split())) < 20:
                # No OCR and no guessing that an unreadable page is blank.
                raise GovernedError("pdf_needs_ocr_or_review")
            if "\x00" in text:
                raise GovernedError("pdf_invalid_text")
            if "\ufffd" in text:
                warnings.add("pdf_replacement_characters_present")
            part = f"## PDF page {number}\n\n{text}\n\n"
            output_bytes += len(part.encode("utf-8"))
            if output_bytes > limits.max_markdown_bytes:
                raise GovernedError("pdf_output_limit")
            spans.append({"page_number": number, "start_character": characters,
                          "end_character": characters + len(part), "text_sha256": _sha(part)})
            characters += len(part)
            parts.append(part)
        if counter.count:
            warnings.add("pdf_parser_reported_warnings")
        markdown = "".join(parts)
        metadata = {"schema_version": 1, "original_pdf_sha256": hashlib.sha256(raw).hexdigest(),
                    "markdown_sha256": _sha(markdown),
                    "extractor": {"name": "pypdf", "version": EXTRACTOR_VERSION,
                                  "policy_version": POLICY_VERSION, "mode": "plain"},
                    "page_count": count, "pages": spans, "warnings": sorted(warnings)}
        validate_conversion(markdown, metadata)
        return {"markdown": markdown, "metadata": metadata}
    except GovernedError:
        raise
    except Exception:
        raise GovernedError("pdf_invalid") from None
    finally:
        logger.removeHandler(counter)


def _worker(input_path, output_path, limits_json):
    try:
        limits = PdfConfig(**json.loads(limits_json))
        with Path(input_path).open("rb") as stream:
            raw = stream.read(limits.max_file_bytes + 1)
        result = {"ok": True, **_extract(raw, limits)}
    except GovernedError as error:
        result = {"ok": False, "code": error.code}
    except Exception:
        result = {"ok": False, "code": "pdf_worker_failure"}
    Path(output_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


async def convert_pdf(raw: bytes, limits: PdfConfig):
    """One child process; timeout/cancellation kills and reaps it, with no retry."""
    if not isinstance(raw, bytes):
        raise TypeError("PDF input must be bytes")
    if len(raw) > limits.max_file_bytes:
        raise GovernedError("pdf_file_size_limit")
    process = None
    creation = None
    with tempfile.TemporaryDirectory(prefix="indeces-pdf-") as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to(Path(tempfile.gettempdir()).resolve()):
            raise GovernedError("pdf_temporary_path_invalid")
        input_path, output_path = root / "source.pdf", root / "result.json"
        input_path.write_bytes(raw)
        try:
            async with asyncio.timeout(limits.seconds):
                creation = asyncio.create_task(asyncio.create_subprocess_exec(
                    sys.executable, "-m", "indeces.pdf_import", "--worker", str(input_path), str(output_path),
                    json.dumps(asdict(limits)), stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL, **({"creationflags": 0x08000000} if os.name == "nt" else {})))
                # Preserve the process handle if cancellation arrives during spawn.
                process = await asyncio.shield(creation)
                await process.wait()
            if process.returncode != 0 or not output_path.is_file():
                raise GovernedError("pdf_worker_failure")
            # JSON escapes may expand text. Bound the transport independently.
            wire_limit = limits.max_markdown_bytes * 6 + limits.max_pages * 400 + 8192
            with output_path.open("rb") as stream:
                wire = stream.read(wire_limit + 1)
            if len(wire) > wire_limit:
                raise GovernedError("pdf_output_limit")
            result = json.loads(wire)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise GovernedError("pdf_worker_failure")
            if result.get("ok") is not True:
                code = result.get("code")
                if code not in {"pdf_invalid", "pdf_encrypted", "pdf_needs_ocr_or_review", "pdf_invalid_text",
                                "pdf_page_limit", "pdf_output_limit", "pdf_file_size_limit",
                                "pdf_extractor_version_mismatch", "pdf_worker_failure"}:
                    code = "pdf_worker_failure"
                raise GovernedError(code)
            if set(result) != {"ok", "markdown", "metadata"}:
                raise GovernedError("pdf_worker_failure")
            markdown, metadata = result["markdown"], result["metadata"]
            if not isinstance(markdown, str) or not isinstance(metadata, dict):
                raise GovernedError("pdf_worker_failure")
            if len(markdown.encode("utf-8")) > limits.max_markdown_bytes or metadata["page_count"] > limits.max_pages:
                raise GovernedError("pdf_output_limit")
            validate_conversion(markdown, metadata, hashlib.sha256(raw).hexdigest())
            return {"markdown": markdown, "metadata": metadata}
        except TimeoutError:
            raise GovernedError("pdf_time_limit") from None
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            raise GovernedError("pdf_worker_failure") from None
        finally:
            async def reap():
                child = process
                if child is None and creation is not None:
                    try:
                        child = await creation
                    except OSError:
                        return
                if child is not None and child.returncode is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                    await child.wait()
            if process is not None or creation is not None:
                # Supervision and shutdown may each cancel this same job. Keep
                # the handle acquisition and reaping alive through both, before
                # removing its temporary inputs or reporting cancellation.
                cleanup = asyncio.create_task(reap())
                cancelled_again = False
                while True:
                    try:
                        await asyncio.shield(cleanup)
                        break
                    except asyncio.CancelledError:
                        cancelled_again = True
                        if cleanup.done():
                            cleanup.result()
                            break
                if cancelled_again:
                    raise asyncio.CancelledError()


def publish_markdown(state_dir, source_id, markdown):
    """Write a version-specific review file outside knowledge; never overwrite notes."""
    if not isinstance(source_id, str) or not re.fullmatch(r"kb:[0-9a-f]{32}", source_id):
        raise ValueError("invalid PDF source ID")
    from .path_policy import PathPolicyError, validate_managed_path
    try:
        base = validate_managed_path(state_dir, "PDF export state")
        validate_managed_path(base / "pdf_markdown", "PDF export directory")
    except PathPolicyError:
        raise GovernedError("pdf_export_path_invalid") from None
    directory = base / "pdf_markdown"
    if directory.is_symlink():
        raise GovernedError("pdf_export_path_invalid")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.resolve() != directory:
        raise GovernedError("pdf_export_path_invalid")
    target = directory / (source_id.removeprefix("kb:") + ".md")
    try:
        validate_managed_path(target, "PDF export file")
    except PathPolicyError:
        raise GovernedError("pdf_export_path_invalid") from None
    desired = markdown.encode("utf-8")
    if target.is_symlink():
        raise GovernedError("pdf_export_path_invalid")
    if target.exists():
        if not target.is_file() or target.stat().st_size != len(desired) or target.read_bytes() != desired:
            raise GovernedError("pdf_export_conflict")
        return target
    descriptor, name = tempfile.mkstemp(prefix=".pdf-md-", suffix=".tmp", dir=directory)
    temporary = Path(name)
    owned = True
    try:
        output = os.fdopen(descriptor, "wb")
        owned = False
        with output:
            output.write(desired)
            output.flush()
            os.fsync(output.fileno())
        # Hard-link publication is atomic and refuses an existing target.
        # The lease prevents another runtime; still preserve a concurrent user edit.
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or target.read_bytes() != desired:
                raise GovernedError("pdf_export_conflict") from None
        return target
    finally:
        if owned:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "--worker":
        raise SystemExit("This module is an internal PDF worker. Use the Indeces Console.")
    _worker(*sys.argv[2:])
