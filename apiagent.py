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

import json
import hashlib
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any

from codex_history_images import (
    RepairReport,
    default_state_root,
    repair_codex_history_images,
)
from codex_desktop_windows import label_codex_desktop_window
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
SECRET_STORE = SecureStore(HOME / ".apiagent-secrets")
DEFAULT_CODEX_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_REASONING_EFFORT = "high"
CODEX_API_AUTH_MARKER = "apicodex-managed-key-in-child-environment"
CODEX_AUTH_STORE = "keyring"
CODEX_INSTALL_SCRIPT = "$env:CODEX_NON_INTERACTIVE=1; irm https://chatgpt.com/codex/install.ps1 | iex"
CODEX_ACCOUNT_IMAGE_REPAIR_TASK = "ApiCodex Account History Image Repair"
HIDDEN_PREFIX_CHARS = "\ufeff\u200b\u200c\u200d\u2060\ufffd"
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
    "CLAUDE_CONFIG_DIR",
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
    if migrate_codex_secrets(profiles, SECRET_STORE):
        save_codex_profiles(profiles)
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
    config = f'''model = "{model}"
model_provider = "apicodex"
model_reasoning_effort = "{reasoning_effort}"
cli_auth_credentials_store = "keyring"

[windows]
sandbox = "unelevated"

[shell_environment_policy]
exclude = ["APICODEX_API_KEY"]

[features]
apps = false
plugins = false

[model_providers.apicodex]
name = "API Codex"
base_url = "{base_url}"
wire_api = "responses"
env_key = "APICODEX_API_KEY"
requires_openai_auth = false

[desktop]
conversationDetailMode = "STEPS_COMMANDS"
'''
    (home / "config.toml").write_text(config, encoding="utf-8")


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


