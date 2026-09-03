#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Cross-platform API profile launcher for Codex CLI and Claude Code.

The script intentionally keeps Codex and Claude storage compatible with the
older local tools:
- Codex API profiles live under ~/.codex-api
- Claude API nodes live in ~/.apiclaude_config.json
"""

from __future__ import annotations

import ast
import json
import hashlib
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from codex_history_images import (
    RepairReport,
    default_state_root,
    repair_codex_history_images,
)
from codex_desktop_windows import label_codex_desktop_window
from codex_vision_proxy import (
    VisionImage,
    VisionProxyError,
    request_gemini_vision,
    vision_proxy,
)
from codex_vision_mcp import run_vision_mcp
from claude_shared_config import merge_shared_mcp_servers as merge_claude_shared_mcp
from codex_shared_config import (
    merge_shared_mcp_servers as merge_codex_shared_mcp,
    preserve_mcp_server_sections,
)
from claude_codex_bridge import (
    BridgeEndpoint,
    BridgeStartupError,
    cpa_bridge,
    litellm_bridge,
)
from claude_desktop_windows import (
    CLAUDE_DESKTOP_ROUTE_MODEL,
    ClaudeDesktopAlreadyRunning,
    ClaudeDesktopError,
    clear_desktop_stop_request,
    clear_runtime_state,
    clear_startup_error,
    close_claude_desktop_process,
    desktop_instance_lock,
    ensure_private_desktop_directory,
    find_claude_desktop_executable,
    launch_claude_desktop_process,
    monitor_claude_desktop_process,
    prepare_claude_gateway_mcp_config,
    prepare_claude_desktop_profile,
    process_is_running,
    read_runtime_state,
    request_desktop_stop,
    runtime_is_active,
    wait_for_worker_start,
    wait_for_claude_desktop_start,
    worker_log_path,
    write_runtime_state,
    write_startup_error,
)
from secure_store import SecureStore, SecureStoreError


HOME = Path.home()
CODEX_HOME = HOME / ".codex-api"
CODEX_PROFILES_PATH = CODEX_HOME / "profiles.json"
CODEX_ARCHIVE_ROOT = CODEX_HOME / "archived-profiles"
CODEX_VSCODE_DATA_ROOT = HOME / ".apicodex-vscode"
CODEX_DESKTOP_DATA_ROOT = HOME / ".apicodex-desktop"
CLAUDE_CONFIG_PATH = HOME / ".apiclaude_config.json"
CLAUDE_NODES_ROOT = HOME / ".apiclaude"
CLAUDE_ARCHIVE_ROOT = CLAUDE_NODES_ROOT / "archived-nodes"
CLAUDE_VSCODE_DATA_ROOT = HOME / ".apiclaude-vscode"
CLAUDE_DESKTOP_DATA_ROOT = HOME / ".apiclaude-desktop" / "nodes"
CLAUDE_DESKTOP_GATEWAY_MODEL = CLAUDE_DESKTOP_ROUTE_MODEL
CLAUDE_DESKTOP_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,191}$")
SECRET_STORE = SecureStore(HOME / ".apiagent-secrets")
DEFAULT_CODEX_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING_EFFORT = "high"
DEFAULT_CLAUDE_PROXY_URL = "http://127.0.0.1:7897"
DEFAULT_GEMINI_VISION_MODEL = "gemini-3.5-flash-lite"
CODEX_VISION_TOOL_NAMESPACE = "mcp__apicodex_vision"
GEMINI_VISION_CREDENTIAL_ID = "vision:gemini"
CODEX_API_AUTH_MARKER = "apicodex-managed-key-in-child-environment"
CODEX_AUTH_STORE = "keyring"
CODEX_EPHEMERAL_AUTH_OVERRIDE = 'cli_auth_credentials_store="ephemeral"'
CODEX_INSTALL_SCRIPT = "$env:CODEX_NON_INTERACTIVE=1; irm https://chatgpt.com/codex/install.ps1 | iex"
APICODEX_CODEX_EXE_ENV = "APICODEX_CODEX_EXE"
CODEX_ACCOUNT_IMAGE_REPAIR_TASK = "ApiCodex Account History Image Repair"
CODEX_SHARED_MCP_STATE_NAME = "shared-mcp.json"
CODEX_SHARED_MCP_BACKUP_DIR = "shared-mcp-backups"
CLAUDE_SHARED_MCP_STATE_NAME = "shared-mcp.json"
CLAUDE_SHARED_MCP_BACKUP_DIR = "shared-mcp-backups"
CLAUDE_DESKTOP_CODE_CONFIG_DIR_NAME = "claude-code-config"
HIDDEN_PREFIX_CHARS = "\ufeff\u200b\u200c\u200d\u2060\ufffd"
VISION_TEST_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)
CODEX_PARENT_CONTEXT_ENV = (
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "CODEX_PERMISSION_PROFILE",
    "CODEX_SHELL",
    "CODEX_THREAD_ID",
)
CODEX_DESKTOP_ENV_REMOVE = CODEX_PARENT_CONTEXT_ENV + (
    "CODEX_HOME",
    "APICODEX_DREAM_SKIN_SCRIPT",
    "APICODEX_DREAM_SKIN_PORT",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
)
CLAUDE_PROFILE_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_ATTRIBUTION_HEADER",
    "CLAUDE_CONFIG_DIR",
    "APICLAUDE_WEB_SEARCH_BASE_URL",
    "APICLAUDE_WEB_SEARCH_TOKEN",
    "APICLAUDE_WEB_SEARCH_MODEL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def clean_hidden_prefix(value: str) -> str:
    return value.lstrip(HIDDEN_PREFIX_CHARS).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip(".-")
    return slug or "profile"


def dream_skin_instance_id(profile: dict[str, Any]) -> str:
    raw = slugify(str(profile.get("id") or profile.get("name") or "profile"))
    if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", raw):
        return raw
    normalized = re.sub(r"[^a-z0-9-]+", "-", raw).strip("-") or "profile"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    return f"{normalized[:54].rstrip('-')}-{digest}"


def mask_secret(value: str, head: int = 8, tail: int = 5) -> str:
    if not value:
        return "<empty>"
    if len(value) <= head + tail:
        return "*" * len(value)
    return f"{value[:head]}***{value[-tail:]}"


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        print(f"Error: failed to read {path}: {exc}", file=sys.stderr)
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


def run_command(
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    env_remove: tuple[str, ...] | list[str] = (),
) -> int:
    exe = shutil.which(command)
    if not exe:
        print(f"Error: command not found: {command}", file=sys.stderr)
        return 1

    proc_env = os.environ.copy()
    for key in env_remove:
        proc_env.pop(key, None)
    if env:
        proc_env.update(env)

    if os.name == "nt":
        if Path(exe).suffix.lower() in {".bat", ".cmd"}:
            comspec = os.environ.get("ComSpec", "cmd.exe")
            command_line = subprocess.list2cmdline([exe, *args])
            cmd: list[str] | str = (
                f"{subprocess.list2cmdline([comspec])} /d /s /c call {command_line}"
            )
        else:
            cmd = [exe, *args]
    else:
        cmd = [exe, *args]

    try:
        completed = subprocess.run(
            cmd,
            input=input_text,
            text=True,
            encoding="utf-8",
            env=proc_env,
        )
        return completed.returncode
    except KeyboardInterrupt:
        return 130


def custom_codex_cli_path() -> Path:
    executable = "codex.exe" if os.name == "nt" else "codex"
    return HOME / ".codex-local" / "bin" / executable


def find_official_codex_cli_executable() -> str | None:
    """Resolve an official Codex CLI without selecting the ApiCodex local build."""
    custom_path = custom_codex_cli_path()
    if os.name == "nt":
        local_app_data = clean_hidden_prefix(os.environ.get("LOCALAPPDATA", ""))
        if local_app_data:
            standalone = (
                Path(local_app_data)
                / "Programs"
                / "OpenAI"
                / "Codex"
                / "bin"
                / "codex.exe"
            )
            if standalone.is_file():
                return str(standalone)

    try:
        custom_parent = custom_path.parent.resolve()
        filtered_path = os.pathsep.join(
            entry
            for entry in os.get_exec_path()
            if Path(entry).expanduser().resolve() != custom_parent
        )
    except (OSError, RuntimeError, ValueError):
        filtered_path = os.environ.get("PATH", "")
    return shutil.which("codex", path=filtered_path)


def find_codex_cli_executable(
    profile: dict[str, Any] | None = None,
) -> str | None:
    """Resolve the official CLI by default, with per-Profile custom opt-in."""
    override = clean_hidden_prefix(os.environ.get(APICODEX_CODEX_EXE_ENV, ""))
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return str(candidate)
        print(
            f"Error: {APICODEX_CODEX_EXE_ENV} does not point to a file: {candidate}",
            file=sys.stderr,
        )
        return None

    if bool((profile or {}).get("useCustomCodexCli", False)):
        local_cli = custom_codex_cli_path()
        return str(local_cli) if local_cli.is_file() else None

    return find_official_codex_cli_executable()


def start_detached_process(
    command: str,
    args: list[str],
    env: dict[str, str] | None = None,
    env_remove: tuple[str, ...] | list[str] = (),
) -> int:
    command_path = Path(command)
    exe = str(command_path) if command_path.is_file() else shutil.which(command)
    if not exe:
        print(f"Error: command not found: {command}", file=sys.stderr)
        return 1

    proc_env = os.environ.copy()
    for key in env_remove:
        proc_env.pop(key, None)
    if env:
        proc_env.update(env)

    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)

    try:
        subprocess.Popen(
            [exe, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=proc_env,
            close_fds=True,
            creationflags=creationflags,
        )
        return 0
    except OSError as exc:
        print(f"Error: failed to start {command}: {exc}", file=sys.stderr)
        return 1


def toml_basic_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def extract_base_url_from_config(home: Path) -> str:
    config_path = home / "config.toml"
    if not config_path.exists():
        return DEFAULT_CODEX_BASE_URL
    match = re.search(r'(?m)^\s*base_url\s*=\s*"([^"]+)"', config_path.read_text(encoding="utf-8-sig"))
    return match.group(1) if match else DEFAULT_CODEX_BASE_URL


def extract_model_from_config(home: Path) -> str:
    config_path = home / "config.toml"
    if not config_path.exists():
        return DEFAULT_CODEX_MODEL
    match = re.search(
        r'(?m)^\s*model\s*=\s*"([^"]+)"',
        config_path.read_text(encoding="utf-8-sig"),
    )
    return match.group(1) if match else DEFAULT_CODEX_MODEL


def codex_profile_home(profile: dict[str, Any]) -> Path:
    home = profile.get("home", ".")
    return CODEX_HOME if home == "." else CODEX_HOME / home


def is_safe_api_profile_home(profile: dict[str, Any]) -> bool:
    """Reject profile paths that could reach account or arbitrary state."""

    raw_home = profile.get("home", ".")
    if not isinstance(raw_home, str) or not raw_home:
        return False
    candidate = Path(raw_home)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    home = codex_profile_home(profile)
    if not is_isolated_codex_home(home):
        return False
    try:
        # Do not follow a profile directory link during credential migration.
        return home.resolve() == home.absolute()
    except (OSError, RuntimeError, ValueError):
        return False


def load_codex_profiles_for_image_repair() -> tuple[list[dict[str, Any]], int]:
    """Read only non-sensitive profile metadata; never migrate credentials."""

    if not CODEX_PROFILES_PATH.is_file():
        return [], 0
    try:
        data = json.loads(CODEX_PROFILES_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"Error: failed to read Codex profile metadata: {exc}", file=sys.stderr)
        return [], 1
    raw_profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(raw_profiles, list):
        print("Error: Codex profile metadata has an invalid profiles list.", file=sys.stderr)
        return [], 1
    profiles: list[dict[str, Any]] = []
    invalid = 0
    allowed = {"id", "name", "home", "baseUrl", "lastUsedAt"}
    for raw in raw_profiles:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        profile = {key: raw[key] for key in allowed if key in raw}
        if not isinstance(profile.get("home", "."), str):
            invalid += 1
            continue
        profile["id"] = str(profile.get("id") or profile.get("name") or "")
        profile["name"] = str(profile.get("name") or profile["id"] or "profile")
        profiles.append(profile)
    return profiles, invalid


def select_codex_image_repair_profile(
    profiles: list[dict[str, Any]],
    requested: str | None,
) -> dict[str, Any] | None:
    """Select metadata without the normal add/migrate side effects."""

    if not profiles:
        print("No Codex API profiles saved; nothing to repair.", file=sys.stderr)
        return None
    if requested:
        profile = find_profile(profiles, requested)
        if not profile:
            print(f"Error: Codex profile '{requested}' was not found.", file=sys.stderr)
        return profile
    if len(profiles) == 1:
        return profiles[0]
    print("Choose Codex API profile for image repair")
    for index, profile in enumerate(profiles, 1):
        print(f"[{index}] {profile.get('name')}  {profile.get('baseUrl', '')}")
    last = sorted(profiles, key=lambda item: item.get("lastUsedAt") or "", reverse=True)[0]
    choice = input(f"Choose number or name [{last.get('name')}]: ").strip()
    if not choice:
        return last
    if choice.isdigit() and 1 <= int(choice) <= len(profiles):
        return profiles[int(choice) - 1]
    profile = find_profile(profiles, choice)
    if not profile:
        print(f"Error: Codex profile '{choice}' was not found.", file=sys.stderr)
    return profile


def codex_image_repair_state_root() -> Path:
    """Return the tool-owned image index location, outside every Codex home."""

    return default_state_root()


def repair_codex_home_images(
    home: Path,
    *,
    label: str,
    dry_run: bool = False,
    quiet: bool = False,
) -> RepairReport | None:
    """Run image repair without touching credentials or Codex configuration."""

    try:
        report = repair_codex_history_images(
            home,
            state_root=codex_image_repair_state_root(),
            protected_roots=(HOME / ".codex", CODEX_HOME),
            dry_run=dry_run,
        )
    except Exception as exc:  # auxiliary self-heal must never block Desktop
        print(
            f"Warning: Codex history image repair failed for {label}: {exc}",
            file=sys.stderr,
        )
        return None

    if not quiet:
        mode = "dry-run" if dry_run else "repair"
        print(
            f"Codex history images ({mode}, {label}): "
            f"indexed={report.indexed_images}, "
            f"restored={report.restored}, "
            f"recoverable={report.recoverable}, "
            f"present={report.already_present}, "
            f"skipped={report.skipped}, "
            f"conflicts={report.conflicts}, "
            f"rejected={report.rejected}, "
            f"stale={report.stale}, "
            f"errors={report.errors}"
        )
        for issue in report.issues[:8]:
            print(f"  - {issue}")
    elif report.errors or report.conflicts or report.stale:
        print(
            f"Warning: history image repair for {label} reported "
            f"conflicts={report.conflicts}, stale={report.stale}, "
            f"errors={report.errors}.",
            file=sys.stderr,
        )
    elif report.restored:
        print(
            f"Restored {report.restored} Codex history image(s) for {label}."
        )
    return report


def finish_explicit_image_repair(
    home: Path,
    *,
    label: str,
    dry_run: bool,
) -> int:
    report = repair_codex_home_images(home, label=label, dry_run=dry_run)
    if report is None:
        return 1
    return int(bool(report.errors or report.conflicts or report.stale or report.rejected))


def configure_account_image_repair_task(*, install: bool) -> int:
    """Install or remove the explicitly opted-in Windows logon repair task."""

    if os.name != "nt":
        print(
            "Error: the account image repair task is supported only on Windows.",
            file=sys.stderr,
        )
        return 1

    if not install:
        result = run_command(
            "schtasks",
            ["/Delete", "/TN", CODEX_ACCOUNT_IMAGE_REPAIR_TASK, "/F"],
        )
        if result == 0:
            print("Removed the account history image repair logon task.")
        return result

    task_command = subprocess.list2cmdline(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "codex",
            "--repair-images",
            "--account",
        ]
    )
    result = run_command(
        "schtasks",
        [
            "/Create",
            "/TN",
            CODEX_ACCOUNT_IMAGE_REPAIR_TASK,
            "/SC",
            "ONLOGON",
            "/TR",
            task_command,
            "/RL",
            "LIMITED",
            "/IT",
            "/F",
        ],
    )
    if result == 0:
        print(
            "Installed the account history image repair logon task. "
            "It can be removed with --repair-images --account --uninstall-task."
        )
    return result


def load_codex_profiles() -> list[dict[str, Any]]:
    initialize_codex_store()
    data = read_json(CODEX_PROFILES_PATH, {"version": 1, "profiles": []})
    profiles = list(data.get("profiles") or [])
    migrated_secrets = migrate_codex_secrets(profiles, SECRET_STORE)
    migrated_cli_preferences = False
    for profile in profiles:
        if not isinstance(profile.get("useCustomCodexCli"), bool):
            profile["useCustomCodexCli"] = False
            migrated_cli_preferences = True
    if migrated_secrets or migrated_cli_preferences:
        save_codex_profiles(profiles)
    if migrated_secrets:
        finalize_codex_migration(profiles, SECRET_STORE)
    return profiles


def save_codex_profiles(profiles: list[dict[str, Any]]) -> None:
    write_json(CODEX_PROFILES_PATH, {"version": 1, "profiles": profiles})


def codex_credential_id(profile: dict[str, Any]) -> str:
    identity = profile.get("id") or profile.get("name") or "default"
    return f"codex:{identity}"


def get_codex_secret(profile: dict[str, Any]) -> str:
    credential_id = profile.get("credentialId") or codex_credential_id(profile)
    value = clean_hidden_prefix(SECRET_STORE.get(credential_id))
    if not value or value == CODEX_API_AUTH_MARKER:
        raise KeyError(f"Credential '{credential_id}' does not contain a valid API key")
    return value


def _saved_api_key(auth_path: Path) -> str:
    if not auth_path.exists():
        return ""
    data = read_json(auth_path, {})
    key_name = next(
        (
            name
            for name in data
            if "API_KEY" in name.upper() or name.upper() == "OPENAI_API_KEY"
        ),
        None,
    )
    value = data.get(key_name, "") if key_name else ""
    if not isinstance(value, str):
        return ""
    value = clean_hidden_prefix(value)
    return "" if value == CODEX_API_AUTH_MARKER else value


def migrate_codex_secrets(
    profiles: list[dict[str, Any]],
    store: SecureStore,
) -> bool:
    changed = False
    for profile in profiles:
        credential_id = profile.get("credentialId") or codex_credential_id(profile)
        if not is_safe_api_profile_home(profile):
            print(
                f"Warning: skipping Codex profile with unsafe home '{profile.get('home')}'.",
                file=sys.stderr,
            )
            continue
        home = codex_profile_home(profile)
        auth_path = home / "auth.json"
        embedded = profile.get("api_key") or profile.get("apiKey") or ""
        saved = _saved_api_key(auth_path)
        secret = clean_hidden_prefix(embedded) if isinstance(embedded, str) else ""
        secret = secret or saved

        if secret:
            store.set(credential_id, secret)
            if store.get(credential_id) != secret:
                raise SecureStoreError(
                    f"Failed to verify migrated credential '{credential_id}'"
                )
            profile["credentialId"] = credential_id
            profile.pop("api_key", None)
            profile.pop("apiKey", None)
            base_url = (
                profile.get("baseUrl")
                or profile.get("base_url")
                or DEFAULT_CODEX_BASE_URL
            )
            model = profile.get("model") or DEFAULT_CODEX_MODEL
            reasoning_effort = (
                profile.get("reasoningEffort") or DEFAULT_CODEX_REASONING_EFFORT
            )
            write_codex_config(home, base_url, model, reasoning_effort)
            changed = True
    return changed


def finalize_codex_migration(
    profiles: list[dict[str, Any]],
    store: SecureStore,
) -> None:
    for profile in profiles:
        credential_id = profile.get("credentialId")
        if not credential_id:
            continue
        if not is_safe_api_profile_home(profile):
            continue
        auth_path = codex_profile_home(profile) / "auth.json"
        saved = _saved_api_key(auth_path)
        if not saved:
            continue
        if store.get(credential_id) != saved:
            raise SecureStoreError(
                f"Refusing to sanitize unverified credential '{credential_id}'"
            )
        write_json(
            auth_path,
            {
                "credential_store": "windows-dpapi",
                "credential_id": credential_id,
            },
        )


def initialize_codex_store() -> None:
    CODEX_HOME.mkdir(parents=True, exist_ok=True)
    if CODEX_PROFILES_PATH.exists():
        return

    root_config = CODEX_HOME / "config.toml"
    root_auth = CODEX_HOME / "auth.json"
    profiles: list[dict[str, Any]] = []
    if root_config.exists() and root_auth.exists():
        profiles.append(
            {
                "id": "default",
                "name": "default",
                "baseUrl": extract_base_url_from_config(CODEX_HOME),
                "home": ".",
                "createdAt": now_iso(),
                "lastUsedAt": now_iso(),
            }
        )
    save_codex_profiles(profiles)


def write_codex_config(
    home: Path,
    base_url: str,
    model: str = DEFAULT_CODEX_MODEL,
    reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT,
) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    existing = (
        config_path.read_text(encoding="utf-8-sig") if config_path.is_file() else ""
    )
    catalog_path = home / "models.json"
    catalog_line = ""
    if catalog_path.is_file():
        catalog_line = (
            f"model_catalog_json = {toml_basic_string(catalog_path.resolve().as_posix())}\n"
        )
    config = f'''model = {toml_basic_string(model)}
model_provider = "apicodex"
model_reasoning_effort = {toml_basic_string(reasoning_effort)}
{catalog_line}cli_auth_credentials_store = "keyring"

[windows]
sandbox = "unelevated"

[shell_environment_policy]
exclude = ["APICODEX_API_KEY"]

[features]
apps = false
plugins = false

[model_providers.apicodex]
name = "API Codex"
base_url = {toml_basic_string(base_url)}
wire_api = "responses"
env_key = "APICODEX_API_KEY"
requires_openai_auth = false

[desktop]
conversationDetailMode = "STEPS_COMMANDS"
'''
    if existing:
        config = preserve_mcp_server_sections(config, existing)
    write_text_atomic(config_path, config)


def codex_account_config_path() -> Path:
    return HOME / ".codex" / "config.toml"


def codex_shared_mcp_state_path() -> Path:
    return CODEX_HOME / CODEX_SHARED_MCP_STATE_NAME


def load_codex_shared_mcp_state() -> dict[str, Any]:
    raw = read_json(
        codex_shared_mcp_state_path(),
        {"version": 1, "accountMcpEnabled": False, "managedServers": {}},
    )
    if not isinstance(raw, dict):
        return {"version": 1, "accountMcpEnabled": False, "managedServers": {}}
    managed = raw.get("managedServers")
    raw["managedServers"] = (
        {
            str(name): str(fingerprint)
            for name, fingerprint in managed.items()
            if isinstance(name, str) and isinstance(fingerprint, str)
        }
        if isinstance(managed, dict)
        else {}
    )
    return raw


def _shared_mcp_backup_root() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return CODEX_HOME / CODEX_SHARED_MCP_BACKUP_DIR / stamp


def _backup_shared_mcp_configs(
    changes: list[tuple[dict[str, Any], Path, str, str]],
) -> Path | None:
    if not changes:
        return None
    backup_root = _shared_mcp_backup_root()
    backup_root.mkdir(parents=True, exist_ok=False)
    manifest: list[dict[str, str]] = []
    for profile, config_path, existing, _updated in changes:
        profile_id = slugify(str(profile.get("id") or profile.get("name") or "profile"))
        backup_path = backup_root / f"{profile_id}.config.toml"
        write_text_atomic(backup_path, existing)
        manifest.append(
            {
                "profile": str(profile.get("name") or profile_id),
                "source": str(config_path),
                "backup": str(backup_path),
            }
        )
    write_json_atomic(
        backup_root / "manifest.json",
        {"version": 1, "createdAt": now_iso(), "files": manifest},
    )
    return backup_root


def sync_codex_shared_mcp(
    profiles: list[dict[str, Any]],
    *,
    enabled: bool | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> tuple[bool, dict[str, Any]]:
    state = load_codex_shared_mcp_state()
    was_enabled = state.get("accountMcpEnabled") is True
    desired_enabled = was_enabled if enabled is None else enabled
    report: dict[str, Any] = {
        "enabled": desired_enabled,
        "changedProfiles": [],
        "conflicts": {},
        "skippedProfiles": [],
        "servers": [],
        "excludedServers": [],
        "backup": None,
        "dryRun": dry_run,
    }
    if enabled is None and not was_enabled:
        return True, report

    source_path = codex_account_config_path()
    if desired_enabled:
        if not source_path.is_file():
            if not quiet:
                print(
                    f"Error: account Codex config was not found at {source_path}.",
                    file=sys.stderr,
                )
            return False, report
        try:
            source_text = source_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            if not quiet:
                print(f"Error: failed to read {source_path}: {exc}", file=sys.stderr)
            return False, report
    else:
        source_text = ""

    previous_hashes = state.get("managedServers") or {}
    changes: list[tuple[dict[str, Any], Path, str, str]] = []
    source_summary = merge_codex_shared_mcp("", source_text, previous_hashes)
    source_hashes = source_summary.source_hashes
    excluded = source_summary.excluded
    for profile in profiles:
        if not is_safe_api_profile_home(profile):
            if not quiet:
                print(
                    f"Error: refusing to sync MCP into unsafe profile "
                    f"'{profile.get('name')}'.",
                    file=sys.stderr,
                )
            return False, report
        config_path = codex_profile_home(profile) / "config.toml"
        if not config_path.is_file():
            report["skippedProfiles"].append(str(profile.get("name") or "profile"))
            continue
        try:
            existing = config_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            if not quiet:
                print(f"Error: failed to read {config_path}: {exc}", file=sys.stderr)
            return False, report
        result = merge_codex_shared_mcp(existing, source_text, previous_hashes)
        profile_name = str(profile.get("name") or profile.get("id") or "profile")
        if result.conflicts:
            report["conflicts"][profile_name] = list(result.conflicts)
        if result.changed:
            changes.append((profile, config_path, existing, result.text))
            report["changedProfiles"].append(profile_name)

    report["servers"] = list(source_hashes)
    report["excludedServers"] = list(excluded)
    next_state = {
        "version": 1,
        "accountMcpEnabled": desired_enabled,
        "source": str(source_path),
        "managedServers": source_hashes,
        "updatedAt": now_iso(),
    }
    if dry_run:
        return True, report

    backup_root: Path | None = None
    written: list[tuple[Path, str]] = []
    try:
        backup_root = _backup_shared_mcp_configs(changes)
        for _profile, config_path, existing, updated in changes:
            write_text_atomic(config_path, updated)
            written.append((config_path, existing))
        write_json_atomic(codex_shared_mcp_state_path(), next_state)
    except OSError as exc:
        for config_path, existing in reversed(written):
            try:
                write_text_atomic(config_path, existing)
            except OSError:
                pass
        if not quiet:
            print(f"Error: shared MCP sync failed: {exc}", file=sys.stderr)
        return False, report
    report["backup"] = str(backup_root) if backup_root else None
    return True, report


def _print_codex_shared_mcp_report(report: dict[str, Any]) -> None:
    status = "enabled" if report.get("enabled") else "disabled"
    print(f"Account MCP sharing: {status}")
    servers = report.get("servers") or []
    print(f"Shared servers: {', '.join(servers) if servers else '(none)'}")
    excluded = report.get("excludedServers") or []
    if excluded:
        print(f"Profile-owned servers not copied: {', '.join(excluded)}")
    changed = report.get("changedProfiles") or []
    action = "Would update" if report.get("dryRun") else "Updated"
    print(f"{action} profiles: {', '.join(changed) if changed else '(none)'}")
    skipped = report.get("skippedProfiles") or []
    if skipped:
        print(f"Profiles without config.toml: {', '.join(skipped)}")
    conflicts = report.get("conflicts") or {}
    for profile, names in conflicts.items():
        print(
            f"Conflict in '{profile}' (kept profile-local): {', '.join(names)}",
            file=sys.stderr,
        )
    if report.get("backup"):
        print(f"Backup: {report['backup']}")


def codex_shared_main(args: list[str]) -> int:
    action = args[0] if args else "status"
    flags = set(args[1:])
    allowed_flags = {"--account", "--dry-run"}
    if action not in {"enable", "sync", "status", "disable"} or not flags.issubset(
        allowed_flags
    ):
        print(
            "Usage: apicodex shared <enable|sync|status|disable> "
            "[--account] [--dry-run]",
            file=sys.stderr,
        )
        return 1
    if action == "enable" and "--account" not in flags:
        print(
            "Error: shared enable requires --account to authorize reading "
            "~/.codex/config.toml.",
            file=sys.stderr,
        )
        return 1

    state = load_codex_shared_mcp_state()
    if action == "status":
        report = {
            "enabled": state.get("accountMcpEnabled") is True,
            "servers": list((state.get("managedServers") or {}).keys()),
            "changedProfiles": [],
            "conflicts": {},
            "skippedProfiles": [],
            "excludedServers": [],
            "dryRun": False,
        }
        _print_codex_shared_mcp_report(report)
        print(f"Source: {state.get('source') or codex_account_config_path()}")
        return 0
    if action == "sync" and state.get("accountMcpEnabled") is not True:
        print(
            "Error: account MCP sharing is disabled. Run "
            "'apicodex shared enable --account' first.",
            file=sys.stderr,
        )
        return 1

    profiles = load_codex_profiles()
    requested_enabled = True if action == "enable" else False if action == "disable" else None
    ok, report = sync_codex_shared_mcp(
        profiles,
        enabled=requested_enabled,
        dry_run="--dry-run" in flags,
    )
    if ok:
        _print_codex_shared_mcp_report(report)
        if action == "enable" and not report.get("dryRun"):
            print(
                "Future ApiCodex launches will refresh shared MCP servers from "
                "the account config."
            )
    return 0 if ok else 1


def read_codex_model_catalog(source: Path, model: str) -> dict[str, Any]:
    try:
        if not source.is_file():
            raise ValueError("the path is not a file")
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid model catalog '{source}': {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        raise ValueError("model catalog must be a JSON object with a 'models' list")
    slugs = {
        item.get("slug")
        for item in payload["models"]
        if isinstance(item, dict) and isinstance(item.get("slug"), str)
    }
    if model not in slugs:
        raise ValueError(f"model catalog does not contain selected model slug '{model}'")
    return payload


def install_codex_model_catalog(home: Path, payload: dict[str, Any]) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    target = home / "models.json"
    temporary = home / f".models-{secrets.token_hex(8)}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def merge_codex_builtin_model_metadata(
    payload: dict[str, Any],
    builtin_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if not builtin_payload or not isinstance(builtin_payload.get("models"), list):
        return payload
    builtin_models = {
        item.get("slug").casefold(): item
        for item in builtin_payload["models"]
        if isinstance(item, dict) and isinstance(item.get("slug"), str)
    }
    merged_models: list[Any] = []
    for item in payload.get("models", []):
        if not isinstance(item, dict) or not isinstance(item.get("slug"), str):
            merged_models.append(item)
            continue
        builtin = builtin_models.get(item["slug"].casefold())
        if builtin is None:
            merged_models.append(item)
            continue
        merged = dict(builtin)
        merged.update(
            {
                "slug": item["slug"],
                "display_name": builtin.get("display_name") or item.get("display_name"),
                "description": builtin.get("description") or item.get("description"),
                "visibility": "list",
                "supported_in_api": True,
                "priority": item.get("priority", builtin.get("priority")),
            }
        )
        merged_models.append(merged)
    return {**payload, "models": merged_models}


def fetch_codex_builtin_model_catalog(
    profile: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    exe = find_codex_cli_executable(profile)
    if not exe:
        return None
    if os.name == "nt" and Path(exe).suffix.lower() in {".bat", ".cmd"}:
        comspec = os.environ.get("ComSpec", "cmd.exe")
        command_line = subprocess.list2cmdline([exe, "debug", "models"])
        command: list[str] | str = (
            f"{subprocess.list2cmdline([comspec])} /d /s /c call {command_line}"
        )
    else:
        command = [exe, "debug", "models"]

    probe_env = os.environ.copy()
    for key in (*CODEX_PARENT_CONTEXT_ENV, "APICODEX_API_KEY", "OPENAI_API_KEY"):
        probe_env.pop(key, None)
    try:
        with tempfile.TemporaryDirectory(prefix="apicodex-model-catalog-") as probe_home:
            probe_env.update(
                {
                    "CODEX_HOME": probe_home,
                    "CODEX_NON_INTERACTIVE": "1",
                }
            )
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=probe_env,
                timeout=20,
            )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0 or len(completed.stdout) > 16 * 1024 * 1024:
        return None
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return None
    return payload


def codex_vision_config(profile: dict[str, Any]) -> dict[str, Any] | None:
    value = profile.get("vision")
    if not isinstance(value, dict) or value.get("enabled") is not True:
        return None
    try:
        port = int(value.get("proxyPort"))
    except (TypeError, ValueError):
        return None
    if not 1024 <= port <= 65535:
        return None
    return value


def codex_vision_control_credential_id(profile: dict[str, Any]) -> str:
    profile_id = slugify(str(profile.get("id") or profile.get("name") or "profile"))
    return f"vision-control:{profile_id}"


def codex_vision_proxy_base_url(
    profile: dict[str, Any],
    *,
    upstream_base_url: str | None = None,
    on_demand: bool = False,
) -> str:
    vision = codex_vision_config(profile)
    if vision is None:
        return str(upstream_base_url or profile.get("baseUrl") or DEFAULT_CODEX_BASE_URL)
    base_url = str(
        upstream_base_url or profile.get("baseUrl") or DEFAULT_CODEX_BASE_URL
    )
    parsed = urlparse(base_url)
    path = parsed.path.rstrip("/")
    mode_path = "/__apicodex_vision__/on-demand" if on_demand else ""
    return f"http://127.0.0.1:{int(vision['proxyPort'])}{mode_path}{path}"


def choose_codex_vision_port(
    profile_id: str,
    profiles: list[dict[str, Any]],
) -> int:
    used = {
        int(vision["proxyPort"])
        for profile in profiles
        if (vision := codex_vision_config(profile)) is not None
        and str(profile.get("id")) != str(profile_id)
    }
    start = 19000 + int(
        hashlib.sha256(str(profile_id).encode("utf-8")).hexdigest()[:8], 16
    ) % 1000
    for offset in range(1000):
        port = 19000 + ((start - 19000 + offset) % 1000)
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            try:
                listener.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise ValueError("no free loopback port is available for the vision worker")


def _replace_toml_key_in_section(
    path: Path,
    section: str,
    key: str,
    value: str | None,
) -> None:
    raw = path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    header = f"[{section}]"
    section_index: int | None = None
    end_index = len(lines)
    key_index: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section_index is not None:
                end_index = index
                break
            if stripped == header:
                section_index = index
                continue
        if section_index is not None and re.match(rf"^{re.escape(key)}\s*=", stripped):
            key_index = index
    if section_index is None:
        if value is None:
            return
        suffix = "" if not raw or raw.endswith(("\n", "\r")) else newline
        lines.append(f"{suffix}{header}{newline}{key} = {value}{newline}")
    elif key_index is not None:
        if value is None:
            del lines[key_index]
        else:
            lines[key_index] = f"{key} = {value}{newline}"
    elif value is not None:
        lines.insert(end_index, f"{key} = {value}{newline}")
    updated = "".join(lines)
    if updated != raw:
        path.write_text(updated, encoding="utf-8")


def _update_toml_string_array_key_in_section(
    path: Path,
    section: str,
    key: str,
    value: str,
    *,
    enabled: bool,
) -> None:
    raw = path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    header = f"[{section}]"
    section_index: int | None = None
    end_index = len(lines)
    key_index: int | None = None
    values: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section_index is not None:
                end_index = index
                break
            if stripped == header:
                section_index = index
                continue
        if section_index is not None and re.match(rf"^{re.escape(key)}\s*=", stripped):
            key_index = index
            literal = stripped.split("=", 1)[1].strip()
            try:
                parsed = ast.literal_eval(literal)
            except (SyntaxError, ValueError) as exc:
                raise ValueError(
                    f"unsupported TOML array syntax for [{section}] {key}"
                ) from exc
            if not isinstance(parsed, list) or not all(
                isinstance(item, str) for item in parsed
            ):
                raise ValueError(f"[{section}] {key} must be an array of strings")
            values = list(dict.fromkeys(parsed))

    if enabled:
        if value not in values:
            values.append(value)
    else:
        values = [item for item in values if item != value]

    if section_index is None:
        if not enabled:
            return
        suffix = "" if not raw or raw.endswith(("\n", "\r")) else newline
        rendered = ", ".join(toml_basic_string(item) for item in values)
        lines.append(f"{suffix}{header}{newline}{key} = [{rendered}]{newline}")
    elif key_index is not None:
        if not values:
            del lines[key_index]
        else:
            rendered = ", ".join(toml_basic_string(item) for item in values)
            lines[key_index] = f"{key} = [{rendered}]{newline}"
    elif enabled:
        rendered = ", ".join(toml_basic_string(item) for item in values)
        lines.insert(end_index, f"{key} = [{rendered}]{newline}")

    updated = "".join(lines)
    if updated != raw:
        path.write_text(updated, encoding="utf-8")


def _replace_toml_section(
    path: Path,
    section: str,
    body: list[str] | None,
) -> None:
    raw = path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    start: int | None = None
    end: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("[") and stripped.endswith("]")):
            continue
        current = stripped[1:-1]
        if start is None and (current == section or current.startswith(section + ".")):
            start = index
            continue
        if start is not None and not (
            current == section or current.startswith(section + ".")
        ):
            end = index
            break
    if start is not None:
        del lines[start : end if end is not None else len(lines)]
    updated = "".join(lines).rstrip("\r\n")
    if body is not None:
        if updated:
            updated += newline + newline
        updated += newline.join([f"[{section}]", *body])
    if updated:
        updated += newline
    if updated != raw:
        path.write_text(updated, encoding="utf-8")


def configure_codex_vision_files(
    profile: dict[str, Any],
    *,
    enabled: bool,
    upstream_base_url: str | None = None,
) -> None:
    home = codex_profile_home(profile)
    config_path = home / "config.toml"
    if not config_path.is_file():
        write_codex_config(
            home,
            str(profile.get("baseUrl") or DEFAULT_CODEX_BASE_URL),
            str(profile.get("model") or DEFAULT_CODEX_MODEL),
            str(profile.get("reasoningEffort") or DEFAULT_CODEX_REASONING_EFFORT),
        )
    base_url = (
        codex_vision_proxy_base_url(
            profile,
            upstream_base_url=upstream_base_url,
            on_demand=True,
        )
        if enabled
        else str(upstream_base_url or profile.get("baseUrl") or DEFAULT_CODEX_BASE_URL)
    )
    _replace_toml_key_in_section(
        config_path,
        "model_providers.apicodex",
        "base_url",
        toml_basic_string(base_url),
    )
    _replace_toml_key_in_section(
        config_path,
        "features",
        "enable_request_compression",
        "false" if enabled else None,
    )
    _update_toml_string_array_key_in_section(
        config_path,
        "features.code_mode",
        "direct_only_tool_namespaces",
        "mcp__apicodex_vision__",
        enabled=False,
    )
    _update_toml_string_array_key_in_section(
        config_path,
        "features.code_mode",
        "direct_only_tool_namespaces",
        CODEX_VISION_TOOL_NAMESPACE,
        enabled=enabled,
    )
    profile_id = str(profile.get("id") or profile.get("name") or "profile")
    if enabled:
        args = [
            str(Path(__file__).resolve()),
            "codex",
            "--vision-mcp",
            "--api-profile",
            profile_id,
        ]
        _replace_toml_section(
            config_path,
            "mcp_servers.apicodex_vision",
            [
                f"command = {toml_basic_string(sys.executable)}",
                "args = [" + ", ".join(toml_basic_string(value) for value in args) + "]",
                'env = { PYTHONIOENCODING = "utf-8", PYTHONUTF8 = "1" }',
                "enabled = true",
                "required = true",
                'enabled_tools = ["inspect_images"]',
                'default_tools_approval_mode = "auto"',
                "startup_timeout_sec = 10",
                "tool_timeout_sec = 60",
            ],
        )
    else:
        _replace_toml_section(config_path, "mcp_servers.apicodex_vision", None)

    catalog_path = home / "models.json"
    if catalog_path.is_file():
        payload = read_json(catalog_path, {})
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ValueError(f"invalid model catalog: {catalog_path}")
        changed = False
        modalities = ["text", "image"] if enabled else ["text"]
        for item in payload["models"]:
            if isinstance(item, dict) and item.get("input_modalities") != modalities:
                item["input_modalities"] = modalities
                changed = True
        if changed:
            install_codex_model_catalog(home, payload)


def validate_gemini_vision(api_key: str, model: str) -> None:
    description = request_gemini_vision(
        api_key=api_key,
        model=model,
        user_prompt="Return the single word OK if this one-pixel image is readable.",
        images=[VisionImage("image/png", VISION_TEST_IMAGE)],
    )
    if not description.strip():
        raise ValueError("Gemini vision validation returned an empty response")


def codex_vision_worker_is_healthy(
    profile: dict[str, Any],
    control_token: str,
) -> bool:
    vision = codex_vision_config(profile)
    if vision is None:
        return False
    parsed = urlparse(codex_vision_proxy_base_url(profile))
    request = Request(
        f"{parsed.scheme}://{parsed.netloc}/__apicodex_vision__/health",
        headers={"X-ApiCodex-Vision-Control": control_token},
    )
    try:
        with urlopen(request, timeout=1) as response:
            raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return False
        payload = json.loads(raw.decode("utf-8-sig"))
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError):
        return False
    expected = str(profile.get("id") or profile.get("name") or "profile")
    return payload.get("status") == "ok" and payload.get("profile") == expected


def ensure_codex_vision_worker(profile: dict[str, Any]) -> bool:
    vision = codex_vision_config(profile)
    if vision is None:
        return True
    credential_id = str(
        vision.get("controlCredentialId")
        or codex_vision_control_credential_id(profile)
    )
    try:
        control_token = clean_hidden_prefix(SECRET_STORE.get(credential_id))
    except (KeyError, SecureStoreError):
        print(
            f"Error: profile '{profile.get('name')}' has no vision worker control token.",
            file=sys.stderr,
        )
        return False
    if codex_vision_worker_is_healthy(profile, control_token):
        return True

    profile_name = str(profile.get("id") or profile.get("name") or "")
    if not profile_name:
        return False
    code = start_detached_process(
        sys.executable,
        [
            str(Path(__file__).resolve()),
            "codex",
            "--vision-worker",
            "--api-profile",
            profile_name,
        ],
        env_remove=CODEX_DESKTOP_ENV_REMOVE
        + ("APICODEX_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    )
    if code != 0:
        return False
    for _ in range(40):
        time.sleep(0.25)
        if codex_vision_worker_is_healthy(profile, control_token):
            return True
    print(
        f"Error: vision worker for '{profile.get('name')}' did not become ready.",
        file=sys.stderr,
    )
    return False


def prepare_codex_vision_runtime(profile: dict[str, Any]) -> bool:
    if codex_vision_config(profile) is None:
        return True
    try:
        configure_codex_vision_files(profile, enabled=True)
    except (OSError, UnicodeError, ValueError) as exc:
        print(
            f"Error: failed to prepare vision tools for '{profile.get('name')}': {exc}",
            file=sys.stderr,
        )
        return False
    return ensure_codex_vision_worker(profile)


def run_codex_vision_worker(requested: str) -> int:
    profiles = load_codex_profiles()
    profile = find_profile(profiles, requested)
    if not profile or not is_safe_api_profile_home(profile):
        return 1
    vision = codex_vision_config(profile)
    if vision is None:
        return 1
    try:
        upstream_key = get_codex_secret(profile)
        gemini_key = clean_hidden_prefix(
            SECRET_STORE.get(str(vision.get("credentialId") or GEMINI_VISION_CREDENTIAL_ID))
        )
        control_token = clean_hidden_prefix(
            SECRET_STORE.get(
                str(
                    vision.get("controlCredentialId")
                    or codex_vision_control_credential_id(profile)
                )
            )
        )
    except (KeyError, SecureStoreError):
        return 1

    profile_id = str(profile.get("id") or profile.get("name") or "profile")
    try:
        with vision_proxy(
            upstream_base_url=str(profile.get("baseUrl") or DEFAULT_CODEX_BASE_URL),
            upstream_api_key=upstream_key,
            gemini_api_key=gemini_key,
            gemini_model=str(vision.get("model") or DEFAULT_GEMINI_VISION_MODEL),
            control_token=control_token,
            profile_id=profile_id,
            observation_cache_path=(
                codex_profile_home(profile) / "vision-observations.json"
            ),
            port=int(vision["proxyPort"]),
        ):
            while True:
                time.sleep(5)
                data = read_json(CODEX_PROFILES_PATH, {"profiles": []})
                current = find_profile(data.get("profiles") or [], profile_id)
                current_vision = codex_vision_config(current or {})
                if (
                    current_vision is None
                    or int(current_vision["proxyPort"]) != int(vision["proxyPort"])
                ):
                    return 0
    except (OSError, VisionProxyError):
        return 1
    except KeyboardInterrupt:
        return 130


def run_codex_vision_mcp(requested: str) -> int:
    profiles = load_codex_profiles()
    profile = find_profile(profiles, requested)
    if not profile or not is_safe_api_profile_home(profile):
        return 1
    vision = codex_vision_config(profile)
    if vision is None:
        return 1
    credential_id = str(
        vision.get("controlCredentialId")
        or codex_vision_control_credential_id(profile)
    )
    try:
        control_token = clean_hidden_prefix(SECRET_STORE.get(credential_id))
    except (KeyError, SecureStoreError):
        return 1
    parsed = urlparse(codex_vision_proxy_base_url(profile))
    endpoint_origin = f"{parsed.scheme}://{parsed.netloc}"
    return run_vision_mcp(endpoint_origin, control_token)


def setup_codex_vision(profiles: list[dict[str, Any]], names: list[str]) -> int:
    if not names:
        print(
            "Error: usage: apicodex vision setup PROFILE [PROFILE ...]",
            file=sys.stderr,
        )
        return 1
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in names:
        profile = find_profile(profiles, name)
        if not profile:
            print(f"Error: Codex profile '{name}' was not found.", file=sys.stderr)
            return 1
        if not is_safe_api_profile_home(profile):
            print(
                f"Error: refusing to configure vision outside ~/.codex-api: {name}",
                file=sys.stderr,
            )
            return 1
        identity = str(profile.get("id") or profile.get("name"))
        if identity not in seen:
            selected.append(profile)
            seen.add(identity)

    api_key = clean_hidden_prefix(getpass("Gemini API key: "))
    if not api_key:
        print("Error: Gemini API key cannot be empty.", file=sys.stderr)
        return 1
    try:
        validate_gemini_vision(api_key, DEFAULT_GEMINI_VISION_MODEL)
    except (ValueError, VisionProxyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    missing = object()
    previous_visions = {
        id(profile): json.loads(json.dumps(profile["vision"]))
        if "vision" in profile
        else missing
        for profile in selected
    }
    file_snapshots: dict[Path, bytes | None] = {}
    for profile in selected:
        home = codex_profile_home(profile)
        for path in (home / "config.toml", home / "models.json"):
            file_snapshots[path] = path.read_bytes() if path.is_file() else None
    try:
        previous_gemini_key: str | None = SECRET_STORE.get(
            GEMINI_VISION_CREDENTIAL_ID
        )
    except KeyError:
        previous_gemini_key = None
    except SecureStoreError as exc:
        print(f"Error: failed to read the saved Gemini credential: {exc}", file=sys.stderr)
        return 1

    created_control_credentials: list[str] = []
    try:
        SECRET_STORE.set(GEMINI_VISION_CREDENTIAL_ID, api_key)
        for profile in selected:
            profile_id = str(profile.get("id") or profile.get("name") or "profile")
            existing = codex_vision_config(profile)
            port = (
                int(existing["proxyPort"])
                if existing is not None
                else choose_codex_vision_port(profile_id, profiles)
            )
            control_credential_id = codex_vision_control_credential_id(profile)
            try:
                SECRET_STORE.get(control_credential_id)
            except KeyError:
                SECRET_STORE.set(control_credential_id, secrets.token_urlsafe(32))
                created_control_credentials.append(control_credential_id)
            profile["vision"] = {
                "enabled": True,
                "provider": "gemini",
                "model": DEFAULT_GEMINI_VISION_MODEL,
                "credentialId": GEMINI_VISION_CREDENTIAL_ID,
                "controlCredentialId": control_credential_id,
                "proxyPort": port,
            }
            configure_codex_vision_files(profile, enabled=True)
        save_codex_profiles(profiles)
    except (OSError, UnicodeError, ValueError, SecureStoreError) as exc:
        for profile in selected:
            previous = previous_visions[id(profile)]
            if previous is missing:
                profile.pop("vision", None)
            else:
                profile["vision"] = previous
        for path, content in file_snapshots.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
            except OSError as restore_exc:
                print(
                    f"Warning: failed to restore {path}: {restore_exc}",
                    file=sys.stderr,
                )
        if previous_gemini_key is None:
            SECRET_STORE.clear(GEMINI_VISION_CREDENTIAL_ID)
        else:
            SECRET_STORE.set(GEMINI_VISION_CREDENTIAL_ID, previous_gemini_key)
        for credential_id in created_control_credentials:
            SECRET_STORE.clear(credential_id)
        print(f"Error: failed to configure vision fallback: {exc}", file=sys.stderr)
        return 1

    ready = True
    for profile in selected:
        profile_ready = ensure_codex_vision_worker(profile)
        ready = profile_ready and ready
        if profile_ready:
            print(
                f"Enabled Gemini vision fallback for '{profile.get('name')}' "
                f"with {DEFAULT_GEMINI_VISION_MODEL}."
            )
    return 0 if ready else 1


def disable_codex_vision(profiles: list[dict[str, Any]], names: list[str]) -> int:
    if not names:
        print(
            "Error: usage: apicodex vision disable PROFILE [PROFILE ...]",
            file=sys.stderr,
        )
        return 1
    selected: list[dict[str, Any]] = []
    for name in names:
        profile = find_profile(profiles, name)
        if not profile:
            print(f"Error: Codex profile '{name}' was not found.", file=sys.stderr)
            return 1
        selected.append(profile)
    for profile in selected:
        vision = codex_vision_config(profile)
        if vision is None:
            continue
        configure_codex_vision_files(profile, enabled=False)
        credential_id = str(
            vision.get("controlCredentialId")
            or codex_vision_control_credential_id(profile)
        )
        SECRET_STORE.clear(credential_id)
        profile.pop("vision", None)
        print(f"Disabled vision fallback for '{profile.get('name')}'.")
    save_codex_profiles(profiles)
    if not any(codex_vision_config(profile) for profile in profiles):
        SECRET_STORE.clear(GEMINI_VISION_CREDENTIAL_ID)
    return 0


def show_codex_vision_status(profiles: list[dict[str, Any]]) -> int:
    configured = 0
    for profile in profiles:
        vision = codex_vision_config(profile)
        if vision is None:
            continue
        configured += 1
        print(
            f"{profile.get('name')}: {vision.get('provider')} / "
            f"{vision.get('model')} on 127.0.0.1:{vision.get('proxyPort')}"
        )
    if not configured:
        print("No Codex API profiles have a vision fallback configured.")
    return 0


def codex_vision_main(args: list[str]) -> int:
    if not args or args[0] in {"-h", "--help"}:
        print(
            "Usage:\n"
            "  apicodex vision setup PROFILE [PROFILE ...]\n"
            "  apicodex vision status\n"
            "  apicodex vision disable PROFILE [PROFILE ...]"
        )
        return 0 if args else 1
    command = args[0]
    profiles = load_codex_profiles()
    if command == "setup":
        return setup_codex_vision(profiles, args[1:])
    if command == "status" and len(args) == 1:
        return show_codex_vision_status(profiles)
    if command == "disable":
        return disable_codex_vision(profiles, args[1:])
    print(f"Error: unknown vision command: {command}", file=sys.stderr)
    return 1


def fetch_codex_provider_models(base_url: str, api_key: str) -> list[str]:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("API base URL must be an absolute HTTP or HTTPS URL")
    request = Request(
        base_url.rstrip("/") + "/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
    except HTTPError as exc:
        raise ValueError(f"model discovery returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError(f"model discovery failed: {exc.reason}") from exc
    except OSError as exc:
        raise ValueError(f"model discovery failed: {exc}") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("model discovery response exceeded 4 MiB")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("model discovery returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("model discovery response must contain a 'data' list")
    models: list[str] = []
    seen: set[str] = set()
    for item in payload["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        model_id = clean_hidden_prefix(item["id"])
        if (
            not model_id
            or len(model_id) > 512
            or any(ord(char) < 32 for char in model_id)
            or model_id.casefold() in seen
        ):
            continue
        seen.add(model_id.casefold())
        models.append(model_id)
    if not models:
        raise ValueError("model discovery returned no usable model IDs")
    return models


def is_codex_text_model(model: str) -> bool:
    normalized = model.casefold()
    non_agent_markers = (
        "embedding",
        "rerank",
        "whisper",
        "text-to-speech",
        "dall-e",
        "flux",
        "sora",
    )
    return not any(marker in normalized for marker in non_agent_markers)


def build_codex_provider_catalog(
    models: list[str],
    default_model: str,
) -> dict[str, Any]:
    reasoning_markers = (
        "deepseek",
        "think",
        "reason",
        "glm-5",
        "kimi",
        "minimax",
        "qwen",
        "gpt-oss",
    )
    openai_reasoning_markers = (
        "codex",
        "gpt-5",
        "o1",
        "o3",
        "o4",
    )
    selected = next(
        (model for model in models if model.casefold() == default_model.casefold()),
        None,
    )
    if selected is None:
        raise ValueError(f"default model '{default_model}' was not discovered")
    ordered_models = [selected, *(model for model in models if model != selected)]
    catalog: list[dict[str, Any]] = []
    for priority, model in enumerate(ordered_models, start=1):
        normalized_model = model.casefold()
        is_openai_reasoning_model = any(
            marker in normalized_model for marker in openai_reasoning_markers
        )
        supports_reasoning = is_openai_reasoning_model or any(
            marker in normalized_model for marker in reasoning_markers
        )
        if is_openai_reasoning_model:
            reasoning_levels = [
                {
                    "effort": effort,
                    "description": description,
                }
                for effort, description in (
                    ("low", "Fast responses with lighter reasoning"),
                    ("medium", "Balances speed and reasoning depth"),
                    ("high", "Greater reasoning depth for complex tasks"),
                    ("xhigh", "Extra high reasoning depth for complex tasks"),
                )
            ]
        elif supports_reasoning:
            reasoning_levels = [
                {
                    "effort": "high",
                    "description": "Use the provider's high reasoning mode",
                }
            ]
        else:
            reasoning_levels = []
        catalog.append(
            {
                "slug": model,
                "display_name": model,
                "description": "Imported from the configured OpenAI-compatible provider.",
                "default_reasoning_level": "high" if supports_reasoning else None,
                "supported_reasoning_levels": reasoning_levels,
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": priority,
                "availability_nux": None,
                "upgrade": None,
                "base_instructions": (
                    "You are Codex, a coding agent. You and the user share a workspace. "
                    "Work carefully, use the available tools when appropriate, and complete "
                    "the user's software task with clear verification."
                ),
                "supports_reasoning_summary_parameter": supports_reasoning,
                "default_reasoning_summary": "none",
                "support_verbosity": False,
                "default_verbosity": None,
                "apply_patch_tool_type": "freeform",
                "truncation_policy": {"mode": "tokens", "limit": 10000},
                "supports_parallel_tool_calls": True,
                "context_window": 128000,
                "max_context_window": 128000,
                "effective_context_window_percent": 95,
                "experimental_supported_tools": [],
                "input_modalities": ["text"],
                "supports_search_tool": False,
            }
        )
    return {"models": catalog}


def choose_codex_provider_model(models: list[str], default: str | None = None) -> str:
    print("Available Codex-compatible models")
    for index, model in enumerate(models, start=1):
        suffix = " (current)" if default and model.casefold() == default.casefold() else ""
        print(f"[{index}] {model}{suffix}")
    prompt = "Choose default model number or name"
    if default and any(model.casefold() == default.casefold() for model in models):
        prompt += f" [{default}]"
    choice = input(prompt + ": ").strip()
    if not choice and default:
        for model in models:
            if model.casefold() == default.casefold():
                return model
    if choice.isdigit() and 1 <= int(choice) <= len(models):
        return models[int(choice) - 1]
    for model in models:
        if model.casefold() == choice.casefold():
            return model
    raise ValueError(f"model selection '{choice}' was not found")


def ensure_codex_keyring_store(home: Path) -> None:
    config_path = home / "config.toml"
    raw = config_path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    first_table_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("[") and line.strip().endswith("]")
        ),
        len(lines),
    )
    key_indices = [
        index
        for index, line in enumerate(lines[:first_table_index])
        if re.match(r"^\s*cli_auth_credentials_store\s*=", line)
    ]
    replacement = f'cli_auth_credentials_store = "{CODEX_AUTH_STORE}"{newline}'
    if key_indices:
        lines[key_indices[0]] = replacement
        for index in reversed(key_indices[1:]):
            del lines[index]
    else:
        lines.insert(first_table_index, replacement)
    updated = "".join(lines)
    if updated != raw:
        config_path.write_text(updated, encoding="utf-8")


def ensure_codex_desktop_coding_mode(home: Path) -> None:
    config_path = home / "config.toml"
    raw = config_path.read_text(encoding="utf-8-sig")
    newline = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.splitlines(keepends=True)
    desktop_header_index: int | None = None
    detail_index: int | None = None
    in_desktop = False

    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_desktop = stripped == "[desktop]"
            if in_desktop:
                desktop_header_index = index
            continue
        if in_desktop and re.match(r"^conversationDetailMode\s*=", stripped):
            detail_index = index
            break

    if detail_index is not None:
        current = lines[detail_index]
        replacement = f'conversationDetailMode = "STEPS_COMMANDS"{newline}'
        if current != replacement:
            lines[detail_index] = replacement
    elif desktop_header_index is not None:
        lines.insert(
            desktop_header_index + 1,
            f'conversationDetailMode = "STEPS_COMMANDS"{newline}',
        )
    else:
        suffix = "" if not raw or raw.endswith(("\n", "\r")) else newline
        lines.append(
            f'{suffix}[desktop]{newline}'
            f'conversationDetailMode = "STEPS_COMMANDS"{newline}'
        )

    updated = "".join(lines)
    if updated != raw:
        config_path.write_text(updated, encoding="utf-8")


def prepare_codex_desktop_profile(home: Path, profile: dict[str, Any]) -> None:
    # Older versions wrote a marker here, but official Codex sends that field
    # as a real bearer token instead of falling back to the provider env key.
    auth_path = home / "auth.json"
    if auth_path.exists():
        auth_path.unlink()
    ensure_codex_keyring_store(home)
    ensure_codex_desktop_coding_mode(home)


def codex_auth_decryption_failed(home: Path, codex_exe: str) -> bool:
    """Return true only when Codex explicitly reports an unreadable secrets file."""
    probe_env = os.environ.copy()
    for key in CODEX_DESKTOP_ENV_REMOVE + (
        "APICODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
    ):
        probe_env.pop(key, None)
    probe_env["CODEX_HOME"] = str(home)
    try:
        completed = subprocess.run(
            [codex_exe, "login", "status"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=probe_env,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    output = f"{completed.stdout}\n{completed.stderr}".casefold()
    return "failed to decrypt secrets file" in output


def archive_unreadable_codex_auth(home: Path) -> Path | None:
    """Move an unreadable encrypted auth cache aside without deleting it."""
    source = home / "secrets" / "codex_auth.age"
    if not source.is_file():
        return None
    timestamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    target = source.with_name(f"codex_auth.age.unreadable-{timestamp}.bak")
    counter = 1
    while target.exists():
        target = source.with_name(
            f"codex_auth.age.unreadable-{timestamp}-{counter}.bak"
        )
        counter += 1
    source.replace(target)
    return target


def ensure_codex_keyring_auth(
    home: Path,
    api_key: str,
    profile: dict[str, Any] | None = None,
) -> bool:
    """Sync one API profile's key into the selected Codex CLI keyring."""
    if os.name != "nt":
        print("Error: Codex keyring authentication requires Windows.", file=sys.stderr)
        return False
    prepare_codex_desktop_profile(home, {})
    codex_exe = find_codex_cli_executable(profile)
    if not codex_exe:
        print("Error: Codex CLI was not found.", file=sys.stderr)
        return False
    if codex_auth_decryption_failed(home, codex_exe):
        try:
            archived = archive_unreadable_codex_auth(home)
        except OSError as exc:
            print(
                f"Error: failed to archive unreadable Codex auth for {home}: {exc}",
                file=sys.stderr,
            )
            return False
        if archived is not None:
            print(f"Archived unreadable Codex auth cache to {archived}")
    code = run_command(
        codex_exe,
        ["login", "--with-api-key"],
        env={"CODEX_HOME": str(home)},
        input_text=clean_hidden_prefix(api_key) + "\n",
        env_remove=CODEX_DESKTOP_ENV_REMOVE + (
            "APICODEX_API_KEY",
            "CODEX_ACCESS_TOKEN",
        ),
    )
    if code != 0:
        print(
            f"Error: failed to store the API key in Codex keyring for {home}.",
            file=sys.stderr,
        )
        return False
    # Do not retain a plaintext fallback if an older Codex build created one.
    auth_path = home / "auth.json"
    if auth_path.exists():
        auth_path.unlink()
    return True


