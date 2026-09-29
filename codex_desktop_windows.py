"""Windows helpers for launching and labeling isolated Codex Desktop instances."""

from __future__ import annotations

import os
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


_PACKAGE_LAUNCH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$executable = [IO.Path]::GetFullPath($env:APICODEX_PACKAGE_EXECUTABLE)
$package = Get-AppxPackage -Name OpenAI.Codex -ErrorAction Stop |
  Where-Object {
    $candidate = Join-Path $_.InstallLocation 'app\ChatGPT.exe'
    [IO.Path]::GetFullPath($candidate).Equals($executable, [StringComparison]::OrdinalIgnoreCase)
  } | Select-Object -First 1
if (-not $package) { throw 'The selected Desktop executable is not registered.' }
Invoke-CommandInDesktopPackage -PackageFamilyName $package.PackageFamilyName -AppId 'App' `
  -Command $env:APICODEX_PACKAGE_HELPER -Args $env:APICODEX_PACKAGE_HELPER_ARGS `
  -PreventBreakaway -ErrorAction Stop
"""


def needs_package_identity(executable: Path) -> bool:
    """Leave unpackaged APICODEX_DESKTOP_EXE overrides on the existing path."""
    return (os.name == "nt" and executable.name.lower() == "chatgpt.exe"
            and (executable.parent.parent / "AppxManifest.xml").is_file())


def start_packaged_codex_desktop(
    executable: Path, args: list[str], environment: dict[str, str],
) -> int:
    result = _start_in_package(executable, [str(executable), *args], environment)
    if result == 0 and not ensure_desktop_tray():
        print("Warning: Desktop started, but independent tray menus could not be enabled.",
              file=sys.stderr)
    return result


def ensure_desktop_tray() -> bool:
    try:
        from codex_desktop_tray import start
        return start()
    except (ImportError, OSError):
        return False


def _start_in_package(
    package_executable: Path, command: list[str], environment: dict[str, str],
) -> int:
    """Activate a short-lived helper, then transfer the environment over a pipe.

    Appx activation does not inherit the caller's environment. A duplicated pipe
    endpoint preserves it without putting credentials in files or arguments.
    The helper and its child retain package identity through PreventBreakaway.
    """
    from multiprocessing.connection import Pipe

    powershell = shutil.which("pwsh")
    if not powershell:
        print("Error: PowerShell 7 (pwsh) is required for packaged Desktop launch.", file=sys.stderr)
        return 1
    helper = Path(sys.executable).with_name("pythonw.exe")
    if not helper.is_file():
        print("Error: pythonw.exe is required for packaged Desktop launch.", file=sys.stderr)
        return 1
    server, endpoint = Pipe(duplex=True)
    try:
        helper_args = subprocess.list2cmdline([
            str(Path(__file__).resolve()), "--package-helper", str(os.getpid()),
            str(endpoint.fileno()),
        ])
        activation_env = environment.copy()
        activation_env.update({
            "APICODEX_PACKAGE_EXECUTABLE": str(package_executable),
            "APICODEX_PACKAGE_HELPER": str(helper),
            "APICODEX_PACKAGE_HELPER_ARGS": helper_args,
        })
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", _PACKAGE_LAUNCH_SCRIPT],
            env=activation_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            raise OSError("package activation failed")
        if not server.poll(20):
            raise TimeoutError("package helper did not connect")
        ready = json.loads(server.recv_bytes(4096))
        if ready != {"ready": True}:
            raise OSError("package helper initialization failed")
        endpoint.close()
        server.send_bytes(json.dumps({
            "command": command, "env": environment, "cwd": os.getcwd(),
        }, ensure_ascii=True).encode("utf-8"))
        if not server.poll(20):
            raise TimeoutError("Desktop launch was not confirmed")
        result = json.loads(server.recv_bytes(4096))
        if not isinstance(result, dict) or result.get("started") is not True:
            raise OSError("Desktop package identity could not be verified")
        return 0
    except (OSError, ValueError, EOFError, subprocess.TimeoutExpired):
        # Never echo payloads, environment values or third-party error messages.
        print("Error: packaged Desktop launch could not be confirmed. Check for an existing "
              "window before retrying; verify the app registration and PowerShell 7.", file=sys.stderr)
        return 1
    finally:
        server.close()
        endpoint.close()


def _package_name(process_handle: int | None = None) -> str:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    length = wintypes.UINT(0)
    if process_handle is None:
        query = kernel.GetCurrentPackageFullName
        query.argtypes = [ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        prefix = ()
    else:
        query = kernel.GetPackageFullName
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        prefix = (wintypes.HANDLE(process_handle),)
    query.restype = wintypes.LONG
    if query(*prefix, ctypes.byref(length), None) != 122:
        raise OSError("package identity unavailable")
    name = ctypes.create_unicode_buffer(length.value)
    if query(*prefix, ctypes.byref(length), name) != 0 or not name.value:
        raise OSError("package identity unavailable")
    return name.value


def _spawn_packaged_child(payload: dict) -> int:
    package = _package_name()
    process = subprocess.Popen(
        payload["command"], env=payload["env"], cwd=payload["cwd"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    try:
        if _package_name(int(process._handle)) != package:
            raise OSError("child package identity mismatch")
    except OSError:
        # Only terminate the process just created by this helper.
        process.terminate()
        process.wait(timeout=5)
        raise
    return process.pid


def _package_helper(parent_pid: int, pipe_handle: int) -> int:
    import _winapi
    from multiprocessing.connection import PipeConnection

    # PROCESS_DUP_HANDLE only. No inherited handles or globally named endpoint.
    parent = _winapi.OpenProcess(0x40, False, parent_pid)
    try:
        handle = _winapi.DuplicateHandle(
            parent, pipe_handle, _winapi.GetCurrentProcess(), 0, False, 2,
        )
    finally:
        _winapi.CloseHandle(parent)
    with PipeConnection(handle) as connection:
        try:
            _package_name()
            connection.send_bytes(b'{"ready":true}')
            if not connection.poll(20):
                return 1
            payload = json.loads(connection.recv_bytes(2 * 1024 * 1024))
            pid = _spawn_packaged_child(payload)
            connection.send_bytes(json.dumps({"started": True, "pid": pid}).encode("utf-8"))
            return 0
        except (OSError, ValueError, KeyError, TypeError, EOFError, subprocess.TimeoutExpired):
            try:
                connection.send_bytes(b'{"started":false}')
            except OSError:
                pass
            return 1


_LABEL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class ApiCodexWindowTitle {
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool SetWindowTextW(IntPtr hWnd, string lpString);
}
'@

function Normalize-Path([string]$Path) {
  return [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
}

function Read-UserDataDir([string]$CommandLine) {
  if (-not $CommandLine) { return $null }
  $match = [regex]::Match(
    $CommandLine,
    '(?i)(?:^|\s)(?:"--user-data-dir=(?<quoted>[^"]+)"|--user-data-dir=(?<bare>\S+))'
  )
  if (-not $match.Success) { return $null }
  $value = if ($match.Groups['quoted'].Success) {
    $match.Groups['quoted'].Value
  } else {
    $match.Groups['bare'].Value
  }
  try { return Normalize-Path $value } catch { return $null }
}

$profilePath = Normalize-Path $env:APICODEX_DESKTOP_PROFILE_PATH
$desktopExecutable = Normalize-Path $env:APICODEX_DESKTOP_EXECUTABLE
$windowTitle = $env:APICODEX_DESKTOP_WINDOW_TITLE
$timeoutMilliseconds = [int]$env:APICODEX_DESKTOP_LABEL_TIMEOUT_MS
$deadline = [DateTime]::UtcNow.AddMilliseconds($timeoutMilliseconds)

do {
  $candidates = @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe'" -ErrorAction Stop |
    Where-Object {
      if (-not $_.ExecutablePath -or -not $_.CommandLine -or $_.CommandLine -match '(?i)(?:^|\s)--type=') {
        return $false
      }
      try {
        $actualExecutable = Normalize-Path "$($_.ExecutablePath)"
      } catch {
        return $false
      }
      if (-not $actualExecutable.Equals($desktopExecutable, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
      }
      $actualProfile = Read-UserDataDir "$($_.CommandLine)"
      return $actualProfile -and $actualProfile.Equals($profilePath, [StringComparison]::OrdinalIgnoreCase)
    })

  foreach ($candidate in $candidates) {
    $process = Get-Process -Id ([int]$candidate.ProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $process -or $process.MainWindowHandle -eq 0) { continue }
    if (-not $process.MainWindowTitle.StartsWith('ChatGPT', [StringComparison]::OrdinalIgnoreCase)) { continue }
    if ([ApiCodexWindowTitle]::SetWindowTextW($process.MainWindowHandle, $windowTitle)) {
      exit 0
    }
  }
  Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)

exit 3
"""


