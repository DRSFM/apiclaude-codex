"""Windows helpers for isolated Claude Desktop gateway instances."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse


CLAUDE_DESKTOP_ROUTE_MODEL = "claude-fable-5"
_CONFIG_NAMESPACE = uuid.UUID("7fb2e18b-0cf0-45ca-aeda-83d9db4248c2")
_RUNTIME_DIR_NAME = ".apiclaude-runtime"
_SENSITIVE_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "APICODEX_API_KEY",
)


class ClaudeDesktopError(RuntimeError):
    pass


class ClaudeDesktopAlreadyRunning(ClaudeDesktopError):
    pass


_PRIVATE_ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = [System.IO.Path]::GetFullPath($env:APICLAUDE_DESKTOP_ACL_PATH)
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
$inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit'
$propagation = [System.Security.AccessControl.PropagationFlags]::None
$allow = [System.Security.AccessControl.AccessControlType]::Allow
$rights = [System.Security.AccessControl.FileSystemRights]::FullControl

$acl = Get-Acl -LiteralPath $path
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) {
  [void]$acl.RemoveAccessRuleSpecific($rule)
}
foreach ($sid in @($current, $system, $admins)) {
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $sid, $rights, $inheritance, $propagation, $allow
  )
  [void]$acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $path -AclObject $acl

$verify = Get-Acl -LiteralPath $path
$allowed = @($current.Value, $system.Value, $admins.Value)
$unexpected = @($verify.Access | Where-Object {
  $_.AccessControlType -eq $allow -and
  $_.IdentityReference.Translate(
    [System.Security.Principal.SecurityIdentifier]
  ).Value -notin $allowed
})
if (-not $verify.AreAccessRulesProtected -or $unexpected.Count -ne 0) {
  throw 'Claude Desktop profile ACL verification failed.'
}
"""

