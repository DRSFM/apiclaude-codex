"""Discover official macOS Codex bundles and isolate their launch environment."""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path


def bundle_executable(bundle: Path) -> Path | None:
    """The newer ChatGPT.app retains Codex's bundle ID; Classic does not."""
    try:
        info = plistlib.loads((bundle / "Contents" / "Info.plist").read_bytes())
        if not isinstance(info, dict) or info.get("CFBundleIdentifier") != "com.openai.codex":
            return None
        name = info.get("CFBundleExecutable")
        if not isinstance(name, str) or not name or Path(name).name != name:
            return None
        executable = bundle / "Contents" / "MacOS" / name
        return executable if executable.is_file() and os.access(executable, os.X_OK) else None
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None


def find_executable(home: Path) -> Path | None:
    for directory in (home / "Applications", Path("/Applications")):
        for name in ("ChatGPT.app", "Codex.app"):
            executable = bundle_executable(directory / name)
            if executable is not None:
                return executable
    return None


def environment_remove() -> tuple[str, ...]:
    # A launcher running inside another Desktop inherits its CLI, sockets,
    # SQLite directory and auth. Only explicitly supplied profile values survive.
    keys = {"ELECTRON_RUN_AS_NODE", "NODE_OPTIONS"}
    keys.update(key for key in os.environ if key.upper().startswith(
        ("CODEX_", "APICODEX_", "OPENAI_", "ELECTRON_")
    ))
    return tuple(sorted(keys))


def activate_process(pid: int) -> bool:
    """Focus this instance without activating another profile's bundle window."""
    script = (
        'ObjC.import("AppKit"); function run(argv) {'
        'const app = $.NSRunningApplication.runningApplicationWithProcessIdentifier(Number(argv[0]));'
        'return app.isNil() ? false : app.activateWithOptions($.NSApplicationActivateIgnoringOtherApps); }'
    )
    try:
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script, str(pid)],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False