def add_current_project_trust(home: Path) -> None:
    cwd = str(Path.cwd())
    config_path = home / "config.toml"
    if not config_path.exists():
        return
    header = f"[projects.{toml_basic_string(cwd)}]"
    raw = config_path.read_text(encoding="utf-8-sig")
    if header not in raw:
        with config_path.open("a", encoding="utf-8") as handle:
            handle.write(f'\n{header}\ntrust_level = "trusted"\n')


def find_profile(profiles: list[dict[str, Any]], name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    for profile in profiles:
        if profile.get("id", "").lower() == name.lower() or profile.get("name", "").lower() == name.lower():
            return profile
    return None


def show_codex_profiles(profiles: list[dict[str, Any]]) -> None:
    if not profiles:
        print("No Codex API profiles saved. Use 'apicodex --api-add' to add one.")
        return
    for index, profile in enumerate(profiles, 1):
        print(
            f"[{index}] {profile.get('name')}  {profile.get('baseUrl')}  "
            f"cli={'custom' if profile.get('useCustomCodexCli') else 'official'}  "
            f"lastUsed={profile.get('lastUsedAt') or '-'}"
        )


def codex_profile_metadata(profile: dict[str, Any]) -> dict[str, Any]:
    if not is_safe_api_profile_home(profile):
        raise ValueError(
            f"refusing to expose an unsafe Codex profile home: {profile.get('home')}"
        )
    profile_id = slugify(str(profile.get("id") or profile.get("name") or "profile"))
    return {
        "id": str(profile.get("id") or profile_id),
        "instanceId": dream_skin_instance_id(profile),
        "name": str(profile.get("name") or profile_id),
        "baseUrl": str(profile.get("baseUrl") or ""),
        "profileHome": str(codex_profile_home(profile).resolve()),
        "desktopData": str((CODEX_DESKTOP_DATA_ROOT / profile_id).resolve()),
        "useCustomCodexCli": bool(profile.get("useCustomCodexCli", False)),
        "createdAt": profile.get("createdAt"),
        "lastUsedAt": profile.get("lastUsedAt"),
    }


def show_codex_profiles_json(profiles: list[dict[str, Any]]) -> bool:
    try:
        metadata = [codex_profile_metadata(profile) for profile in profiles]
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return False
    payload = {"schemaVersion": 1, "profiles": metadata}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return True


def add_codex_profile(
    requested: str | None = None,
    *,
    name: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    model_catalog: Path | None = None,
) -> int:
    profiles = load_codex_profiles()
    print("Add or update a Codex API profile")
    selected = find_profile(profiles, requested) if requested else None
    if requested and not selected:
        print(f"Error: Codex profile '{requested}' was not found.", file=sys.stderr)
        return 1
    if selected and not is_safe_api_profile_home(selected):
        print(
            "Error: refusing to update a Codex profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1
    default_name = str(selected.get("name")) if selected else ""
    if name is None:
        prompt = f"Profile name [{default_name}]: " if default_name else "Profile name: "
        profile_name = input(prompt).strip() or default_name
        if not profile_name:
            profile_name = "default" if not profiles else ""
    else:
        profile_name = clean_hidden_prefix(name)
    if not profile_name:
        print("Error: profile name cannot be empty.", file=sys.stderr)
        return 1

    existing = selected or find_profile(profiles, profile_name)
    conflict = find_profile(profiles, profile_name)
    if selected and conflict and conflict.get("id") != selected.get("id"):
        print(f"Error: profile name '{profile_name}' is already in use.", file=sys.stderr)
        return 1
    if existing and not is_safe_api_profile_home(existing):
        print(
            "Error: refusing to update a Codex profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1
    default_url = existing.get("baseUrl") if existing else DEFAULT_CODEX_BASE_URL
    if base_url is None:
        provider_url = clean_hidden_prefix(
            input(f"API base URL [{default_url}]: ") or default_url
        ).rstrip("/")
    else:
        provider_url = clean_hidden_prefix(base_url).rstrip("/")
    if not provider_url:
        print("Error: API base URL cannot be empty.", file=sys.stderr)
        return 1

    selected_model = clean_hidden_prefix(
        model or str((existing or {}).get("model") or DEFAULT_CODEX_MODEL)
    )
    selected_reasoning_effort = clean_hidden_prefix(
        reasoning_effort
        or str(
            (existing or {}).get("reasoningEffort")
            or DEFAULT_CODEX_REASONING_EFFORT
        )
    )
    if not selected_model:
        print("Error: model cannot be empty.", file=sys.stderr)
        return 1
    if not selected_reasoning_effort:
        print("Error: reasoning effort cannot be empty.", file=sys.stderr)
        return 1

    catalog_payload: dict[str, Any] | None = None
    if model_catalog is not None:
        try:
            catalog_payload = read_codex_model_catalog(
                Path(model_catalog).expanduser(), selected_model
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    if existing:
        profile = existing
        home = codex_profile_home(profile)
    else:
        base_id = slugify(profile_name)
        profile_id = base_id
        n = 2
        while find_profile(profiles, profile_id):
            profile_id = f"{base_id}-{n}"
            n += 1
        home_rel = "." if not profiles and not (CODEX_HOME / "auth.json").exists() else str(Path("profiles") / profile_id)
        profile = {
            "id": profile_id,
            "name": profile_name,
            "baseUrl": provider_url,
            "home": home_rel,
            "createdAt": now_iso(),
            "lastUsedAt": None,
            "useCustomCodexCli": False,
        }
        home = codex_profile_home(profile)

    if not is_safe_api_profile_home(profile):
        print(
            "Error: refusing to create a Codex profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1

    api_key = getpass("API key: ")
    if not api_key.strip():
        print("Error: API key cannot be empty.", file=sys.stderr)
        return 1
    cleaned_api_key = clean_hidden_prefix(api_key)
    if model is None and model_catalog is None:
        try:
            discovered_models = fetch_codex_provider_models(
                provider_url,
                cleaned_api_key,
            )
            compatible_models = [
                item for item in discovered_models if is_codex_text_model(item)
            ]
            excluded_count = len(discovered_models) - len(compatible_models)
            if not compatible_models:
                raise ValueError("model discovery found no Codex-compatible text models")
            if excluded_count:
                print(
                    f"Excluded {excluded_count} non-agent model(s) from the Codex picker."
                )
            selected_model = choose_codex_provider_model(
                compatible_models,
                str((existing or {}).get("model") or "") or None,
            )
            catalog_payload = build_codex_provider_catalog(
                compatible_models,
                selected_model,
            )
            catalog_payload = merge_codex_builtin_model_metadata(
                catalog_payload,
                fetch_codex_builtin_model_catalog(profile),
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    credential_id = codex_credential_id(profile)
    SECRET_STORE.set(credential_id, cleaned_api_key)
    if catalog_payload is not None:
        install_codex_model_catalog(home, catalog_payload)
    write_codex_config(
        home,
        codex_vision_proxy_base_url(profile, upstream_base_url=provider_url),
        selected_model,
        selected_reasoning_effort,
    )
    if codex_vision_config(profile) is not None:
        configure_codex_vision_files(
            profile,
            enabled=True,
            upstream_base_url=provider_url,
        )

    profile["name"] = profile_name
    profile["baseUrl"] = provider_url
    profile["model"] = selected_model
    profile["reasoningEffort"] = selected_reasoning_effort
    if (home / "models.json").is_file():
        profile["modelCatalog"] = "models.json"
    profile["credentialId"] = credential_id
    profile["lastUsedAt"] = now_iso()
    updated = [profile if item.get("id") == profile.get("id") else item for item in profiles]
    if not any(item.get("id") == profile.get("id") for item in profiles):
        updated.append(profile)
    save_codex_profiles(updated)
    sync_codex_shared_mcp(updated)
    print(f"Saved Codex profile '{profile['name']}'.")
    return 0


def remove_codex_profile(requested: str | None = None) -> int:
    profiles = load_codex_profiles()
    if not profiles:
        show_codex_profiles(profiles)
        return 0
    if requested:
        profile = find_profile(profiles, requested)
    else:
        show_codex_profiles(profiles)
        choice = input("Remove which profile number or name: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            profile = profiles[int(choice) - 1]
        else:
            profile = find_profile(profiles, choice)
    if not profile:
        print("Error: profile was not found.", file=sys.stderr)
        return 1
    if not is_safe_api_profile_home(profile):
        print(
            "Error: refusing to remove a Codex profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1
    if input(f"Unregister '{profile['name']}'? Type YES to confirm: ") != "YES":
        print("Cancelled.")
        return 0
    save_codex_profiles([item for item in profiles if item.get("id") != profile.get("id")])
    if profile.get("credentialId"):
        SECRET_STORE.clear(profile["credentialId"])
    if profile.get("home") != ".":
        home = codex_profile_home(profile)
        if home.exists():
            CODEX_ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
            archive_id = slugify(str(profile.get("id") or profile.get("name") or "profile"))
            archive_path = CODEX_ARCHIVE_ROOT / f"{archive_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            shutil.move(str(home), str(archive_path))
            print(f"Archived profile directory to {archive_path}")
    else:
        print(f"Root profile unregistered. Existing files under {CODEX_HOME} were left in place.")
    return 0


def select_codex_profile(profiles: list[dict[str, Any]], requested: str | None) -> dict[str, Any] | None:
    if not profiles:
        if add_codex_profile() != 0:
            return None
        profiles = load_codex_profiles()
    if requested:
        profile = find_profile(profiles, requested)
        if not profile:
            print(f"Error: Codex profile '{requested}' was not found.", file=sys.stderr)
        return profile
    if len(profiles) == 1:
        return profiles[0]

    print("Choose Codex API profile")
    show_codex_profiles(profiles)
    last = sorted(profiles, key=lambda item: item.get("lastUsedAt") or "", reverse=True)[0]
    choice = input(f"Choose number or name [{last['name']}]: ").strip()
    if not choice:
        return last
    if choice.isdigit() and 1 <= int(choice) <= len(profiles):
        return profiles[int(choice) - 1]
    profile = find_profile(profiles, choice)
    if not profile:
        print(f"Error: Codex profile '{choice}' was not found.", file=sys.stderr)
    return profile


def update_codex_last_used(selected: dict[str, Any]) -> None:
    profiles = load_codex_profiles()
    for profile in profiles:
        if profile.get("id") == selected.get("id"):
            profile["lastUsedAt"] = now_iso()
    save_codex_profiles(profiles)


def configure_codex_custom_cli(requested: str | None = None) -> int:
    profiles = load_codex_profiles()
    selected = select_codex_profile(profiles, requested)
    if not selected:
        return 1
    choice = input(
        f"Use custom Codex CLI for '{selected.get('name')}'? [y/N]: "
    ).strip().casefold()
    if choice not in {"", "n", "no", "y", "yes"}:
        print("Error: enter yes or no.", file=sys.stderr)
        return 1
    use_custom = choice in {"y", "yes"}
    if use_custom:
        custom_cli = custom_codex_cli_path()
        executable = str(custom_cli) if custom_cli.is_file() else None
    else:
        executable = find_official_codex_cli_executable()
    if not executable:
        label = "custom" if use_custom else "official"
        print(
            f"Error: the {label} Codex CLI was not found.",
            file=sys.stderr,
        )
        return 1
    selected["useCustomCodexCli"] = use_custom
    save_codex_profiles(profiles)
    saved = find_profile(load_codex_profiles(), str(selected.get("id") or ""))
    if saved is None or bool(saved.get("useCustomCodexCli")) != use_custom:
        print("Error: failed to verify the Codex CLI preference.", file=sys.stderr)
        return 1
    label = "custom" if use_custom else "official"
    print(
        f"Profile '{selected.get('name')}' now uses the {label} Codex CLI: "
        f"{executable}."
    )
    return 0


def upgrade_codex() -> int:
    """Update the standalone Codex CLI through the official installer."""
    if shutil.which("pwsh"):
        powershell = "pwsh"
    elif shutil.which("powershell"):
        powershell = "powershell"
    else:
        print(
            "Error: PowerShell (pwsh or powershell) is required to update Codex.",
            file=sys.stderr,
        )
        return 1

    print("Updating Codex CLI...")
    return run_command(
        powershell,
        [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            CODEX_INSTALL_SCRIPT,
        ],
    )


def find_codex_desktop_executable() -> Path | None:
    override = clean_hidden_prefix(os.environ.get("APICODEX_DESKTOP_EXE", ""))
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None

    if os.name != "nt":
        return None

    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        return None

    script = (
        "$package = Get-AppxPackage -Name OpenAI.Codex -ErrorAction "
        "SilentlyContinue | Sort-Object Version -Descending | "
        "Select-Object -First 1; "
        "if ($package) { Join-Path $package.InstallLocation "
        "'app\\ChatGPT.exe' }"
    )
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None

    if completed.returncode != 0:
        return None
    output = completed.stdout.strip().splitlines()
    if not output:
        return None
    candidate = Path(output[-1].strip())
    return candidate if candidate.is_file() else None


def is_isolated_codex_home(home: Path) -> bool:
    resolved_home = home.resolve()
    api_root = CODEX_HOME.resolve()
    account_home = (HOME / ".codex").resolve()
    return resolved_home != account_home and resolved_home.is_relative_to(api_root)


def launch_codex_desktop(
    profiles: list[dict[str, Any]],
    selected: dict[str, Any],
) -> int:
    if os.name != "nt":
        print("Error: --desktop is currently supported only on Windows.", file=sys.stderr)
        return 1

    home = codex_profile_home(selected)
    if not is_safe_api_profile_home(selected):
        print(
            "Error: refusing to launch a desktop profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1

    desktop_exe = find_codex_desktop_executable()
    if not desktop_exe:
        print(
            "Error: the ChatGPT desktop app was not found. Install the official "
            "Windows app or set APICODEX_DESKTOP_EXE.",
            file=sys.stderr,
        )
        return 1

    repair_codex_home_images(
        home,
        label=f"API profile '{selected.get('name') or selected.get('id')}'",
        quiet=True,
    )

    if not (home / "config.toml").exists():
        write_codex_config(
            home,
            codex_vision_proxy_base_url(selected),
            selected.get("model") or DEFAULT_CODEX_MODEL,
            selected.get("reasoningEffort") or DEFAULT_CODEX_REASONING_EFFORT,
        )

    try:
        api_key = get_codex_secret(selected)
    except (KeyError, SecureStoreError):
        print(f"Profile '{selected.get('name')}' has no saved API key yet.")
        api_key = getpass("API key: ")
        if not api_key.strip():
            print("Error: API key cannot be empty.", file=sys.stderr)
            return 1
        credential_id = codex_credential_id(selected)
        SECRET_STORE.set(credential_id, clean_hidden_prefix(api_key))
        selected["credentialId"] = credential_id
        save_codex_profiles(profiles)

    if not prepare_codex_vision_runtime(selected):
        return 1
    sync_codex_shared_mcp(profiles)

    profile_id = slugify(str(selected.get("id") or selected.get("name") or "default"))
    dream_skin_id = dream_skin_instance_id(selected)
    desktop_data = CODEX_DESKTOP_DATA_ROOT / profile_id
    desktop_data.mkdir(parents=True, exist_ok=True)
    if not ensure_codex_keyring_auth(home, api_key, selected):
        return 1
    add_current_project_trust(home)
    update_codex_last_used(selected)
    print(
        f"Opening ChatGPT desktop with isolated Codex profile "
        f"'{selected.get('name')}' ({selected.get('baseUrl')})"
    )
    command = str(desktop_exe)
    args = [f"--user-data-dir={desktop_data}"]
    launch_env = {
        "CODEX_HOME": str(home),
        "APICODEX_API_KEY": clean_hidden_prefix(api_key),
    }

    def finish_launch(exit_code: int) -> int:
        if exit_code != 0:
            return exit_code
        if not label_codex_desktop_window(
            desktop_data,
            str(selected.get("name") or selected.get("id") or profile_id),
            desktop_exe,
        ):
            print(
                f"Warning: Desktop started, but the window title for "
                f"'{selected.get('name') or profile_id}' could not be labeled.",
                file=sys.stderr,
            )
        return exit_code

    dream_skin_script_value = clean_hidden_prefix(
        os.environ.get("APICODEX_DREAM_SKIN_SCRIPT", "")
    )
    if dream_skin_script_value:
        dream_skin_script = Path(dream_skin_script_value).expanduser().resolve()
        if not dream_skin_script.is_file():
            print(
                f"Error: Dream Skin launcher was not found: {dream_skin_script}",
                file=sys.stderr,
            )
            return 1
        powershell = shutil.which("pwsh") or shutil.which("powershell")
        if not powershell:
            print(
                "Error: PowerShell is required for Dream Skin desktop launch.",
                file=sys.stderr,
            )
            return 1
        port_value = clean_hidden_prefix(
            os.environ.get("APICODEX_DREAM_SKIN_PORT", "")
        )
        try:
            dream_skin_port = int(port_value)
        except ValueError:
            dream_skin_port = 0
        if dream_skin_port < 1024 or dream_skin_port > 65535:
            print(
                "Error: APICODEX_DREAM_SKIN_PORT must be between 1024 and 65535.",
                file=sys.stderr,
            )
            return 1
        command = powershell
        args = [
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(dream_skin_script),
            "-InstanceId",
            dream_skin_id,
            "-Port",
            str(dream_skin_port),
            "-ProfilePath",
            str(desktop_data),
            "-RestartExisting",
        ]
        print(
            f"Dream Skin instance '{dream_skin_id}' will use loopback port "
            f"{dream_skin_port}."
        )
        return finish_launch(
            run_command(
                command,
                args,
                env=launch_env,
                env_remove=CODEX_DESKTOP_ENV_REMOVE,
            )
        )
    return finish_launch(
        start_detached_process(
            command,
            args,
            env=launch_env,
            env_remove=CODEX_DESKTOP_ENV_REMOVE,
        )
    )


def launch_codex_vscode(
    profiles: list[dict[str, Any]],
    selected: dict[str, Any],
) -> int:
    home = codex_profile_home(selected)
    if not is_safe_api_profile_home(selected):
        print(
            "Error: refusing to launch a VS Code profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1
    if not (home / "config.toml").exists():
        write_codex_config(
            home,
            codex_vision_proxy_base_url(selected),
            selected.get("model") or DEFAULT_CODEX_MODEL,
            selected.get("reasoningEffort") or DEFAULT_CODEX_REASONING_EFFORT,
        )

    try:
        api_key = get_codex_secret(selected)
    except (KeyError, SecureStoreError):
        print(f"Profile '{selected.get('name')}' has no saved API key yet.")
        api_key = getpass("API key: ")
        if not api_key.strip():
            print("Error: API key cannot be empty.", file=sys.stderr)
            return 1
        credential_id = codex_credential_id(selected)
        SECRET_STORE.set(credential_id, clean_hidden_prefix(api_key))
        selected["credentialId"] = credential_id
        save_codex_profiles(profiles)

    if not prepare_codex_vision_runtime(selected):
        return 1
    sync_codex_shared_mcp(profiles)

    profile_id = slugify(str(selected.get("id") or selected.get("name") or "default"))
    vscode_data = CODEX_VSCODE_DATA_ROOT / profile_id
    vscode_data.mkdir(parents=True, exist_ok=True)
    add_current_project_trust(home)
    update_codex_last_used(selected)
    print(
        f"Opening VS Code with Codex profile '{selected.get('name')}' "
        f"({selected.get('baseUrl')})"
    )
    return run_command(
        "code",
        [
            "--new-window",
            "--user-data-dir",
            str(vscode_data),
            str(Path.cwd()),
        ],
        env={
            "CODEX_HOME": str(home),
            "APICODEX_API_KEY": clean_hidden_prefix(api_key),
        },
        env_remove=CODEX_PARENT_CONTEXT_ENV,
    )


def codex_help() -> None:
    print(
        """apicodex commands
  apicodex                         Select a saved API profile, then start Codex
  apicodex --api-add               Add/update a profile and choose fetched models
  apicodex --setup                 Alias for --api-add
  apicodex --api-list              List saved API profiles
  apicodex --api-list --json       List non-sensitive profile metadata as JSON
  apicodex --api-profile <name>    Start a specific API profile
  apicodex --api-remove            Unregister/archive a saved API profile
  apicodex shared enable --account Share account MCP config with API profiles
  apicodex shared sync             Refresh shared MCP config now
  apicodex shared status           Show account MCP sharing status
  apicodex shared disable          Remove managed MCP copies from API profiles
  apicodex --cus                   Choose a Profile and toggle custom Codex CLI use
  apicodex --vscode                Choose a profile and open VS Code here
  apicodex --desktop               Choose a profile and open an isolated desktop app
  apicodex --repair-images         Repair one API profile's missing history images
  apicodex --repair-images --all   Repair all API profiles (never the account home)
  apicodex --repair-images --account
                                   Repair the account home explicitly
  apicodex --repair-images --dry-run
                                   Scan and report without writing Temp or index files
  apicodex --repair-images --account --install-task
                                   Opt in to account repair at Windows logon
  apicodex --repair-images --account --uninstall-task
                                   Remove the opt-in Windows logon task
  apicodex vision setup PROFILE... Configure Gemini vision fallback per Profile
  apicodex vision status           Show configured vision fallbacks
  apicodex vision disable PROFILE... Disable a Profile's vision fallback
  apicodex share --help            Manage portable local conversation snapshots
  apicodex --up                    Update the Codex CLI
  apicodex --api-help              Show this help

Any remaining arguments are passed to codex."""
    )


def codex_share_main(args: list[str]) -> int:
    """Route only the explicit share namespace to the conversation-pool CLI."""

    from codex_share_cli import ShareContext, default_local_state_root, main

    def load_profiles_read_only() -> list[dict[str, Any]]:
        profiles, invalid = load_codex_profiles_for_image_repair()
        if invalid:
            print(
                f"Warning: ignored {invalid} unsafe or invalid API Profile(s).",
                file=sys.stderr,
            )
        return profiles

    def load_claude_nodes_read_only() -> dict[str, Any]:
        nodes = load_claude_config().get("nodes") or {}
        return nodes if isinstance(nodes, dict) else {}

    context = ShareContext(
        account_home=(HOME / ".codex").resolve(),
        api_root=CODEX_HOME.resolve(),
        local_state_root=default_local_state_root(),
        load_api_profiles=load_profiles_read_only,
        claude_account_home=(HOME / ".claude").resolve(),
        claude_nodes_root=CLAUDE_NODES_ROOT.resolve(),
        load_claude_nodes=load_claude_nodes_read_only,
    )
    return main(args, context)


def codex_main(args: list[str]) -> int:
    if args and args[0] == "vision":
        return codex_vision_main(args[1:])
    if args and args[0] == "--vision-worker":
        if len(args) != 3 or args[1] != "--api-profile":
            return 1
        return run_codex_vision_worker(args[2])
    if args and args[0] == "--vision-mcp":
        if len(args) != 3 or args[1] != "--api-profile":
            return 1
        return run_codex_vision_mcp(args[2])
    if args and args[0] == "share":
        return codex_share_main(args[1:])
    if args and args[0] == "shared":
        return codex_shared_main(args[1:])
    pass_through: list[str] = []
    requested: str | None = None
    add_name: str | None = None
    add_base_url: str | None = None
    add_model: str | None = None
    add_reasoning_effort: str | None = None
    add_model_catalog: Path | None = None
    do_add = do_list = do_remove = do_help = do_upgrade = do_vscode = False
    do_custom_cli = False
    do_desktop = do_json = False
    add_mode = any(arg in ("--api-add", "--setup") for arg in args)
    repair_mode = "--repair-images" in args
    do_repair = repair_all = repair_account = repair_dry_run = False
    repair_install_task = repair_uninstall_task = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--api-add", "--setup"):
            do_add = True
        elif arg == "--api-list":
            do_list = True
        elif arg == "--api-remove":
            do_remove = True
        elif arg == "--cus":
            do_custom_cli = True
        elif arg == "--up":
            do_upgrade = True
        elif arg == "--vscode":
            do_vscode = True
        elif arg == "--desktop":
            do_desktop = True
        elif arg == "--repair-images":
            do_repair = True
        elif repair_mode and arg == "--all":
            repair_all = True
        elif repair_mode and arg == "--account":
            repair_account = True
        elif repair_mode and arg == "--dry-run":
            repair_dry_run = True
        elif repair_mode and arg == "--install-task":
            repair_install_task = True
        elif repair_mode and arg == "--uninstall-task":
            repair_uninstall_task = True
        elif arg == "--json":
            do_json = True
        elif arg == "--api-help":
            do_help = True
        elif arg == "--api-profile":
            if i + 1 >= len(args):
                print("Error: --api-profile requires a profile name.", file=sys.stderr)
                return 1
            requested = args[i + 1]
            i += 1
        elif add_mode and arg in {
            "--name",
            "--base-url",
            "--model",
            "--reasoning-effort",
            "--model-catalog",
        }:
            if i + 1 >= len(args):
                print(f"Error: {arg} requires a value.", file=sys.stderr)
                return 1
            value = args[i + 1]
            if arg == "--name":
                add_name = value
            elif arg == "--base-url":
                add_base_url = value
            elif arg == "--model":
                add_model = value
            elif arg == "--reasoning-effort":
                add_reasoning_effort = value
            else:
                add_model_catalog = Path(value)
            i += 1
        else:
            pass_through.append(arg)
        i += 1

    if do_repair:
        if (
            do_help
            or do_add
            or do_list
            or do_remove
            or do_upgrade
            or do_vscode
            or do_desktop
            or do_custom_cli
            or do_json
        ):
            print(
                "Error: --repair-images cannot be combined with another management command.",
                file=sys.stderr,
            )
            return 1
        if pass_through:
            print(
                f"Error: unexpected repair arguments: {' '.join(pass_through)}",
                file=sys.stderr,
            )
            return 1
        if repair_all and repair_account:
            print(
                "Error: --repair-images --all and --account are mutually exclusive.",
                file=sys.stderr,
            )
            return 1
        if repair_install_task and repair_uninstall_task:
            print(
                "Error: --install-task and --uninstall-task are mutually exclusive.",
                file=sys.stderr,
            )
            return 1
        if (repair_install_task or repair_uninstall_task) and not repair_account:
            print(
                "Error: a repair task can be configured only with explicit --account.",
                file=sys.stderr,
            )
            return 1
        if (repair_install_task or repair_uninstall_task) and repair_dry_run:
            print(
                "Error: --dry-run cannot be combined with task installation or removal.",
                file=sys.stderr,
            )
            return 1
        if repair_account and requested:
            print(
                "Error: --account repair cannot select an API profile.",
                file=sys.stderr,
            )
            return 1
        if repair_all and requested:
            print(
                "Error: --all repair cannot select an individual API profile.",
                file=sys.stderr,
            )
            return 1
        if repair_account:
            # Deliberately branch before load_codex_profiles(): that routine
            # can migrate API credentials and must never run for account repair.
            if repair_install_task or repair_uninstall_task:
                return configure_account_image_repair_task(
                    install=repair_install_task,
                )
            account_home = (HOME / ".codex").resolve()
            return finish_explicit_image_repair(
                account_home,
                label="account Codex",
                dry_run=repair_dry_run,
            )

        profiles, invalid_profiles = load_codex_profiles_for_image_repair()
        result = int(bool(invalid_profiles))
        if repair_all:
            selected_profiles = profiles
        else:
            selected = select_codex_image_repair_profile(profiles, requested)
            if not selected:
                return 1
            selected_profiles = [selected]
        for profile in selected_profiles:
            home = codex_profile_home(profile)
            if not is_safe_api_profile_home(profile):
                print(
                    f"Error: refusing to repair profile outside ~/.codex-api: {home}",
                    file=sys.stderr,
                )
                result = 1
                continue
            result = max(
                result,
                finish_explicit_image_repair(
                    home,
                    label=f"API profile '{profile.get('name') or profile.get('id')}'",
                    dry_run=repair_dry_run,
                ),
            )
        return result

    if repair_all or repair_account or repair_dry_run:
        # Without --repair-images these remain ordinary Codex pass-through
        # arguments, preserving the launcher contract.
        pass

    if do_custom_cli:
        if (
            do_add
            or do_list
            or do_remove
            or do_help
            or do_upgrade
            or do_vscode
            or do_desktop
            or do_json
            or pass_through
        ):
            print(
                "Error: --cus cannot be combined with another command.",
                file=sys.stderr,
            )
            return 1
        return configure_codex_custom_cli(requested)
    if do_help:
        codex_help()
        return 0
    if do_json and not do_list:
        print("Error: --json is only supported with --api-list.", file=sys.stderr)
        return 1
    if do_upgrade:
        return upgrade_codex()
    if do_desktop and do_vscode:
        print("Error: --desktop and --vscode cannot be used together.", file=sys.stderr)
        return 1
    if do_desktop:
        if pass_through:
            print(
                f"Error: unexpected desktop arguments: {' '.join(pass_through)}",
                file=sys.stderr,
            )
            return 1
        profiles = load_codex_profiles()
        selected = select_codex_profile(profiles, requested)
        if not selected:
            return 1
        return launch_codex_desktop(profiles, selected)
    if do_vscode:
        if pass_through:
            print(
                f"Error: unexpected VS Code arguments: {' '.join(pass_through)}",
                file=sys.stderr,
            )
            return 1
        profiles = load_codex_profiles()
        selected = select_codex_profile(profiles, requested)
        if not selected:
            return 1
        return launch_codex_vscode(profiles, selected)
    if do_list:
        profiles = load_codex_profiles()
        if do_json:
            return 0 if show_codex_profiles_json(profiles) else 1
        else:
            show_codex_profiles(profiles)
        return 0
    if do_remove:
        return remove_codex_profile(requested)
    if do_add:
        code = add_codex_profile(
            requested,
            name=add_name,
            base_url=add_base_url,
            model=add_model,
            reasoning_effort=add_reasoning_effort,
            model_catalog=add_model_catalog,
        )
        if code != 0 or not pass_through:
            return code

    profiles = load_codex_profiles()
    selected = select_codex_profile(profiles, requested)
    if not selected:
        return 1
    home = codex_profile_home(selected)
    if not is_safe_api_profile_home(selected):
        print(
            "Error: refusing to run a Codex profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1
    if not (home / "config.toml").exists():
        write_codex_config(
            home,
            codex_vision_proxy_base_url(selected),
            selected.get("model") or DEFAULT_CODEX_MODEL,
            selected.get("reasoningEffort") or DEFAULT_CODEX_REASONING_EFFORT,
        )
    try:
        api_key = get_codex_secret(selected)
    except (KeyError, SecureStoreError):
        print(f"Profile '{selected.get('name')}' has no saved API key yet.")
        api_key = getpass("API key: ")
        if not api_key.strip():
            print("Error: API key cannot be empty.", file=sys.stderr)
            return 1
        credential_id = codex_credential_id(selected)
        SECRET_STORE.set(credential_id, clean_hidden_prefix(api_key))
        selected["credentialId"] = credential_id
        save_codex_profiles(profiles)

    if not prepare_codex_vision_runtime(selected):
        return 1
    sync_codex_shared_mcp(profiles)

    add_current_project_trust(home)
    update_codex_last_used(selected)
    codex_exe = find_codex_cli_executable(selected)
    if not codex_exe:
        print("Error: Codex CLI was not found.", file=sys.stderr)
        return 1
    return run_command(
        codex_exe,
        [
            "-c",
            CODEX_EPHEMERAL_AUTH_OVERRIDE,
            "--disable",
            "apps",
            "--disable",
            "plugins",
            *pass_through,
        ],
        env={
            "CODEX_HOME": str(home),
            "APICODEX_API_KEY": clean_hidden_prefix(api_key),
        },
        env_remove=CODEX_PARENT_CONTEXT_ENV,
    )


def load_claude_config() -> dict[str, Any]:
    config = read_json(CLAUDE_CONFIG_PATH, {"nodes": {}, "current": None})
    changed = migrate_claude_secrets(config, SECRET_STORE)
    if migrate_claude_proxy_settings(config):
        changed = True
    if changed:
        save_claude_config(config)
    return config


def save_claude_config(config: dict[str, Any]) -> None:
    write_json(CLAUDE_CONFIG_PATH, config)


def claude_credential_id(name: str) -> str:
    return f"claude:{name}"


def claude_desktop_bridge_credential_id(name: str) -> str:
    return f"claude-desktop-bridge:{name}"


def get_or_create_claude_desktop_bridge_token(name: str) -> str:
    credential_id = claude_desktop_bridge_credential_id(name)
    try:
        return clean_hidden_prefix(SECRET_STORE.get(credential_id))
    except KeyError:
        token = secrets.token_urlsafe(32)
        SECRET_STORE.set(credential_id, token)
        return token


def is_claude_codex_bridge(node: dict[str, Any]) -> bool:
    return node.get("type") == "codex_bridge"


def claude_node_isolation(node: dict[str, Any]) -> str:
    return "isolated" if node.get("isolation") == "isolated" else "shared"


def claude_node_proxy_enabled(node: dict[str, Any]) -> bool:
    enabled = node.get("proxy_enabled")
    if isinstance(enabled, bool):
        return enabled
    return clean_hidden_prefix(str(node.get("proxy_url") or "")).lower() != "direct"


def claude_node_proxy_url(node: dict[str, Any]) -> str:
    proxy_url = clean_hidden_prefix(str(node.get("proxy_url") or ""))
    if not proxy_url or proxy_url.lower() == "direct":
        return DEFAULT_CLAUDE_PROXY_URL
    return proxy_url


def claude_node_effective_proxy(node: dict[str, Any]) -> str:
    return claude_node_proxy_url(node) if claude_node_proxy_enabled(node) else "direct"


def add_claude_proxy_environment(env: dict[str, str], node: dict[str, Any]) -> None:
    if not claude_node_proxy_enabled(node):
        return
    proxy_url = claude_node_proxy_url(node)
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url


def claude_node_home(name: str, node: dict[str, Any]) -> Path:
    raw = node.get("home") or f"nodes/{slugify(name)}"
    return CLAUDE_NODES_ROOT / raw


def default_claude_node_home(config: dict[str, Any], name: str) -> str:
    candidate = f"nodes/{slugify(name)}"
    taken = {
        other_node.get("home")
        for other_name, other_node in (config.get("nodes") or {}).items()
        if other_name != name
    }
    if candidate in taken:
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:8]
        candidate = f"{candidate}-{digest}"
    return candidate


def is_safe_claude_node_home(name: str, node: dict[str, Any]) -> bool:
    """Reject node config dirs that could reach the account home or arbitrary state."""

    raw = node.get("home") or f"nodes/{slugify(name)}"
    if not isinstance(raw, str) or not raw:
        return False
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        return False
    home = CLAUDE_NODES_ROOT / candidate
    try:
        resolved = home.resolve()
        if resolved != home.absolute():
            return False
    except (OSError, RuntimeError, ValueError):
        return False
    account_home = (HOME / ".claude").resolve()
    return resolved != account_home and resolved.is_relative_to(CLAUDE_NODES_ROOT.resolve())


def claude_node_slug(name: str, node: dict[str, Any]) -> str:
    raw = node.get("home")
    if isinstance(raw, str) and raw:
        tail = Path(raw).name
        if tail:
            return slugify(tail)
    return slugify(name)


def normalize_claude_desktop_models(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise ValueError("desktop models must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        model = clean_hidden_prefix(str(value or "")).strip()
        if not model or not CLAUDE_DESKTOP_MODEL_RE.fullmatch(model):
            raise ValueError(f"invalid Claude Desktop model ID: {model!r}")
        if model not in seen:
            normalized.append(model)
            seen.add(model)
    return normalized


def claude_node_metadata(name: str, node: dict[str, Any]) -> dict[str, Any]:
    isolation = claude_node_isolation(node)
    if isolation == "isolated" and not is_safe_claude_node_home(name, node):
        raise ValueError(
            f"refusing to expose an unsafe Claude node home: {node.get('home')}"
        )
    if isolation == "isolated":
        config_dir = str(claude_node_home(name, node).resolve())
    else:
        config_dir = str((HOME / ".claude").resolve())
    return {
        "name": name,
        "type": str(node.get("type") or "anthropic"),
        "baseUrl": str(node.get("base_url") or ""),
        "codexProfile": node.get("codex_profile"),
        "model": node.get("model"),
        "desktopModels": normalize_claude_desktop_models(
            node.get("desktop_models")
        ),
        "proxyEnabled": claude_node_proxy_enabled(node),
        "proxyUrl": claude_node_proxy_url(node),
        "isolation": isolation,
        "configDir": config_dir,
        "vscodeData": str((CLAUDE_VSCODE_DATA_ROOT / claude_node_slug(name, node)).resolve()),
        "lastUsedAt": node.get("lastUsedAt"),
    }


def show_claude_nodes_json(config: dict[str, Any]) -> bool:
    try:
        metadata = [
            claude_node_metadata(name, node)
            for name, node in (config.get("nodes") or {}).items()
        ]
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return False
    payload = {
        "schemaVersion": 1,
        "current": config.get("current"),
        "nodes": metadata,
    }
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return True


def ensure_claude_node_home(
    config: dict[str, Any], name: str, node: dict[str, Any]
) -> Path | None:
    if not node.get("home"):
        node["home"] = default_claude_node_home(config, name)
    if not is_safe_claude_node_home(name, node):
        print(
            f"Error: refusing unsafe isolated config dir for Claude node '{name}'.",
            file=sys.stderr,
        )
        return None
    home = claude_node_home(name, node)
    home.mkdir(parents=True, exist_ok=True)
    return home


def claude_account_mcp_config_path() -> Path:
    return HOME / ".claude.json"


def claude_shared_mcp_state_path() -> Path:
    return CLAUDE_NODES_ROOT / CLAUDE_SHARED_MCP_STATE_NAME


def load_claude_shared_mcp_state() -> dict[str, Any]:
    raw = read_json(
        claude_shared_mcp_state_path(),
        {"version": 1, "accountMcpEnabled": False, "managedServers": {}},
    )
    if not isinstance(raw, dict):
        return {"version": 1, "accountMcpEnabled": False, "managedServers": {}}
    managed = raw.get("managedServers")
    raw["managedServers"] = (
        {
            str(name): str(fingerprint)
            for name, fingerprint in managed.items()
            if isinstance(name, str) and isinstance(fingerprint, str)
        }
        if isinstance(managed, dict)
        else {}
    )
    return raw


def _read_json_object_strict(path: Path, *, missing_ok: bool = False) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {}
        raise OSError(f"file was not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"failed to read JSON config '{path}': {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON config '{path}' must contain an object")
    return payload


def _claude_shared_mcp_targets(
    config: dict[str, Any],
) -> list[tuple[str, Path]]:
    targets: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for name, node in (config.get("nodes") or {}).items():
        if not isinstance(name, str) or not isinstance(node, dict):
            continue
        if claude_node_isolation(node) == "isolated":
            if not is_safe_claude_node_home(name, node):
                raise ValueError(f"unsafe isolated config dir for Claude node '{name}'")
            path = claude_node_home(name, node) / ".claude.json"
            resolved = path.resolve()
            if resolved not in seen:
                targets.append((f"node:{name}", path))
                seen.add(resolved)

        desktop_home = claude_desktop_profile_home(name, node)
        if desktop_home is None:
            continue
        desktop_path = (
            desktop_home
            / CLAUDE_DESKTOP_CODE_CONFIG_DIR_NAME
            / ".claude.json"
        )
        if desktop_path.exists():
            resolved = desktop_path.resolve()
            if resolved not in seen:
                targets.append((f"desktop:{name}", desktop_path))
                seen.add(resolved)
    return targets


def _claude_shared_mcp_backup_root() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return CLAUDE_NODES_ROOT / CLAUDE_SHARED_MCP_BACKUP_DIR / stamp


def _backup_claude_shared_mcp_configs(
    changes: list[tuple[str, Path, str | None, dict[str, Any]]],
) -> Path | None:
    if not changes:
        return None
    backup_root = _claude_shared_mcp_backup_root()
    backup_root.mkdir(parents=True, exist_ok=False)
    manifest: list[dict[str, Any]] = []
    for index, (label, config_path, existing, _updated) in enumerate(changes, 1):
        backup_path = backup_root / f"{index:02d}-{slugify(label)}.claude.json"
        write_text_atomic(backup_path, existing or "")
        manifest.append(
            {
                "target": label,
                "source": str(config_path),
                "backup": str(backup_path),
                "existed": existing is not None,
            }
        )
    write_json_atomic(
        backup_root / "manifest.json",
        {"version": 1, "createdAt": now_iso(), "files": manifest},
    )
    return backup_root


def sync_claude_shared_mcp(
    config: dict[str, Any],
    *,
    enabled: bool | None = None,
    dry_run: bool = False,
    quiet: bool = False,
) -> tuple[bool, dict[str, Any]]:
    state = load_claude_shared_mcp_state()
    was_enabled = state.get("accountMcpEnabled") is True
    desired_enabled = was_enabled if enabled is None else enabled
    report: dict[str, Any] = {
        "enabled": desired_enabled,
        "changedTargets": [],
        "conflicts": {},
        "servers": [],
        "backup": None,
        "dryRun": dry_run,
    }
    if enabled is None and not was_enabled:
        return True, report

    source_path = claude_account_mcp_config_path()
    try:
        source_payload = (
            _read_json_object_strict(source_path)
            if desired_enabled
            else {"mcpServers": {}}
        )
        previous_hashes = state.get("managedServers") or {}
        source_summary = merge_claude_shared_mcp({}, source_payload, previous_hashes)
        source_hashes = source_summary.source_hashes
        targets = _claude_shared_mcp_targets(config)
    except (OSError, ValueError) as exc:
        if not quiet:
            print(f"Error: failed to prepare shared Claude MCP sync: {exc}", file=sys.stderr)
        return False, report

    changes: list[tuple[str, Path, str | None, dict[str, Any]]] = []
    for label, config_path in targets:
        try:
            existing = (
                config_path.read_text(encoding="utf-8-sig")
                if config_path.exists()
                else None
            )
            target_payload = (
                _read_json_object_strict(config_path, missing_ok=True)
                if existing is not None
                else {}
            )
            result = merge_claude_shared_mcp(
                target_payload,
                source_payload,
                previous_hashes,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            if not quiet:
                print(f"Error: failed to inspect {config_path}: {exc}", file=sys.stderr)
            return False, report
        if result.conflicts:
            report["conflicts"][label] = list(result.conflicts)
        if result.changed:
            changes.append((label, config_path, existing, result.payload))
            report["changedTargets"].append(label)

    report["servers"] = list(source_hashes)
    next_state = {
        "version": 1,
        "accountMcpEnabled": desired_enabled,
        "source": str(source_path),
        "managedServers": source_hashes,
        "updatedAt": now_iso(),
    }
    if dry_run:
        return True, report

    backup_root: Path | None = None
    written: list[tuple[Path, str | None]] = []
    try:
        backup_root = _backup_claude_shared_mcp_configs(changes)
        for _label, config_path, existing, updated in changes:
            write_json_atomic(config_path, updated)
            written.append((config_path, existing))
        write_json_atomic(claude_shared_mcp_state_path(), next_state)
    except OSError as exc:
        for config_path, existing in reversed(written):
            try:
                if existing is None:
                    config_path.unlink(missing_ok=True)
                else:
                    write_text_atomic(config_path, existing)
            except OSError:
                pass
        if not quiet:
            print(f"Error: shared Claude MCP sync failed: {exc}", file=sys.stderr)
        return False, report
    report["backup"] = str(backup_root) if backup_root else None
    return True, report


def _print_claude_shared_mcp_report(report: dict[str, Any]) -> None:
    status = "enabled" if report.get("enabled") else "disabled"
    print(f"Account MCP sharing: {status}")
    servers = report.get("servers") or []
    print(f"Shared servers: {', '.join(servers) if servers else '(none)'}")
    changed = report.get("changedTargets") or []
    action = "Would update" if report.get("dryRun") else "Updated"
    print(f"{action} targets: {', '.join(changed) if changed else '(none)'}")
    for target, names in (report.get("conflicts") or {}).items():
        print(
            f"Conflict in '{target}' (kept target-local): {', '.join(names)}",
            file=sys.stderr,
        )
    if report.get("backup"):
        print(f"Backup: {report['backup']}")


def claude_shared_main(args: list[str]) -> int:
    action = args[0] if args else "status"
    flags = set(args[1:])
    allowed_flags = {"--account", "--dry-run"}
    if action not in {"enable", "sync", "status", "disable"} or not flags.issubset(
        allowed_flags
    ):
        print(
            "Usage: apiclaude shared <enable|sync|status|disable> "
            "[--account] [--dry-run]",
            file=sys.stderr,
        )
        return 1
    if action == "enable" and "--account" not in flags:
        print(
            "Error: shared enable requires --account to authorize reading "
            "~/.claude.json.",
            file=sys.stderr,
        )
        return 1

    state = load_claude_shared_mcp_state()
    if action == "status":
        report = {
            "enabled": state.get("accountMcpEnabled") is True,
            "servers": list((state.get("managedServers") or {}).keys()),
            "changedTargets": [],
            "conflicts": {},
            "dryRun": False,
        }
        _print_claude_shared_mcp_report(report)
        print(f"Source: {state.get('source') or claude_account_mcp_config_path()}")
        return 0
    if action == "sync" and state.get("accountMcpEnabled") is not True:
        print(
            "Error: account MCP sharing is disabled. Run "
            "'apiclaude shared enable --account' first.",
            file=sys.stderr,
        )
        return 1

    config = load_claude_config()
    requested_enabled = True if action == "enable" else False if action == "disable" else None
    ok, report = sync_claude_shared_mcp(
        config,
        enabled=requested_enabled,
        dry_run="--dry-run" in flags,
    )
    if ok:
        _print_claude_shared_mcp_report(report)
        if action == "enable" and not report.get("dryRun"):
            print(
                "Future ApiClaude isolated nodes will refresh shared MCP servers "
                "from the account user config."
            )
    return 0 if ok else 1


def ensure_claude_codex_bridge_settings(home: Path) -> None:
    settings_path = home / "settings.json"
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
        if not isinstance(settings, dict):
            raise ValueError("settings.json must contain a JSON object")
    else:
        settings = {}

    skill_overrides = settings.get("skillOverrides")
    if skill_overrides is None:
        skill_overrides = {}
    elif not isinstance(skill_overrides, dict):
        raise ValueError("settings.json skillOverrides must contain a JSON object")
    if "claude-api" in skill_overrides:
        return

    settings["skillOverrides"] = {
        **skill_overrides,
        "claude-api": "user-invocable-only",
    }
    write_json(settings_path, settings)


def migrate_claude_secrets(config: dict[str, Any], store: SecureStore) -> bool:
    changed = False
    for name, node in (config.get("nodes") or {}).items():
        token = clean_hidden_prefix(node.get("token", ""))
        if not token:
            continue
        credential_id = node.get("credential_id") or claude_credential_id(name)
        store.set(credential_id, token)
        if store.get(credential_id) != token:
            raise SecureStoreError(f"Failed to verify migrated credential '{credential_id}'")
        node["credential_id"] = credential_id
        node.pop("token", None)
        changed = True
    return changed


def migrate_claude_proxy_settings(config: dict[str, Any]) -> bool:
    changed = False
    for node in (config.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        proxy_url = claude_node_proxy_url(node)
        proxy_enabled = claude_node_proxy_enabled(node)
        if node.get("proxy_url") != proxy_url:
            node["proxy_url"] = proxy_url
            changed = True
        if node.get("proxy_enabled") is not proxy_enabled:
            node["proxy_enabled"] = proxy_enabled
            changed = True
    return changed


def get_claude_secret(name: str, node: dict[str, Any]) -> str:
    credential_id = node.get("credential_id") or claude_credential_id(name)
    return SECRET_STORE.get(credential_id)


def show_claude_nodes(config: dict[str, Any]) -> None:
    nodes = config.get("nodes") or {}
    if not nodes:
        print("No Claude API nodes saved. Use 'apiclaude add' to add one.")
        return
    current = config.get("current")
    for index, (name, node) in enumerate(nodes.items(), 1):
        marker = " [current]" if name == current else ""
        print(f"[{index}] {name}{marker}")
        if is_claude_codex_bridge(node):
            print(
                f"    Codex bridge: {node.get('codex_profile')} "
                f"(model={node.get('model')})"
            )
        else:
            print(f"    Base URL: {node.get('base_url')}")
        if claude_node_isolation(node) == "isolated":
            print(f"    Mode: isolated ({claude_node_home(name, node)})")
        else:
            print(f"    Mode: shared ({HOME / '.claude'})")
        proxy_status = "enabled" if claude_node_proxy_enabled(node) else "disabled"
        print(f"    Proxy: {proxy_status} ({claude_node_proxy_url(node)})")
        print(f"    Last used: {node.get('lastUsedAt') or '-'}")
        if is_claude_codex_bridge(node):
            print("    Token: managed by the referenced Codex profile")
        else:
            print("    Token: stored")


def add_claude_node(config: dict[str, Any], requested: str | None = None) -> int:
    print("Add or update a Claude API node")
    name = (requested or input("Node name: ")).strip()
    if not name:
        print("Error: node name cannot be empty.", file=sys.stderr)
        return 1
    existing = (config.get("nodes") or {}).get(name)
    if existing is not None and input(f"Node '{name}' exists. Overwrite? (y/N): ").strip().lower() != "y":
        print("Cancelled.")
        return 0
    base_url = clean_hidden_prefix(input("ANTHROPIC_BASE_URL: "))
    token = clean_hidden_prefix(getpass("ANTHROPIC_AUTH_TOKEN: "))
    if not base_url or not token:
        print("Error: base URL and token cannot be empty.", file=sys.stderr)
        return 1
    default_mode = claude_node_isolation(existing) if existing else "isolated"
    mode_choice = input(f"Config mode, isolated/shared [{default_mode}]: ").strip().lower()
    if not mode_choice:
        mode = default_mode
    elif mode_choice in ("isolated", "i"):
        mode = "isolated"
    elif mode_choice in ("shared", "s"):
        mode = "shared"
    else:
        print("Error: config mode must be 'isolated' or 'shared'.", file=sys.stderr)
        return 1
    default_proxy_enabled = (
        claude_node_proxy_enabled(existing) if existing is not None else True
    )
    proxy_default = "Y/n" if default_proxy_enabled else "y/N"
    proxy_choice = input(
        f"Use proxy {DEFAULT_CLAUDE_PROXY_URL}? [{proxy_default}]: "
    ).strip().lower()
    if not proxy_choice:
        proxy_enabled = default_proxy_enabled
    elif proxy_choice in ("y", "yes"):
        proxy_enabled = True
    elif proxy_choice in ("n", "no"):
        proxy_enabled = False
    else:
        print("Error: proxy choice must be 'y' or 'n'.", file=sys.stderr)
        return 1
    credential_id = claude_credential_id(name)
    SECRET_STORE.set(credential_id, token)
    node: dict[str, Any] = {
        "base_url": base_url,
        "credential_id": credential_id,
        "isolation": mode,
        "proxy_enabled": proxy_enabled,
        "proxy_url": (
            claude_node_proxy_url(existing)
            if existing is not None
            else DEFAULT_CLAUDE_PROXY_URL
        ),
        "lastUsedAt": existing.get("lastUsedAt") if existing else None,
    }
    if existing and existing.get("home"):
        node["home"] = existing["home"]
    if mode == "isolated" and not node.get("home"):
        node["home"] = default_claude_node_home(config, name)
    config.setdefault("nodes", {})[name] = node
    if not config.get("current"):
        config["current"] = name
    save_claude_config(config)
    if not sync_claude_shared_mcp(config)[0]:
        return 1
    print(f"Saved Claude node '{name}' ({mode}).")
    return 0


def add_claude_codex_bridge(
    config: dict[str, Any],
    codex_profile_name: str,
    *,
    node_name: str | None = None,
    model: str | None = None,
    cpa_executable: Path | None = None,
    proxy_url: str | None = None,
    desktop_models: list[str] | None = None,
) -> int:
    profiles = load_codex_profiles()
    profile = find_profile(profiles, clean_hidden_prefix(codex_profile_name))
    if not profile:
        print(
            f"Error: Codex profile '{codex_profile_name}' was not found.",
            file=sys.stderr,
        )
        return 1
    if not is_safe_api_profile_home(profile):
        print(
            "Error: refusing to bridge a Codex profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1

    name = clean_hidden_prefix(node_name or f"codex-{profile.get('name')}")
    if not name:
        print("Error: bridge node name cannot be empty.", file=sys.stderr)
        return 1
    existing = (config.get("nodes") or {}).get(name)
    if existing and not is_claude_codex_bridge(existing):
        print(
            f"Error: Claude node '{name}' already exists and is not a Codex bridge.",
            file=sys.stderr,
        )
        return 1

    selected_model = clean_hidden_prefix(
        model or extract_model_from_config(codex_profile_home(profile))
    )
    if not selected_model:
        print("Error: bridge model cannot be empty.", file=sys.stderr)
        return 1
    if cpa_executable is None:
        print(
            "Error: --cpa-exe is required for a new CPA bridge node.",
            file=sys.stderr,
        )
        return 1
    resolved_cpa_executable = cpa_executable.expanduser().resolve()
    if not resolved_cpa_executable.is_file():
        print(
            f"Error: CPA executable was not found: {resolved_cpa_executable}",
            file=sys.stderr,
        )
        return 1

    node: dict[str, Any] = {
        "type": "codex_bridge",
        "codex_profile": str(profile.get("id") or profile.get("name")),
        "model": selected_model,
        "gateway": "cpa",
        "cpa_executable": str(resolved_cpa_executable),
        "isolation": "isolated",
        "lastUsedAt": existing.get("lastUsedAt") if existing else None,
    }
    cleaned_proxy_url = clean_hidden_prefix(proxy_url or "")
    if cleaned_proxy_url.lower() == "direct":
        node["proxy_enabled"] = False
        node["proxy_url"] = (
            claude_node_proxy_url(existing)
            if existing is not None
            else DEFAULT_CLAUDE_PROXY_URL
        )
    elif cleaned_proxy_url:
        node["proxy_enabled"] = True
        node["proxy_url"] = cleaned_proxy_url
    else:
        node["proxy_enabled"] = (
            claude_node_proxy_enabled(existing) if existing is not None else True
        )
        node["proxy_url"] = (
            claude_node_proxy_url(existing)
            if existing is not None
            else DEFAULT_CLAUDE_PROXY_URL
        )
    requested_desktop_models = (
        desktop_models
        if desktop_models is not None
        else (existing or {}).get("desktop_models")
    )
    try:
        normalized_desktop_models = normalize_claude_desktop_models(
            requested_desktop_models
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if normalized_desktop_models:
        node["desktop_models"] = normalized_desktop_models
    if existing and existing.get("home"):
        node["home"] = existing["home"]
    else:
        node["home"] = default_claude_node_home(config, name)
    if not is_safe_claude_node_home(name, node):
        print(
            f"Error: refusing unsafe isolated config dir for bridge node '{name}'.",
            file=sys.stderr,
        )
        return 1

    config.setdefault("nodes", {})[name] = node
    if not config.get("current"):
        config["current"] = name
    save_claude_config(config)
    if not sync_claude_shared_mcp(config)[0]:
        return 1
    print(
        f"Saved isolated Claude bridge node '{name}' from Codex profile "
        f"'{profile.get('name')}' (model={selected_model}, "
        f"desktop_models={len(normalized_desktop_models)})."
    )
    return 0


def remove_claude_node(config: dict[str, Any], name: str | None) -> int:
    nodes = config.get("nodes") or {}
    if not nodes:
        show_claude_nodes(config)
        return 0
    if not name:
        show_claude_nodes(config)
        choice = input("Remove which node number or name: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(nodes):
            name = list(nodes.keys())[int(choice) - 1]
        else:
            name = choice
    if not name:
        print("Error: specify node name.", file=sys.stderr)
        return 1
    if name not in nodes:
        print(f"Error: node '{name}' was not found.", file=sys.stderr)
        return 1
    if input(f"Remove '{name}'? Type YES to confirm: ") != "YES":
        print("Cancelled.")
        return 0
    node = nodes[name]
    credential_id = node.get("credential_id") or claude_credential_id(name)
    archived_path: Path | None = None
    if claude_node_isolation(node) == "isolated":
        if not is_safe_claude_node_home(name, node):
            print(
                f"Error: refusing unsafe isolated config dir of '{name}'.",
                file=sys.stderr,
            )
            return 1
        home = claude_node_home(name, node)
        if home.exists():
            CLAUDE_ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
            archive_path = (
                CLAUDE_ARCHIVE_ROOT
                / f"{slugify(name)}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            archive_base = archive_path
            suffix = 2
            while archive_path.exists():
                archive_path = Path(f"{archive_base}-{suffix}")
                suffix += 1
            try:
                shutil.move(str(home), str(archive_path))
            except OSError as exc:
                print(
                    f"Error: failed to archive Claude node '{name}': {exc}",
                    file=sys.stderr,
                )
                return 1
            archived_path = archive_path
    del nodes[name]
    if config.get("current") == name:
        config["current"] = None
    save_claude_config(config)
    if not is_claude_codex_bridge(node):
        SECRET_STORE.clear(credential_id)
    if archived_path is not None:
        print(f"Archived node config dir to {archived_path}")
    print(f"Removed Claude node '{name}'.")
    return 0


def select_claude_node(config: dict[str, Any]) -> str | None:
    nodes = config.get("nodes") or {}
    if not nodes:
        print("No Claude API nodes saved. Use 'apiclaude add' to add one.")
        return None
    if len(nodes) == 1:
        return next(iter(nodes.keys()))
    show_claude_nodes(config)
    current = config.get("current")
    prompt = f"Choose number or name [{current}]: " if current else "Choose number or name: "
    choice = input(prompt).strip()
    if not choice and current in nodes:
        return current
    if choice.isdigit() and 1 <= int(choice) <= len(nodes):
        return list(nodes.keys())[int(choice) - 1]
    if choice in nodes:
        return choice
    print(f"Error: Claude node '{choice}' was not found.", file=sys.stderr)
    return None


def run_claude_codex_bridge_node(
    config: dict[str, Any],
    name: str,
    node: dict[str, Any],
    claude_args: list[str],
) -> int:
    profiles = load_codex_profiles()
    profile_name = str(node.get("codex_profile") or "")
    profile = find_profile(profiles, profile_name)
    if not profile:
        print(
            f"Error: bridge node '{name}' references missing Codex profile "
            f"'{profile_name}'.",
            file=sys.stderr,
        )
        return 1
    if not is_safe_api_profile_home(profile):
        print(
            f"Error: bridge node '{name}' references an unsafe Codex profile.",
            file=sys.stderr,
        )
        return 1
    try:
        api_key = clean_hidden_prefix(get_codex_secret(profile))
    except (KeyError, SecureStoreError):
        print(
            f"Error: Codex profile '{profile.get('name')}' has no saved API key.",
            file=sys.stderr,
        )
        return 1
    model = clean_hidden_prefix(str(node.get("model") or ""))
    if not model:
        print(f"Error: bridge node '{name}' has no model.", file=sys.stderr)
        return 1
    home = ensure_claude_node_home(config, name, node)
    if home is None:
        return 1
    try:
        ensure_claude_codex_bridge_settings(home)
        prepare_claude_gateway_mcp_config(home)
    except (ClaudeDesktopError, OSError, UnicodeError, ValueError) as exc:
        print(
            f"Error: failed to configure bridge node settings for '{name}': {exc}",
            file=sys.stderr,
        )
        return 1

    upstream_base_url = clean_hidden_prefix(
        str(profile.get("baseUrl") or DEFAULT_CODEX_BASE_URL)
    ).rstrip("/")
    if not ensure_codex_vision_worker(profile):
        return 1
    if codex_vision_config(profile) is not None:
        upstream_base_url = codex_vision_proxy_base_url(profile).rstrip("/")
    try:
        gateway = str(node.get("gateway") or "litellm")
        if gateway == "cpa":
            cpa_executable = clean_hidden_prefix(
                str(node.get("cpa_executable") or "")
            )
            if not cpa_executable:
                print(
                    f"Error: CPA bridge node '{name}' has no CPA executable.",
                    file=sys.stderr,
                )
                return 1
            bridge_context = cpa_bridge(
                upstream_base_url=upstream_base_url,
                upstream_api_key=api_key,
                model=model,
                cpa_executable=cpa_executable,
                proxy_url=claude_node_effective_proxy(node),
            )
        else:
            bridge_context = litellm_bridge(
                upstream_base_url=upstream_base_url,
                upstream_api_key=api_key,
                model=model,
            )
        with bridge_context as endpoint:
            env = {
                "ANTHROPIC_BASE_URL": endpoint.base_url,
                "ANTHROPIC_AUTH_TOKEN": endpoint.token,
                "ANTHROPIC_MODEL": model,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
                "CLAUDE_CODE_SUBAGENT_MODEL": model,
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
                "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING": "1",
                "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
                "CLAUDE_CONFIG_DIR": str(home),
            }
            if (
                endpoint.hosted_search_url
                and endpoint.hosted_search_token
                and endpoint.hosted_search_model
            ):
                env.update(
                    {
                        "APICLAUDE_WEB_SEARCH_BASE_URL": endpoint.hosted_search_url,
                        "APICLAUDE_WEB_SEARCH_TOKEN": endpoint.hosted_search_token,
                        "APICLAUDE_WEB_SEARCH_MODEL": endpoint.hosted_search_model,
                    }
                )
            config["current"] = name
            node["lastUsedAt"] = now_iso()
            save_claude_config(config)
            print(
                f"Using Claude bridge node '{name}' "
                f"(gateway={gateway}, Codex profile={profile.get('name')}, "
                f"model={model}, "
                f"isolated: {home})"
            )
            return run_command(
                "claude",
                claude_args,
                env=env,
                env_remove=CLAUDE_PROFILE_ENV,
            )
    except BridgeStartupError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def claude_desktop_profile_home(name: str, node: dict[str, Any]) -> Path | None:
    candidate = CLAUDE_DESKTOP_DATA_ROOT / claude_node_slug(name, node)
    try:
        resolved = candidate.resolve()
        root = CLAUDE_DESKTOP_DATA_ROOT.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if resolved != candidate.absolute() or not resolved.is_relative_to(root):
        return None
    return candidate


def resolve_claude_desktop_bridge(
    config: dict[str, Any],
    name: str,
) -> tuple[dict[str, Any], dict[str, Any], str, str, str] | None:
    node = (config.get("nodes") or {}).get(name)
    if not node:
        print(f"Error: Claude node '{name}' was not found.", file=sys.stderr)
        return None
    if not is_claude_codex_bridge(node) or node.get("gateway") != "cpa":
        print(
            "Error: --desktop currently requires a CPA-backed Codex bridge node.",
            file=sys.stderr,
        )
        return None
    profile_name = clean_hidden_prefix(str(node.get("codex_profile") or ""))
    profile = find_profile(load_codex_profiles(), profile_name)
    if not profile or not is_safe_api_profile_home(profile):
        print(
            f"Error: bridge node '{name}' references a missing or unsafe Codex profile.",
            file=sys.stderr,
        )
        return None
    model = clean_hidden_prefix(str(node.get("model") or ""))
    cpa_executable = clean_hidden_prefix(str(node.get("cpa_executable") or ""))
    if not model or not cpa_executable:
        print(f"Error: bridge node '{name}' is incomplete.", file=sys.stderr)
        return None
    upstream_base_url = clean_hidden_prefix(
        str(profile.get("baseUrl") or DEFAULT_CODEX_BASE_URL)
    ).rstrip("/")
    return node, profile, model, cpa_executable, upstream_base_url


def _redact_desktop_worker_error(message: str, *secrets_to_hide: str) -> str:
    safe = str(message)
    for value in secrets_to_hide:
        if value:
            safe = safe.replace(value, "<redacted>")
    return safe[:4000]


def run_claude_desktop_worker(
    config: dict[str, Any],
    name: str,
    *,
    port: int | None = None,
) -> int:
    if os.name != "nt":
        print("Error: Claude Desktop currently requires Windows.", file=sys.stderr)
        return 1
    profile_dir: Path | None = None
    upstream_api_key = ""
    local_token = ""
    desktop_process: subprocess.Popen[bytes] | None = None
    try:
        resolved = resolve_claude_desktop_bridge(config, name)
        if resolved is None:
            return 1
        node, profile, model, cpa_executable, upstream_base_url = resolved
        try:
            desktop_models = normalize_claude_desktop_models(
                node.get("desktop_models")
            )
        except ValueError as exc:
            raise ClaudeDesktopError(str(exc)) from exc
        profile_dir = claude_desktop_profile_home(name, node)
        if profile_dir is None:
            raise ClaudeDesktopError("refusing an unsafe Claude Desktop profile path")
        ensure_private_desktop_directory(profile_dir)
        executable = find_claude_desktop_executable()
        if executable is None:
            raise ClaudeDesktopError(
                "a healthy Claude Desktop MSIX package was not found; reinstall "
                "the official Windows app"
            )

        with desktop_instance_lock(profile_dir):
            clear_runtime_state(profile_dir)
            clear_startup_error(profile_dir)
            clear_desktop_stop_request(profile_dir)
            try:
                upstream_api_key = clean_hidden_prefix(get_codex_secret(profile))
                local_token = get_or_create_claude_desktop_bridge_token(name)
            except (KeyError, SecureStoreError) as exc:
                raise ClaudeDesktopError(f"failed to load bridge credentials: {exc}") from exc

            if not ensure_codex_vision_worker(profile):
                raise ClaudeDesktopError(
                    f"vision worker for '{profile.get('name')}' is unavailable"
                )
            if codex_vision_config(profile) is not None:
                upstream_base_url = codex_vision_proxy_base_url(profile).rstrip("/")

            with cpa_bridge(
                upstream_base_url=upstream_base_url,
                upstream_api_key=upstream_api_key,
                model=model,
                cpa_executable=cpa_executable,
                proxy_url=claude_node_effective_proxy(node),
                listen_port=port,
                local_token=local_token,
                route_model=CLAUDE_DESKTOP_GATEWAY_MODEL,
                extra_models=desktop_models,
            ) as endpoint:
                prepare_claude_desktop_profile(
                    profile_dir,
                    node_name=name,
                    gateway_base_url=endpoint.base_url,
                    local_token=local_token,
                    model=model,
                    route_model=CLAUDE_DESKTOP_GATEWAY_MODEL,
                    extra_models=desktop_models,
                )
                if not sync_claude_shared_mcp(config)[0]:
                    return 1
                desktop_process = launch_claude_desktop_process(
                    executable,
                    profile_dir,
                    web_search_base_url=endpoint.hosted_search_url,
                    web_search_token=endpoint.hosted_search_token,
                    web_search_model=endpoint.hosted_search_model,
                )
                wait_for_claude_desktop_start(desktop_process)
                endpoint_port = urlparse(endpoint.base_url).port
                if endpoint_port is None:
                    raise ClaudeDesktopError("CPA returned a gateway URL without a port")
                write_runtime_state(
                    profile_dir,
                    {
                        "schemaVersion": 1,
                        "node": name,
                        "model": model,
                        "models": [
                            CLAUDE_DESKTOP_GATEWAY_MODEL,
                            *desktop_models,
                        ],
                        "workerPid": os.getpid(),
                        "desktopPid": desktop_process.pid,
                        "port": endpoint_port,
                        "baseUrl": endpoint.base_url,
                        "userDataDir": str(profile_dir.resolve()),
                        "startedAt": now_iso(),
                    },
                )
                print(
                    f"Claude Desktop worker ready for '{name}' "
                    f"(model={model}, desktop_models={len(desktop_models)}, "
                    f"port={endpoint_port}, pid={desktop_process.pid}).",
                    flush=True,
                )
                try:
                    monitor_claude_desktop_process(desktop_process, profile_dir)
                except KeyboardInterrupt:
                    close_claude_desktop_process(desktop_process)
                return 0
    except (BridgeStartupError, ClaudeDesktopAlreadyRunning, ClaudeDesktopError, OSError) as exc:
        message = _redact_desktop_worker_error(
            str(exc),
            upstream_api_key,
            local_token,
        )
        if profile_dir is not None:
            try:
                write_startup_error(profile_dir, message)
            except OSError:
                pass
        print(f"Error: {message}", file=sys.stderr, flush=True)
        return 1
    finally:
        if desktop_process is not None and desktop_process.poll() is None:
            close_claude_desktop_process(desktop_process)
        if profile_dir is not None:
            clear_runtime_state(profile_dir, worker_pid=os.getpid())
            clear_desktop_stop_request(profile_dir)


def _spawn_claude_desktop_worker(
    profile_dir: Path,
    name: str,
    *,
    port: int | None,
) -> int:
    clear_runtime_state(profile_dir)
    clear_startup_error(profile_dir)
    clear_desktop_stop_request(profile_dir)
    log_path = worker_log_path(profile_dir)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "claude",
        "--desktop-worker",
        "--api-profile",
        name,
    ]
    if port is not None:
        command.extend(["--desktop-port", str(port)])
    environment = os.environ.copy()
    for key in (*CLAUDE_PROFILE_ENV, "OPENAI_API_KEY", "OPENAI_BASE_URL", "APICODEX_API_KEY"):
        environment.pop(key, None)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
    creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        with log_path.open("w", encoding="utf-8") as log:
            worker = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                close_fds=True,
                creationflags=creationflags,
            )
    except OSError as exc:
        print(f"Error: failed to start the Desktop worker: {exc}", file=sys.stderr)
        return 1
    state, error = wait_for_worker_start(profile_dir, worker)
    if state is None:
        print(f"Error: {error or 'Desktop worker failed to start'}", file=sys.stderr)
        print(f"Worker log: {log_path}", file=sys.stderr)
        return 1
    print(
        f"Opened isolated Claude Desktop node '{name}' "
        f"(model={state.get('model')}, port={state.get('port')}, "
        f"pid={state.get('desktopPid')})."
    )
    print(f"User data: {state.get('userDataDir')}")
    return 0


def launch_claude_desktop_bridge(
    config: dict[str, Any],
    name: str,
    *,
    port: int | None = None,
    foreground: bool = False,
) -> int:
    if os.name != "nt":
        print("Error: Claude Desktop currently requires Windows.", file=sys.stderr)
        return 1
    if port is not None and not 1 <= port <= 65535:
        print("Error: --desktop-port must be between 1 and 65535.", file=sys.stderr)
        return 1
    resolved = resolve_claude_desktop_bridge(config, name)
    if resolved is None:
        return 1
    node, _profile, _model, _cpa_executable, _upstream_base_url = resolved
    profile_dir = claude_desktop_profile_home(name, node)
    if profile_dir is None:
        print("Error: refusing an unsafe Claude Desktop profile path.", file=sys.stderr)
        return 1
    state = read_runtime_state(profile_dir)
    if runtime_is_active(state):
        print(
            f"Claude Desktop node '{name}' is already running "
            f"(pid={state.get('desktopPid')}, port={state.get('port')})."
        )
        return 0
    try:
        ensure_private_desktop_directory(profile_dir)
    except (ClaudeDesktopError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if find_claude_desktop_executable() is None:
        print(
            "Error: a healthy Claude Desktop MSIX package was not found. "
            "Install or repair the official Windows app first.",
            file=sys.stderr,
        )
        return 1
    config["current"] = name
    node["lastUsedAt"] = now_iso()
    save_claude_config(config)
    if foreground:
        print(
            f"Starting Claude Desktop node '{name}' in foreground debug mode. "
            "Closing its window will stop the bridge."
        )
        return run_claude_desktop_worker(config, name, port=port)
    return _spawn_claude_desktop_worker(profile_dir, name, port=port)


def show_claude_desktop_status(
    config: dict[str, Any],
    name: str | None = None,
) -> int:
    nodes = config.get("nodes") or {}
    selected = [name] if name else [
        node_name
        for node_name, node in nodes.items()
        if is_claude_codex_bridge(node) and node.get("gateway") == "cpa"
    ]
    if name and name not in nodes:
        print(f"Error: Claude node '{name}' was not found.", file=sys.stderr)
        return 1
    if name:
        requested_node = nodes.get(name) or {}
        if (
            not is_claude_codex_bridge(requested_node)
            or requested_node.get("gateway") != "cpa"
        ):
            print(
                f"Error: Claude node '{name}' is not a CPA Desktop bridge node.",
                file=sys.stderr,
            )
            return 1
    print("Claude Desktop instances")
    print("NAME                 STATE     MODEL                 PID      PORT")
    for node_name in selected:
        node = nodes.get(node_name) or {}
        profile_dir = claude_desktop_profile_home(node_name, node)
        state = read_runtime_state(profile_dir) if profile_dir else None
        if runtime_is_active(state):
            status = "running"
        elif state:
            status = "stale"
        else:
            status = "stopped"
        model = str((state or {}).get("model") or node.get("model") or "-")
        pid = str((state or {}).get("desktopPid") or "-")
        runtime_port = str((state or {}).get("port") or "-")
        print(
            f"{node_name[:20]:20} {status:9} {model[:21]:21} "
            f"{pid:8} {runtime_port}"
        )
    return 0


def stop_claude_desktop(
    config: dict[str, Any],
    name: str | None,
    *,
    timeout: float = 15.0,
) -> int:
    selected = name or config.get("current")
    node = (config.get("nodes") or {}).get(selected) if selected else None
    if not selected or not node:
        print("Error: select a Claude Desktop bridge node.", file=sys.stderr)
        return 1
    if not is_claude_codex_bridge(node) or node.get("gateway") != "cpa":
        print(
            f"Error: Claude node '{selected}' is not a CPA Desktop bridge node.",
            file=sys.stderr,
        )
        return 1
    profile_dir = claude_desktop_profile_home(str(selected), node)
    if profile_dir is None:
        print("Error: refusing an unsafe Claude Desktop profile path.", file=sys.stderr)
        return 1
    state = read_runtime_state(profile_dir)
    if not runtime_is_active(state):
        print(f"Claude Desktop node '{selected}' is not running.")
        return 0
    worker_pid = int(state.get("workerPid") or 0)
    request_desktop_stop(profile_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_running(worker_pid) or not read_runtime_state(profile_dir):
            print(f"Stopped Claude Desktop node '{selected}'.")
            return 0
        time.sleep(0.1)
    print(
        f"Error: Claude Desktop node '{selected}' did not stop within "
        f"{timeout:.0f} seconds.",
        file=sys.stderr,
    )
    return 1


def show_claude_desktop_bridge_token(config: dict[str, Any], name: str | None) -> int:
    selected = name or config.get("current")
    node = (config.get("nodes") or {}).get(selected) if selected else None
    if not selected or not node or not is_claude_codex_bridge(node):
        print("Error: select a Codex bridge node.", file=sys.stderr)
        return 1
    try:
        print(get_or_create_claude_desktop_bridge_token(str(selected)))
    except SecureStoreError as exc:
        print(f"Error: failed to load desktop bridge token: {exc}", file=sys.stderr)
        return 1
    return 0


def run_claude_node(config: dict[str, Any], name: str, claude_args: list[str]) -> int:
    node = config.get("nodes", {}).get(name)
    if not node:
        print(f"Error: Claude node '{name}' was not found.", file=sys.stderr)
        return 1
    if not sync_claude_shared_mcp(config)[0]:
        return 1
    if is_claude_codex_bridge(node):
        return run_claude_codex_bridge_node(config, name, node, claude_args)
    env = {
        "ANTHROPIC_BASE_URL": clean_hidden_prefix(node.get("base_url", "")),
        "ANTHROPIC_AUTH_TOKEN": get_claude_secret(name, node),
    }
    add_claude_proxy_environment(env, node)
    if claude_node_isolation(node) == "isolated":
        home = ensure_claude_node_home(config, name, node)
        if home is None:
            return 1
        env["CLAUDE_CONFIG_DIR"] = str(home)
        mode_label = f"isolated: {home}"
    else:
        mode_label = f"shared: {HOME / '.claude'}"
    config["current"] = name
    node["lastUsedAt"] = now_iso()
    save_claude_config(config)
    print(f"Using Claude node '{name}' ({env['ANTHROPIC_BASE_URL']}, {mode_label})")
    return run_command(
        "claude",
        claude_args,
        env=env,
        env_remove=CLAUDE_PROFILE_ENV,
    )


def launch_claude_vscode(config: dict[str, Any], name: str) -> int:
    node = (config.get("nodes") or {}).get(name)
    if not node:
        print(f"Error: Claude node '{name}' was not found.", file=sys.stderr)
        return 1
    if not sync_claude_shared_mcp(config)[0]:
        return 1
    if is_claude_codex_bridge(node):
        print(
            "Error: Codex bridge nodes are a CLI prototype; VS Code needs a "
            "persistent gateway lifecycle and is not enabled yet.",
            file=sys.stderr,
        )
        return 1
    try:
        token = get_claude_secret(name, node)
    except (KeyError, SecureStoreError):
        print(f"Error: Claude node '{name}' has no saved token.", file=sys.stderr)
        return 1

    env = {
        "ANTHROPIC_BASE_URL": clean_hidden_prefix(node.get("base_url", "")),
        "ANTHROPIC_AUTH_TOKEN": clean_hidden_prefix(token),
    }
    add_claude_proxy_environment(env, node)
    if claude_node_isolation(node) == "isolated":
        home = ensure_claude_node_home(config, name, node)
        if home is None:
            return 1
        env["CLAUDE_CONFIG_DIR"] = str(home)

    vscode_data = CLAUDE_VSCODE_DATA_ROOT / claude_node_slug(name, node)
    vscode_data.mkdir(parents=True, exist_ok=True)
    config["current"] = name
    node["lastUsedAt"] = now_iso()
    save_claude_config(config)
    print(
        f"Opening VS Code with Claude node '{name}' "
        f"({env['ANTHROPIC_BASE_URL']}, {claude_node_isolation(node)})"
    )
    return run_command(
        "code",
        [
            "--new-window",
            "--user-data-dir",
            str(vscode_data),
            str(Path.cwd()),
        ],
        env=env,
        env_remove=CLAUDE_PROFILE_ENV,
    )


def upgrade_claude() -> int:
    """Update Claude Code through its official CLI command."""

    print("Updating Claude Code...")
    return run_command(
        "claude",
        ["update"],
        env_remove=CLAUDE_PROFILE_ENV,
    )


def set_claude_node_mode(config: dict[str, Any], name: str, requested: str | None) -> int:
    node = (config.get("nodes") or {}).get(name)
    if not node:
        print(f"Error: Claude node '{name}' was not found.", file=sys.stderr)
        return 1
    current_mode = claude_node_isolation(node)
    if requested is None:
        if current_mode == "isolated":
            print(f"Node '{name}' mode: isolated ({claude_node_home(name, node)})")
        else:
            print(f"Node '{name}' mode: shared ({HOME / '.claude'})")
        return 0
    value = requested.strip().lower()
    if value not in ("isolated", "shared"):
        print("Error: mode must be 'isolated' or 'shared'.", file=sys.stderr)
        return 1
    if value == current_mode:
        print(f"Node '{name}' is already {value}.")
        return 0
    if value == "isolated":
        home = ensure_claude_node_home(config, name, node)
        if home is None:
            return 1
        node["isolation"] = "isolated"
        save_claude_config(config)
        if not sync_claude_shared_mcp(config)[0]:
            return 1
        print(f"Node '{name}' switched to isolated (CLAUDE_CONFIG_DIR={home}).")
        print(
            "Note: an empty isolated dir starts fresh (onboarding, trust prompts). "
            f"History under {HOME / '.claude'} is not moved."
        )
    else:
        node["isolation"] = "shared"
        save_claude_config(config)
        print(f"Node '{name}' switched to shared ({HOME / '.claude'}).")
        print(
            f"Note: isolated data is kept at {claude_node_home(name, node)} "
            "for switching back."
        )
    return 0


def set_claude_node_proxy(config: dict[str, Any], name: str) -> int:
    node = (config.get("nodes") or {}).get(name)
    if not node:
        print(f"Error: Claude node '{name}' was not found.", file=sys.stderr)
        return 1
    enabled = claude_node_proxy_enabled(node)
    proxy_url = claude_node_proxy_url(node)
    current = "enabled" if enabled else "disabled"
    print(f"Node '{name}' proxy: {current} ({proxy_url})")
    default = "Y/n" if enabled else "y/N"
    choice = input(f"Enable proxy for '{name}'? [{default}]: ").strip().lower()
    if not choice:
        requested = enabled
    elif choice in ("y", "yes"):
        requested = True
    elif choice in ("n", "no"):
        requested = False
    else:
        print("Error: proxy choice must be 'y' or 'n'.", file=sys.stderr)
        return 1
    node["proxy_enabled"] = requested
    node["proxy_url"] = proxy_url
    save_claude_config(config)
    status = "enabled" if requested else "disabled"
    print(f"Node '{name}' proxy {status}: {proxy_url}")
    return 0


def claude_help() -> None:
    print(
        """apiclaude commands
  apiclaude                        Select a saved API node, then start Claude Code
  apiclaude --api-add              Add or update an API node
  apiclaude --setup                Alias for --api-add
  apiclaude --api-list             List saved API nodes
  apiclaude --api-list --json      List non-sensitive node metadata as JSON
  apiclaude --api-profile <name>   Start a specific API node
  apiclaude --yolo                 Start with all permission checks bypassed
  apiclaude --api-remove           Unregister/archive a saved API node
  apiclaude --proxy                Choose a node and enable/disable its proxy
  apiclaude --proxy --api-profile <name>
                                   Configure the proxy for a specific node
  apiclaude --vscode               Choose a node and open VS Code here
  apiclaude --vscode --api-profile <name>
                                   Open VS Code with a specific node
  apiclaude --desktop [--api-profile <bridge-node>]
             [--desktop-port PORT] Open an isolated Claude Desktop instance
  apiclaude --desktop-foreground [--api-profile <bridge-node>]
             [--desktop-port PORT] Run the Desktop bridge visibly for debugging
  apiclaude --desktop-status [--api-profile <bridge-node>]
                                   Show isolated Desktop instance status
  apiclaude --desktop-stop [--api-profile <bridge-node>]
                                   Stop one isolated Desktop instance
  apiclaude desktop-token [NODE]   Print the securely stored local gateway token
  apiclaude --up                   Update Claude Code
  apiclaude --api-help             Show this help
  apiclaude shared enable --account Share account user MCP with isolated nodes
  apiclaude shared sync             Refresh shared MCP config now
  apiclaude shared status           Show account MCP sharing status
  apiclaude shared disable          Remove managed MCP copies from isolated nodes
  apiclaude mode NAME [MODE]       Show or switch a node between isolated/shared
                                   isolated: node-scoped CLAUDE_CONFIG_DIR
                                   shared:   default ~/.claude (legacy behavior)
  apiclaude bridge CODEX_PROFILE [--name NODE] [--model MODEL]
                   --cpa-exe PATH [--proxy-url URL]
                   [--desktop-model MODEL]...
                                   Create/update an isolated CLI prototype node
                                   backed by an existing Codex API profile via CPA

Legacy subcommands remain available: add, list [--json], current,
remove [NAME], proxy [NAME], run [ARGS], vscode [NAME], update, help.

Any remaining arguments are passed to Claude Code."""
    )


def claude_bridge_main(args: list[str]) -> int:
    if not args or args[0] in ("-h", "--help"):
        print(
            "Usage: apiclaude bridge CODEX_PROFILE "
            "[--name NODE] [--model MODEL] --cpa-exe PATH "
            "[--proxy-url URL] [--desktop-model MODEL]..."
        )
        return 0 if args else 1

    codex_profile = args[0]
    node_name: str | None = None
    model: str | None = None
    cpa_executable: Path | None = None
    proxy_url: str | None = None
    desktop_models: list[str] = []
    i = 1
    while i < len(args):
        option = args[i]
        if option not in (
            "--name",
            "--model",
            "--cpa-exe",
            "--proxy-url",
            "--desktop-model",
        ):
            print(f"Error: unexpected bridge argument: {option}", file=sys.stderr)
            return 1
        if i + 1 >= len(args):
            print(f"Error: {option} requires a value.", file=sys.stderr)
            return 1
        value = args[i + 1]
        if option == "--name":
            node_name = value
        elif option == "--model":
            model = value
        elif option == "--cpa-exe":
            cpa_executable = Path(value)
        elif option == "--proxy-url":
            proxy_url = value
        else:
            desktop_models.append(value)
        i += 2

    config = load_claude_config()
    return add_claude_codex_bridge(
        config,
        codex_profile,
        node_name=node_name,
        model=model,
        cpa_executable=cpa_executable,
        proxy_url=proxy_url,
        desktop_models=desktop_models or None,
    )


def claude_desktop_worker_main(args: list[str]) -> int:
    requested: str | None = None
    port: int | None = None
    i = 0
    while i < len(args):
        option = args[i]
        if option == "--api-profile" and i + 1 < len(args):
            requested = args[i + 1]
            i += 1
        elif option == "--desktop-port" and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print("Error: --desktop-port must be an integer.", file=sys.stderr)
                return 1
            i += 1
        else:
            print(f"Error: unexpected Desktop worker argument: {option}", file=sys.stderr)
            return 1
        i += 1
    if not requested:
        print("Error: Desktop worker requires --api-profile.", file=sys.stderr)
        return 1
    if port is not None and not 1 <= port <= 65535:
        print("Error: --desktop-port must be between 1 and 65535.", file=sys.stderr)
        return 1
    return run_claude_desktop_worker(load_claude_config(), requested, port=port)


def claude_legacy_main(args: list[str]) -> int:
    """Dispatch the original subcommand style (add/list/current/...)."""

    config = load_claude_config()
    command = args[0]
    if command == "add":
        return add_claude_node(config)
    if command == "list":
        if args[1:] == ["--json"]:
            return 0 if show_claude_nodes_json(config) else 1
        if len(args) != 1:
            print("Error: usage: apiclaude list [--json].", file=sys.stderr)
            return 1
        show_claude_nodes(config)
        return 0
    if command == "vscode":
        if len(args) > 2:
            print("Error: usage: apiclaude vscode [NAME].", file=sys.stderr)
            return 1
        selected = args[1] if len(args) == 2 else select_claude_node(config)
        return launch_claude_vscode(config, selected) if selected else 1
    if command == "current":
        current = config.get("current")
        if not current:
            print("No current Claude node.")
            return 0
        node = (config.get("nodes") or {}).get(current)
        if not node:
            print(f"Current node '{current}' no longer exists.")
            return 1
        print(f"Current Claude node: {current}")
        if is_claude_codex_bridge(node):
            print(
                f"Codex bridge: profile={node.get('codex_profile')} "
                f"model={node.get('model')}"
            )
        else:
            print(f"ANTHROPIC_BASE_URL={node.get('base_url')}")
        if claude_node_isolation(node) == "isolated":
            print(f"CLAUDE_CONFIG_DIR={claude_node_home(current, node)} (isolated)")
        else:
            print(f"Config dir: {HOME / '.claude'} (shared)")
        proxy_status = "enabled" if claude_node_proxy_enabled(node) else "disabled"
        print(f"Proxy: {proxy_status} ({claude_node_proxy_url(node)})")
        if is_claude_codex_bridge(node):
            print("ANTHROPIC_AUTH_TOKEN=<ephemeral local bridge token>")
        else:
            print("ANTHROPIC_AUTH_TOKEN=<stored>")
        return 0
    if command == "mode":
        if len(args) < 2:
            show_claude_nodes(config)
            return 0
        return set_claude_node_mode(config, args[1], args[2] if len(args) > 2 else None)
    if command == "proxy":
        if len(args) > 2:
            print("Error: usage: apiclaude proxy [NAME].", file=sys.stderr)
            return 1
        selected = args[1] if len(args) == 2 else select_claude_node(config)
        return set_claude_node_proxy(config, selected) if selected else 1
    if command == "remove":
        return remove_claude_node(config, args[1] if len(args) > 1 else None)
    # command == "run"
    current = config.get("current")
    if not current:
        print("No current Claude node. Use 'apiclaude add' or 'apiclaude'.", file=sys.stderr)
        return 1
    return run_claude_node(config, current, args[1:])


def claude_main(args: list[str]) -> int:
    if args and args[0] == "--desktop-worker":
        return claude_desktop_worker_main(args[1:])
    if args and args[0] == "shared":
        return claude_shared_main(args[1:])
    if args and args[0] in ("--up", "update"):
        if len(args) != 1:
            print("Error: the update command does not accept arguments.", file=sys.stderr)
            return 1
        return upgrade_claude()
    if args and args[0] in ("help", "-h", "--help", "--api-help"):
        claude_help()
        return 0
    if args and args[0] == "bridge":
        return claude_bridge_main(args[1:])
    if args and args[0] == "desktop-token":
        if len(args) > 2:
            print("Error: usage: apiclaude desktop-token [NODE].", file=sys.stderr)
            return 1
        return show_claude_desktop_bridge_token(
            load_claude_config(),
            args[1] if len(args) == 2 else None,
        )
    if args and args[0] in (
        "add",
        "list",
        "current",
        "mode",
        "proxy",
        "remove",
        "run",
        "vscode",
    ):
        return claude_legacy_main(args)

    # Codex-style flag parsing, mirroring codex_main.
    pass_through: list[str] = []
    requested: str | None = None
    do_add = do_list = do_remove = do_proxy = do_vscode = do_desktop = do_json = False
    do_desktop_foreground = do_desktop_status = do_desktop_stop = False
    desktop_port: int | None = None
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--api-add", "--setup"):
            do_add = True
        elif arg == "--api-list":
            do_list = True
        elif arg == "--api-remove":
            do_remove = True
        elif arg == "--proxy":
            do_proxy = True
        elif arg == "--vscode":
            do_vscode = True
        elif arg == "--desktop":
            do_desktop = True
        elif arg == "--desktop-foreground":
            do_desktop_foreground = True
        elif arg == "--desktop-status":
            do_desktop_status = True
        elif arg == "--desktop-stop":
            do_desktop_stop = True
        elif arg == "--desktop-port":
            if i + 1 >= len(args):
                print("Error: --desktop-port requires a value.", file=sys.stderr)
                return 1
            try:
                desktop_port = int(args[i + 1])
            except ValueError:
                print("Error: --desktop-port must be an integer.", file=sys.stderr)
                return 1
            i += 1
        elif arg == "--json":
            do_json = True
        elif arg == "--api-profile":
            if i + 1 >= len(args):
                print("Error: --api-profile requires a node name.", file=sys.stderr)
                return 1
            requested = args[i + 1]
            i += 1
        elif arg == "--yolo":
            pass_through.extend(["--permission-mode", "bypassPermissions"])
        else:
            pass_through.append(arg)
        i += 1

    if do_json and not do_list:
        print("Error: --json is only supported with --api-list.", file=sys.stderr)
        return 1

    desktop_actions = sum(
        bool(value)
        for value in (
            do_desktop,
            do_desktop_foreground,
            do_desktop_status,
            do_desktop_stop,
        )
    )
    if desktop_actions > 1:
        print("Error: choose only one Desktop action.", file=sys.stderr)
        return 1
    if desktop_port is not None and not (do_desktop or do_desktop_foreground):
        print(
            "Error: --desktop-port requires --desktop or --desktop-foreground.",
            file=sys.stderr,
        )
        return 1

    config = load_claude_config()
    if do_desktop or do_desktop_foreground:
        if do_vscode or do_add or do_list or do_remove or do_proxy or do_json or pass_through:
            print(
                "Error: a Desktop launch cannot be combined with other actions or "
                "Claude Code arguments.",
                file=sys.stderr,
            )
            return 1
        selected = requested or select_claude_node(config)
        if not selected:
            return 1
        if do_desktop_foreground:
            return launch_claude_desktop_bridge(
                config,
                selected,
                port=desktop_port,
                foreground=True,
            )
        return launch_claude_desktop_bridge(
            config,
            selected,
            port=desktop_port,
        )
    if do_desktop_status:
        if do_vscode or do_add or do_list or do_remove or do_proxy or do_json or pass_through:
            print("Error: --desktop-status cannot be combined with other actions.", file=sys.stderr)
            return 1
        return show_claude_desktop_status(config, requested)
    if do_desktop_stop:
        if do_vscode or do_add or do_list or do_remove or do_proxy or do_json or pass_through:
            print("Error: --desktop-stop cannot be combined with other actions.", file=sys.stderr)
            return 1
        return stop_claude_desktop(config, requested)
    if do_proxy:
        if do_vscode or do_add or do_list or do_remove or do_json or pass_through:
            print("Error: --proxy cannot be combined with other actions.", file=sys.stderr)
            return 1
        selected = requested or select_claude_node(config)
        return set_claude_node_proxy(config, selected) if selected else 1
    if do_vscode:
        if pass_through:
            print(
                f"Error: unexpected VS Code arguments: {' '.join(pass_through)}",
                file=sys.stderr,
            )
            return 1
        selected = requested or select_claude_node(config)
        return launch_claude_vscode(config, selected) if selected else 1
    if do_list:
        if do_json:
            return 0 if show_claude_nodes_json(config) else 1
        show_claude_nodes(config)
        return 0
    if do_remove:
        return remove_claude_node(config, requested)
    if do_add:
        code = add_claude_node(config, requested)
        if code != 0 or not pass_through:
            return code
    if requested:
        return run_claude_node(config, requested, pass_through)
    selected = select_claude_node(config)
    return run_claude_node(config, selected, pass_through) if selected else 1


def apiagent_help() -> None:
    print(
        """apiagent commands
  apiagent codex [ARGS]     Run/manage Codex API profiles
  apiagent claude [ARGS]    Run/manage Claude API nodes
  apiagent list             List both Codex profiles and Claude nodes
  apiagent help             Show this help

Shortcuts:
  apicodex [ARGS]
  apiclaude [ARGS]"""
    )


def apiagent_main(args: list[str]) -> int:
    if not args or args[0] in ("help", "-h", "--help"):
        apiagent_help()
        return 0
    target = args[0].lower()
    rest = args[1:]
    if target in ("codex", "c"):
        return codex_main(rest)
    if target in ("claude", "cl"):
        return claude_main(rest)
    if target in ("list", "ls"):
        print("Codex API profiles")
        show_codex_profiles(load_codex_profiles())
        print("\nClaude API nodes")
        show_claude_nodes(load_claude_config())
        return 0
    print(f"Unknown apiagent target: {args[0]}", file=sys.stderr)
    apiagent_help()
    return 1


def main() -> int:
    invoked = Path(sys.argv[0]).stem.lower()
    if invoked == "apicodex":
        return codex_main(sys.argv[1:])
    if invoked == "apiclaude":
        return claude_main(sys.argv[1:])
    return apiagent_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