_PRIVATE_ACL_REPORT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$path = [System.IO.Path]::GetFullPath($env:APICLAUDE_DESKTOP_ACL_PATH)
$acl = Get-Acl -LiteralPath $path
$rules = @($acl.Access | ForEach-Object {
  [pscustomobject]@{
    Sid = $_.IdentityReference.Translate(
      [System.Security.Principal.SecurityIdentifier]
    ).Value
    Type = $_.AccessControlType.ToString()
    Rights = $_.FileSystemRights.ToString()
    Inherited = $_.IsInherited
  }
})
[pscustomobject]@{
  Protected = $acl.AreAccessRulesProtected
  CurrentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  Rules = $rules
} | ConvertTo-Json -Depth 5 -Compress
"""


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _run_acl_script(
    powershell: str,
    script: str,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            [
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=30,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeDesktopError(f"failed to protect Desktop data: {exc}") from exc


def _private_acl_report(
    powershell: str,
    environment: dict[str, str],
) -> dict[str, Any]:
    result = _run_acl_script(
        powershell,
        _PRIVATE_ACL_REPORT_SCRIPT,
        environment,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ClaudeDesktopError(
            "failed to inspect the Desktop ACL"
            + (f": {detail}" if detail else "")
        )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeDesktopError("PowerShell returned an invalid Desktop ACL report") from exc
    if not isinstance(report, dict):
        raise ClaudeDesktopError("PowerShell returned an invalid Desktop ACL report")
    return report


def _desktop_acl_is_private(report: dict[str, Any]) -> bool:
    current_sid = str(report.get("CurrentUserSid") or "")
    allowed = {current_sid, "S-1-5-18", "S-1-5-32-544"}
    rules = report.get("Rules")
    if not current_sid or not report.get("Protected") or not isinstance(rules, list):
        return False
    actual: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            return False
        sid = str(rule.get("Sid") or "")
        if (
            sid not in allowed
            or rule.get("Type") != "Allow"
            or bool(rule.get("Inherited"))
            or "FullControl" not in str(rule.get("Rights") or "")
        ):
            return False
        actual.add(sid)
    return actual == allowed


def ensure_private_desktop_directory(path: Path) -> None:
    """Restrict a Desktop profile to the user plus Windows system principals."""

    if os.name != "nt":
        raise ClaudeDesktopError("isolated Claude Desktop currently requires Windows")
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise ClaudeDesktopError("PowerShell is required to protect Desktop data")
    environment = os.environ.copy()
    for key in _SENSITIVE_ENV:
        environment.pop(key, None)
    environment["APICLAUDE_DESKTOP_ACL_PATH"] = str(path)
    if _desktop_acl_is_private(_private_acl_report(powershell, environment)):
        return
    result = _run_acl_script(powershell, _PRIVATE_ACL_SCRIPT, environment)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ClaudeDesktopError(
            "failed to apply the private Desktop ACL"
            + (f": {detail}" if detail else "")
        )
    if not _desktop_acl_is_private(_private_acl_report(powershell, environment)):
        raise ClaudeDesktopError("Desktop profile ACL verification failed")


def prepare_claude_desktop_profile(
    profile_dir: Path,
    *,
    node_name: str,
    gateway_base_url: str,
    local_token: str,
    model: str,
    route_model: str = CLAUDE_DESKTOP_ROUTE_MODEL,
) -> None:
    """Write the node-local Claude Desktop 3P gateway configuration."""

    parsed = urlparse(gateway_base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
    ):
        raise ClaudeDesktopError("Desktop gateway must be an explicit loopback URL")
    if not local_token:
        raise ClaudeDesktopError("Desktop gateway token cannot be empty")
    if not model or not route_model:
        raise ClaudeDesktopError("Desktop gateway model cannot be empty")

    profile_dir = profile_dir.expanduser().resolve()
    ensure_private_desktop_directory(profile_dir)
    cowork_dir = profile_dir / "cowork-files"
    cowork_dir.mkdir(parents=True, exist_ok=True)

    desktop_config_path = profile_dir / "claude_desktop_config.json"
    desktop_config = _read_json_object(desktop_config_path)
    desktop_config["deploymentMode"] = "3p"
    desktop_config["coworkUserFilesPath"] = str(cowork_dir)
    _atomic_write_json(desktop_config_path, desktop_config)

    config_id = str(
        uuid.uuid5(_CONFIG_NAMESPACE, f"apiclaude-desktop:{node_name}")
    )
    library_dir = profile_dir / "configLibrary"
    library_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(
        library_dir / f"{config_id}.json",
        {
            "inferenceGatewayBaseUrl": gateway_base_url.rstrip("/"),
            "inferenceGatewayApiKey": local_token,
            "modelDiscoveryEnabled": False,
            "inferenceModels": [
                {
                    "name": route_model,
                    "labelOverride": model,
                    "anthropicFamilyTier": "fable",
                    "isFamilyDefault": True,
                }
            ],
            "inferenceProvider": "gateway",
            "inferenceCredentialKind": "static",
        },
    )

    meta_path = library_dir / "_meta.json"
    meta = _read_json_object(meta_path)
    entries = meta.get("entries")
    if not isinstance(entries, list):
        entries = []
    retained = [
        item
        for item in entries
        if isinstance(item, dict) and str(item.get("id") or "") != config_id
    ]
    retained.append({"id": config_id, "name": f"ApiClaude ({node_name})"})
    _atomic_write_json(
        meta_path,
        {
            "appliedId": config_id,
            "entries": retained,
        },
    )


def find_claude_desktop_executable() -> Path | None:
    override = os.environ.get("APICLAUDE_DESKTOP_EXE", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate.resolve() if candidate.is_file() else None
    if os.name != "nt":
        return None
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        return None
    script = (
        "$package = Get-AppxPackage -Name Claude -ErrorAction SilentlyContinue | "
        "Where-Object { $_.Status -eq 'Ok' } | Sort-Object Version -Descending | "
        "Select-Object -First 1; if ($package) { "
        "$manifest = Get-AppxPackageManifest -Package $package.PackageFullName; "
        "$application = @($manifest.Package.Applications.Application) | "
        "Where-Object { $_.Id -eq 'Claude' } | Select-Object -First 1; "
        "if (-not $application) { "
        "$application = @($manifest.Package.Applications.Application) | "
        "Select-Object -First 1 }; "
        "if ($application -and $application.Executable) { "
        "Join-Path $package.InstallLocation ([string]$application.Executable) } }"
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    lines = completed.stdout.strip().splitlines()
    if not lines:
        return None
    candidate = Path(lines[-1].strip())
    return candidate.resolve() if candidate.is_file() else None


def launch_claude_desktop_process(
    executable: Path,
    profile_dir: Path,
) -> subprocess.Popen[bytes]:
    executable = executable.expanduser().resolve()
    profile_dir = profile_dir.expanduser().resolve()
    if not executable.is_file():
        raise ClaudeDesktopError(f"Claude Desktop executable was not found: {executable}")
    if not profile_dir.is_dir():
        raise ClaudeDesktopError(f"Claude Desktop profile was not found: {profile_dir}")
    environment = os.environ.copy()
    for key in _SENSITIVE_ENV:
        environment.pop(key, None)
    environment["CLAUDE_USER_DATA_DIR"] = str(profile_dir)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(
            [str(executable)],
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise ClaudeDesktopError(f"failed to start Claude Desktop: {exc}") from exc


def wait_for_claude_desktop_start(
    process: subprocess.Popen[bytes],
    *,
    stability_seconds: float = 1.5,
    window_timeout: float = 20.0,
) -> None:
    """Reject launchers that exit early or never create an isolated window."""

    deadline = time.monotonic() + max(stability_seconds, 0)
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            unsigned_code = int(exit_code) & 0xFFFFFFFF
            raise ClaudeDesktopError(
                "Claude Desktop exited during startup "
                f"(code=0x{unsigned_code:08X}). Close any existing non-isolated "
                "Claude Desktop window and retry."
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)

    window_deadline = time.monotonic() + max(window_timeout, 0)
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            unsigned_code = int(exit_code) & 0xFFFFFFFF
            raise ClaudeDesktopError(
                "Claude Desktop exited before opening its isolated window "
                f"(code=0x{unsigned_code:08X})."
            )
        if desktop_process_has_visible_window(process.pid):
            return
        if time.monotonic() >= window_deadline:
            raise ClaudeDesktopError(
                "Claude Desktop did not open an isolated window within "
                f"{window_timeout:.0f} seconds."
            )
        time.sleep(0.1)


def runtime_dir(profile_dir: Path) -> Path:
    return profile_dir / _RUNTIME_DIR_NAME


def runtime_state_path(profile_dir: Path) -> Path:
    return runtime_dir(profile_dir) / "runtime.json"


def worker_log_path(profile_dir: Path) -> Path:
    return runtime_dir(profile_dir) / "worker.log"


def write_runtime_state(profile_dir: Path, state: dict[str, Any]) -> None:
    _atomic_write_json(runtime_state_path(profile_dir), state)


def read_runtime_state(profile_dir: Path) -> dict[str, Any] | None:
    path = runtime_state_path(profile_dir)
    value = _read_json_object(path)
    return value or None


def clear_runtime_state(profile_dir: Path, *, worker_pid: int | None = None) -> None:
    path = runtime_state_path(profile_dir)
    if worker_pid is not None:
        state = read_runtime_state(profile_dir)
        if state:
            try:
                owner_pid = int(state.get("workerPid") or 0)
            except (TypeError, ValueError):
                owner_pid = 0
            if owner_pid != worker_pid:
                return
    path.unlink(missing_ok=True)


def startup_error_path(profile_dir: Path) -> Path:
    return runtime_dir(profile_dir) / "startup-error.json"


def write_startup_error(profile_dir: Path, message: str) -> None:
    _atomic_write_json(
        startup_error_path(profile_dir),
        {
            "message": message,
            "time": datetime.now(timezone.utc).isoformat(),
            "workerPid": os.getpid(),
        },
    )


def read_startup_error(profile_dir: Path) -> str | None:
    value = _read_json_object(startup_error_path(profile_dir))
    message = value.get("message")
    return str(message) if isinstance(message, str) and message else None


def clear_startup_error(profile_dir: Path) -> None:
    startup_error_path(profile_dir).unlink(missing_ok=True)


def stop_request_path(profile_dir: Path) -> Path:
    return runtime_dir(profile_dir) / "stop.requested"


def request_desktop_stop(profile_dir: Path) -> None:
    path = stop_request_path(profile_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(datetime.now(timezone.utc).isoformat(), encoding="ascii")


def desktop_stop_requested(profile_dir: Path) -> bool:
    return stop_request_path(profile_dir).is_file()


def clear_desktop_stop_request(profile_dir: Path) -> None:
    stop_request_path(profile_dir).unlink(missing_ok=True)


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def runtime_is_active(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    try:
        worker_pid = int(state.get("workerPid") or 0)
        desktop_pid = int(state.get("desktopPid") or 0)
    except (TypeError, ValueError):
        return False
    return process_is_running(worker_pid) and process_is_running(desktop_pid)


@contextmanager
def desktop_instance_lock(profile_dir: Path) -> Iterator[None]:
    lock_path = runtime_dir(profile_dir) / "instance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise ClaudeDesktopAlreadyRunning(
                    "this Claude Desktop node is already running"
                ) from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise ClaudeDesktopAlreadyRunning(
                    "this Claude Desktop node is already running"
                ) from exc
        locked = True
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        yield
    finally:
        if locked:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def desktop_process_has_visible_window(pid: int) -> bool:
    if os.name != "nt" or pid <= 0:
        return False
    user32 = ctypes.windll.user32
    found = False
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        nonlocal found
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindowVisible(hwnd):
            found = True
            return False
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found


def _post_close_to_process_windows(pid: int) -> bool:
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    found = False
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd: int, _lparam: int) -> bool:
        nonlocal found
        process_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if process_id.value == pid and user32.IsWindow(hwnd):
            user32.PostMessageW(hwnd, 0x0010, 0, 0)
            found = True
        return True

    user32.EnumWindows(callback_type(callback), 0)
    return found


def close_claude_desktop_process(
    process: subprocess.Popen[bytes],
    *,
    timeout: float = 8.0,
) -> None:
    if process.poll() is not None:
        return
    try:
        _post_close_to_process_windows(process.pid)
    except OSError:
        pass
    try:
        process.wait(timeout=timeout)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def monitor_claude_desktop_process(
    process: subprocess.Popen[bytes],
    profile_dir: Path,
    *,
    hidden_grace: float = 1.5,
    poll_interval: float = 0.25,
) -> None:
    """Keep the bridge alive only while the node's main window remains open."""

    hidden_since: float | None = None
    while process.poll() is None:
        if desktop_stop_requested(profile_dir):
            close_claude_desktop_process(process)
            return
        now = time.monotonic()
        if desktop_process_has_visible_window(process.pid):
            hidden_since = None
        elif hidden_since is None:
            hidden_since = now
        elif now - hidden_since >= hidden_grace:
            # Claude normally hides to the tray on WM_CLOSE. The isolated
            # worker owns this process, so end it after the window stays gone.
            close_claude_desktop_process(process, timeout=2.0)
            return
        time.sleep(poll_interval)


def wait_for_worker_start(
    profile_dir: Path,
    worker: subprocess.Popen[Any],
    *,
    timeout: float = 20.0,
) -> tuple[dict[str, Any] | None, str | None]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = read_runtime_state(profile_dir)
        if state:
            try:
                state_worker_pid = int(state.get("workerPid") or 0)
            except (TypeError, ValueError):
                state_worker_pid = 0
            if state_worker_pid == worker.pid:
                return state, None
        error = read_startup_error(profile_dir)
        if error:
            return None, error
        if worker.poll() is not None:
            return None, f"Desktop worker exited with code {worker.returncode}"
        time.sleep(0.1)
    return None, "Desktop worker did not become ready within 20 seconds"
