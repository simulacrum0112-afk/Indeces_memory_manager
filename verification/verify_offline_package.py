"""Verify an isolated wheel's bytes, CLI help and Console version without service startup."""
from __future__ import annotations

import argparse
import base64
from collections import deque
from contextlib import ExitStack, redirect_stdout
import csv
import hashlib
from importlib import metadata
import io
import json
from pathlib import Path
import queue
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.package_root.resolve()
    expected = json.loads(args.source_receipt.read_text(encoding="utf-8"))["source_sha256"]
    sys.path.insert(0, str(root))
    import indeces
    from indeces import console, console_session
    if Path(indeces.__file__).resolve().parent != root / "indeces":
        raise AssertionError("wrong package imported")
    for name, identity in expected.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != identity:
            raise AssertionError("source mismatch: " + name)
    distributions = [d for d in metadata.distributions(path=[str(root)])
                     if d.metadata["Name"].replace("-", "_").lower() == "indeces_memory_manager"]
    if len(distributions) != 1 or distributions[0].version != indeces.__version__:
        raise AssertionError("installed metadata/version mismatch")
    record = next(root.glob("indeces_memory_manager-*.dist-info/RECORD"))
    checked, relocated_scripts = 0, []
    for name, encoded, size in csv.reader(io.StringIO(record.read_text(encoding="utf-8"))):
        if not encoded:
            continue
        algorithm, value = encoded.split("=", 1)
        if algorithm != "sha256":
            raise AssertionError("unsupported RECORD algorithm")
        target = (root / name).resolve()
        if not target.is_relative_to(root):
            # pip --target moves its generated Windows launchers into bin/,
            # while RECORD retains their temporary installation coordinates.
            if name not in {"../../bin/indeces.exe", "../../bin/indeces-manual.exe"}:
                raise AssertionError("RECORD path escapes installed root")
            target = root / "bin" / Path(name).name
            relocated_scripts.append({"declared": name, "verified": str(target.relative_to(root))})
        data = target.read_bytes()
        if (base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode() != value
                or len(data) != int(size)):
            raise AssertionError("RECORD mismatch: " + name)
        checked += 1
    help_output, banner_output = io.StringIO(), io.StringIO()
    with ExitStack() as guards:
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection",
                       "aiohttp.ClientSession"):
            guards.enter_context(patch(target, side_effect=AssertionError("external transport forbidden")))
        with patch.object(sys, "argv", ["indeces", "--help"]), redirect_stdout(help_output):
            try:
                console.main()
            except SystemExit as result:
                if result.code != 0:
                    raise
        # Execute the real run() banner with a synthetic immediate-quit reader.
        # No constructor, IPC owner, service, configuration or lease is created.
        session = console_session.ConsoleSession.__new__(console_session.ConsoleSession)
        session._install_output = Mock()
        session._safe_dispatch = Mock(side_effect=[True, False])
        session.instance = SimpleNamespace(commands=queue.Queue())
        session._pending = deque()
        session.reader = SimpleNamespace(poll=lambda: "quit", busy=False)
        session.close = Mock()
        with redirect_stdout(banner_output):
            session.run()
        session.close.assert_called_once()
    if not help_output.getvalue().startswith("usage:"):
        raise AssertionError("CLI help missing")
    if not banner_output.getvalue().startswith(f"Indeces {indeces.__version__} console:"):
        raise AssertionError("Console banner/version mismatch")
    receipt = {"schema": "offline_package_verification_v1", "version": indeces.__version__,
               "loaded_package_verified": True, "application_files_checked": len(expected),
               "record_files_checked": checked, "cli_help_exit_code": 0,
               "record_script_relocations": relocated_scripts,
               "console_banner_version_verified": True, "console_lifecycle": "synthetic_immediate_quit",
               "service_started": False, "credentials_loaded": False, "production_data_read": False,
               "real_network_or_model_calls": 0}
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