def _safe_display_name(value: str) -> str:
    collapsed = " ".join(str(value).split())
    collapsed = re.sub(r"[\x00-\x1f\x7f]", "", collapsed).strip()
    return collapsed[:80]


def label_codex_desktop_window(
    profile_path: Path,
    display_name: str,
    desktop_executable: Path,
    *,
    timeout_seconds: float = 15.0,
) -> bool:
    """Set the main window title for one exact isolated Desktop profile."""

    if os.name != "nt" or timeout_seconds <= 0:
        return False
    name = _safe_display_name(display_name)
    if not name:
        return False
    profile = profile_path.expanduser().resolve()
    executable = desktop_executable.expanduser().resolve()
    if not profile.is_dir() or not executable.is_file():
        return False
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        return False

    environment = os.environ.copy()
    for key in (
        "APICODEX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "APICODEX_DESKTOP_PROFILE_PATH": str(profile),
            "APICODEX_DESKTOP_EXECUTABLE": str(executable),
            "APICODEX_DESKTOP_WINDOW_TITLE": f"ChatGPT ({name})",
            "APICODEX_DESKTOP_LABEL_TIMEOUT_MS": str(int(timeout_seconds * 1000)),
        }
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _LABEL_SCRIPT,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            timeout=timeout_seconds + 5,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


if __name__ == "__main__":
    if os.name == "nt" and len(sys.argv) == 4 and sys.argv[1] == "--package-helper":
        try:
            raise SystemExit(_package_helper(int(sys.argv[2]), int(sys.argv[3])))
        except (OSError, ValueError):
            raise SystemExit(1)
    raise SystemExit(2)