def ensure_codex_keyring_auth(home: Path, api_key: str) -> bool:
    """Sync one API profile's key into the official Codex keyring."""
    if os.name != "nt":
        print("Error: Codex keyring authentication requires Windows.", file=sys.stderr)
        return False
    prepare_codex_desktop_profile(home, {})
    code = run_command(
        "codex",
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


def add_codex_profile(requested: str | None = None) -> int:
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
    prompt = f"Profile name [{default_name}]: " if default_name else "Profile name: "
    name = input(prompt).strip() or default_name
    if not name:
        name = "default" if not profiles else ""
    if not name:
        print("Error: profile name cannot be empty.", file=sys.stderr)
        return 1

    existing = selected or find_profile(profiles, name)
    conflict = find_profile(profiles, name)
    if selected and conflict and conflict.get("id") != selected.get("id"):
        print(f"Error: profile name '{name}' is already in use.", file=sys.stderr)
        return 1
    if existing and not is_safe_api_profile_home(existing):
        print(
            "Error: refusing to update a Codex profile outside ~/.codex-api.",
            file=sys.stderr,
        )
        return 1
    default_url = existing.get("baseUrl") if existing else DEFAULT_CODEX_BASE_URL
    base_url = clean_hidden_prefix(input(f"API base URL [{default_url}]: ") or default_url).rstrip("/")

    if existing:
        profile = existing
        home = codex_profile_home(profile)
    else:
        base_id = slugify(name)
        profile_id = base_id
        n = 2
        while find_profile(profiles, profile_id):
            profile_id = f"{base_id}-{n}"
            n += 1
        home_rel = "." if not profiles and not (CODEX_HOME / "auth.json").exists() else str(Path("profiles") / profile_id)
        profile = {
            "id": profile_id,
            "name": name,
            "baseUrl": base_url,
            "home": home_rel,
            "createdAt": now_iso(),
            "lastUsedAt": None,
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
    credential_id = codex_credential_id(profile)
    SECRET_STORE.set(credential_id, clean_hidden_prefix(api_key))
    write_codex_config(
        home,
        base_url,
        profile.get("model") or DEFAULT_CODEX_MODEL,
        profile.get("reasoningEffort") or DEFAULT_CODEX_REASONING_EFFORT,
    )

    profile["name"] = name
    profile["baseUrl"] = base_url
    profile["credentialId"] = credential_id
    profile["lastUsedAt"] = now_iso()
    updated = [profile if item.get("id") == profile.get("id") else item for item in profiles]
    if not any(item.get("id") == profile.get("id") for item in profiles):
        updated.append(profile)
    save_codex_profiles(updated)
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
            selected.get("baseUrl", DEFAULT_CODEX_BASE_URL),
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

    profile_id = slugify(str(selected.get("id") or selected.get("name") or "default"))
    dream_skin_id = dream_skin_instance_id(selected)
    desktop_data = CODEX_DESKTOP_DATA_ROOT / profile_id
    desktop_data.mkdir(parents=True, exist_ok=True)
    if not ensure_codex_keyring_auth(home, api_key):
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
            selected.get("baseUrl", DEFAULT_CODEX_BASE_URL),
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
  apicodex --api-add               Add or update an API profile
  apicodex --setup                 Alias for --api-add
  apicodex --api-list              List saved API profiles
  apicodex --api-list --json       List non-sensitive profile metadata as JSON
  apicodex --api-profile <name>    Start a specific API profile
  apicodex --api-remove            Unregister/archive a saved API profile
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

    context = ShareContext(
        account_home=(HOME / ".codex").resolve(),
        api_root=CODEX_HOME.resolve(),
        local_state_root=default_local_state_root(),
        load_api_profiles=load_profiles_read_only,
    )
    return main(args, context)


def codex_main(args: list[str]) -> int:
    if args and args[0] == "share":
        return codex_share_main(args[1:])
    pass_through: list[str] = []
    requested: str | None = None
    do_add = do_list = do_remove = do_help = do_upgrade = do_vscode = False
    do_desktop = do_json = False
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
        else:
            pass_through.append(arg)
        i += 1

    if do_repair:
        if do_help or do_add or do_list or do_remove or do_upgrade or do_vscode or do_desktop or do_json:
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
        code = add_codex_profile(requested)
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
            selected.get("baseUrl", DEFAULT_CODEX_BASE_URL),
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

    add_current_project_trust(home)
    update_codex_last_used(selected)
    return run_command(
        "codex",
        ["--disable", "apps", "--disable", "plugins", *pass_through],
        env={
            "CODEX_HOME": str(home),
            "APICODEX_API_KEY": clean_hidden_prefix(api_key),
        },
        env_remove=CODEX_PARENT_CONTEXT_ENV,
    )


def load_claude_config() -> dict[str, Any]:
    config = read_json(CLAUDE_CONFIG_PATH, {"nodes": {}, "current": None})
    if migrate_claude_secrets(config, SECRET_STORE):
        save_claude_config(config)
    return config


def save_claude_config(config: dict[str, Any]) -> None:
    write_json(CLAUDE_CONFIG_PATH, config)


def claude_credential_id(name: str) -> str:
    return f"claude:{name}"


def claude_node_isolation(node: dict[str, Any]) -> str:
    return "isolated" if node.get("isolation") == "isolated" else "shared"


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
        "baseUrl": str(node.get("base_url") or ""),
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
        print(f"    Base URL: {node.get('base_url')}")
        if claude_node_isolation(node) == "isolated":
            print(f"    Mode: isolated ({claude_node_home(name, node)})")
        else:
            print(f"    Mode: shared ({HOME / '.claude'})")
        print(f"    Last used: {node.get('lastUsedAt') or '-'}")
        try:
            token = get_claude_secret(name, node)
        except (KeyError, SecureStoreError):
            token = ""
        print(f"    Token: {mask_secret(token)}")


def add_claude_node(config: dict[str, Any]) -> int:
    print("Add or update a Claude API node")
    name = input("Node name: ").strip()
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
    credential_id = claude_credential_id(name)
    SECRET_STORE.set(credential_id, token)
    node: dict[str, Any] = {
        "base_url": base_url,
        "credential_id": credential_id,
        "isolation": mode,
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
    print(f"Saved Claude node '{name}' ({mode}).")
    return 0


def remove_claude_node(config: dict[str, Any], name: str | None) -> int:
    nodes = config.get("nodes") or {}
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


def run_claude_node(config: dict[str, Any], name: str, claude_args: list[str]) -> int:
    node = config.get("nodes", {}).get(name)
    if not node:
        print(f"Error: Claude node '{name}' was not found.", file=sys.stderr)
        return 1
    env = {
        "ANTHROPIC_BASE_URL": clean_hidden_prefix(node.get("base_url", "")),
        "ANTHROPIC_AUTH_TOKEN": get_claude_secret(name, node),
    }
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
    try:
        token = get_claude_secret(name, node)
    except (KeyError, SecureStoreError):
        print(f"Error: Claude node '{name}' has no saved token.", file=sys.stderr)
        return 1

    env = {
        "ANTHROPIC_BASE_URL": clean_hidden_prefix(node.get("base_url", "")),
        "ANTHROPIC_AUTH_TOKEN": clean_hidden_prefix(token),
    }
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


def claude_help() -> None:
    print(
        """apiclaude commands
  apiclaude                       Select a node, then start Claude Code
  apiclaude add                   Add or update a Claude API node
  apiclaude list                  List saved Claude API nodes
  apiclaude list --json           List non-sensitive node metadata as JSON
  apiclaude current               Show current node
  apiclaude mode NAME [MODE]      Show or switch a node between isolated/shared
                                  isolated: node-scoped CLAUDE_CONFIG_DIR
                                  shared:   default ~/.claude (legacy behavior)
  apiclaude remove NAME           Remove a Claude API node (archives isolated dir)
  apiclaude --vscode [NAME]       Open VS Code with a node-scoped user environment
  apiclaude vscode [NAME]         Alias for --vscode
  apiclaude --up                  Update Claude Code
  apiclaude update                Alias for --up
  apiclaude run [ARGS]            Run current node without selecting
  apiclaude help                  Show this help

Any other arguments are passed to Claude Code after selecting a node."""
    )


def claude_main(args: list[str]) -> int:
    if args and args[0] in ("--up", "update"):
        if len(args) != 1:
            print("Error: the update command does not accept arguments.", file=sys.stderr)
            return 1
        return upgrade_claude()

    config = load_claude_config()
    if not args:
        selected = select_claude_node(config)
        return run_claude_node(config, selected, []) if selected else 1
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
    if command in ("--vscode", "vscode"):
        if len(args) > 2:
            print(
                "Error: usage: apiclaude --vscode [NAME].",
                file=sys.stderr,
            )
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
        print(f"ANTHROPIC_BASE_URL={node.get('base_url')}")
        if claude_node_isolation(node) == "isolated":
            print(f"CLAUDE_CONFIG_DIR={claude_node_home(current, node)} (isolated)")
        else:
            print(f"Config dir: {HOME / '.claude'} (shared)")
        try:
            token = get_claude_secret(current, node)
        except (KeyError, SecureStoreError):
            token = ""
        print(f"ANTHROPIC_AUTH_TOKEN={mask_secret(token)}")
        return 0
    if command == "mode":
        if len(args) < 2:
            show_claude_nodes(config)
            return 0
        return set_claude_node_mode(config, args[1], args[2] if len(args) > 2 else None)
    if command == "remove":
        return remove_claude_node(config, args[1] if len(args) > 1 else None)
    if command in ("help", "-h", "--help"):
        claude_help()
        return 0
    if command == "run":
        current = config.get("current")
        if not current:
            print("No current Claude node. Use 'apiclaude add' or 'apiclaude'.", file=sys.stderr)
            return 1
        return run_claude_node(config, current, args[1:])

    selected = select_claude_node(config)
    return run_claude_node(config, selected, args) if selected else 1


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
