"""Build and check a non-editable wheel without modifying the runtime venv.

Only tracked public files are copied. Build requirements use pip's temporary
isolated environment; no model, Discord, or user configuration is accessed.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def checked(command, *, cwd, env, stdin=None):
    result = subprocess.run(command, cwd=cwd, env=env, input=stdin, text=True,
                            encoding="utf-8", capture_output=True, timeout=180)
    if result.returncode:
        raise RuntimeError(f"Verification command failed (exit {result.returncode}): {command[2]}")
    return result.stdout.strip()


def main():
    sys.path.insert(0, str(ROOT))
    from indeces import __version__

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    with tempfile.TemporaryDirectory(prefix="indeces-wheel-check-") as directory:
        base = Path(directory)
        source = base / "source"
        source.mkdir()
        tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
        for name in filter(None, tracked):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        wheels = base / "wheels"
        wheels.mkdir()
        checked([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheels), str(source)],
                cwd=base, env=env)
        wheel, = wheels.glob("*.whl")
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            assets = ["indeces/web/observer." + suffix for suffix in ("html", "css", "js")]
            assert all(name in names for name in assets)
        site = base / "installed"
        checked([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(site), str(wheel)],
                cwd=base, env=env)
        env["PYTHONPATH"] = str(site)
        probe = "\n".join([
            "from pathlib import Path",
            "import indeces",
            f"assert indeces.__version__ == {__version__!r}",
            f"assert Path(indeces.__file__).is_relative_to(Path({str(site)!r}))",
            "from indeces.memory import MemoryGraph",
            "import inspect",
            "assert inspect.signature(MemoryGraph.retrieve).parameters['ranking_mode'].default == 'static'",
            "from indeces.observer_data import npmi_diagnostics",
            "print('Installed import, static retrieval default and diagnostic entry point: passed')",
        ])
        imported = checked([sys.executable, "-c", probe], cwd=base, env=env)
        config = base / "config.example.toml"
        shutil.copyfile(source / "config.example.toml", config)
        cli_checks = []
        for command in ("check", "npmi"):
            output = checked([sys.executable, "-m", "indeces", command, "--config", str(config)],
                             cwd=base, env=env)
            cli_checks.append({"command": command, "exit_code": 0, "output": output})
        report = {
            "version": __version__, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "platform": platform.platform(), "python_version": platform.python_version(),
            "build_requirements": "pip temporary build isolation; runtime venv unchanged",
            "source_scope": "Current contents of git-tracked public files only",
            "wheel": wheel.name, "non_editable_install": True, "packaged_assets": assets,
            "installed_import_check": imported, "cli_checks": cli_checks,
            "live_model_calls": False, "live_discord_connection": False,
            "private_config_or_runtime_read": False, "passed": True,
        }
    path = ROOT / "verification" / ("WHEEL_" + __version__.replace(".", "") + ".json")
    if path.exists():
        path = path.with_name(path.stem + "_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + path.suffix)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
