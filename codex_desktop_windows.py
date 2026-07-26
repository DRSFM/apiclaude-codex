"""Windows helpers for labeling isolated Codex Desktop instances."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path


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
