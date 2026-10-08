"""Build/install a wheel in a fresh directory using existing local build wheels only."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source-receipt", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_root.resolve()
    if output.exists():
        raise SystemExit("output-root must be fresh; previous artifacts are preserved")
    wheelhouse = args.wheelhouse.resolve()
    if not wheelhouse.is_dir() or not list(wheelhouse.glob("setuptools-*.whl")):
        raise SystemExit("local wheelhouse needs a setuptools>=75 wheel")
    expected = json.loads(args.source_receipt.read_text(encoding="utf-8"))["source_sha256"]
    for name, identity in expected.items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != identity:
            raise SystemExit("source no longer matches tested receipt: " + name)
    output.mkdir(parents=True)
    source, tools, wheels, installed, guard = (output / n for n in ("SOURCE", "TOOLS", "WHEELS", "INSTALLED", "GUARD"))
    source.mkdir(); wheels.mkdir(); guard.mkdir()
    for path in (ROOT / "indeces").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".html", ".css", ".js"}:
            target = source / path.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    for name in ("pyproject.toml", "LICENSE", "README.md"):
        shutil.copyfile(ROOT / name, source / name)
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "def denied(*args, **kwargs):\n    raise RuntimeError('Offline build: network denied')\n"
        "socket.socket.connect = denied\nsocket.socket.connect_ex = denied\nsocket.create_connection = denied\n",
        encoding="utf-8")
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(guard), str(tools))),
                       PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    commands = []
    def run(name, arguments, cwd=source):
        command = [sys.executable, "-X", "utf8", "-B", "-m", "pip", "--isolated", "--disable-pip-version-check", *arguments]
        result = subprocess.run(command, cwd=cwd, env=environment, capture_output=True, text=True, encoding="utf-8")
        (output / (name + ".log")).write_text(result.stdout + result.stderr, encoding="utf-8")
        commands.append({"stage": name, "command": command, "exit_code": result.returncode})
        (output / "COMMANDS.json").write_text(json.dumps(commands, indent=2) + "\n", encoding="utf-8")
        if result.returncode:
            raise SystemExit(name + " failed; see preserved log")
    run("build_tools", ["install", "--no-index", "--no-deps", "--no-compile", "--find-links", str(wheelhouse),
                        "--target", str(tools), "setuptools>=75"])
    run("wheel_build", ["wheel", "--no-index", "--no-deps", "--no-build-isolation", "--wheel-dir", str(wheels), str(source)])
    artifacts = list(wheels.glob("indeces_memory_manager-*.whl"))
    if len(artifacts) != 1:
        raise SystemExit("expected one application wheel")
    run("wheel_install", ["install", "--no-index", "--no-deps", "--no-compile", "--target", str(installed), str(artifacts[0])])
    receipt = {"schema": "offline_wheel_build_v1", "network_allowed": False,
               "build_dependencies_from": "explicit_local_wheelhouse", "commands": commands,
               "wheel": artifacts[0].name, "wheel_sha256": hashlib.sha256(artifacts[0].read_bytes()).hexdigest(),
               "application_files": len(expected), "installed_path": str(installed),
               "production_installation_changed": False}
    (output / "BUILD.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
