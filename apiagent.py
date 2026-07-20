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
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from getpass import getpass
from pathlib import Path
from typing import Any

from secure_store import SecureStore, SecureStoreError


HOME = Path.home()
CODEX_HOME = HOME / ".codex-api"
CODEX_PROFILES_PATH = CODEX_HOME / "profiles.json"
CODEX_ARCHIVE_ROOT = CODEX_HOME / "archived-profiles"
CODEX_VSCODE_DATA_ROOT = HOME / ".apicodex-vscode"
CODEX_DESKTOP_DATA_ROOT = HOME / ".apicodex-desktop"
CLAUDE_CONFIG_PATH = HOME / ".apiclaude_config.json"
SECRET_STORE = SecureStore(HOME / ".apiagent-secrets")
DEFAULT_CODEX_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CODEX_MODEL = "gpt-5.5"
CODEX_API_AUTH_MARKER = "apicodex-managed-key-in-child-environment"
CODEX_AUTH_STORE = "keyring"
CODEX_INSTALL_SCRIPT = "$env:CODEX_NON_INTERACTIVE=1; irm https://chatgpt.com/codex/install.ps1 | iex"
HIDDEN_PREFIX_CHARS = "\ufeff\u200b\u200c\u200d\u2060\ufffd"
CODEX_PARENT_CONTEXT_ENV = (
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
    "CODEX_PERMISSION_PROFILE",
    "CODEX_SHELL",
    "CODEX_THREAD_ID",
)
CODEX_DESKTOP_ENV_REMOVE = CODEX_PARENT_CONTEXT_ENV + (
    "CODEX_HOME",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def clean_hidden_prefix(value: str) -> str:
    return value.lstrip(HIDDEN_PREFIX_CHARS).strip()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or "profile"


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
            write_codex_config(home, base_url, model)
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


def write_codex_config(home: Path, base_url: str, model: str = DEFAULT_CODEX_MODEL) -> None:
    home.mkdir(parents=True, exist_ok=True)
    config = f'''model = "{model}"
model_provider = "apicodex"
model_reasoning_effort = "high"
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


def add_codex_profile() -> int:
    profiles = load_codex_profiles()
    print("Add or update a Codex API profile")
    name = input("Profile name: ").strip()
    if not name:
        name = "default" if not profiles else ""
    if not name:
        print("Error: profile name cannot be empty.", file=sys.stderr)
        return 1

    existing = find_profile(profiles, name)
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

    api_key = getpass("API key: ")
    if not api_key.strip():
        print("Error: API key cannot be empty.", file=sys.stderr)
        return 1
    credential_id = codex_credential_id(profile)
    SECRET_STORE.set(credential_id, clean_hidden_prefix(api_key))
    write_codex_config(home, base_url, profile.get("model") or DEFAULT_CODEX_MODEL)

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


def remove_codex_profile() -> int:
    profiles = load_codex_profiles()
    show_codex_profiles(profiles)
    if not profiles:
        return 0
    choice = input("Remove which profile number or name: ").strip()
    profile = None
    if choice.isdigit() and 1 <= int(choice) <= len(profiles):
        profile = profiles[int(choice) - 1]
    else:
        profile = find_profile(profiles, choice)
    if not profile:
        print("Error: profile was not found.", file=sys.stderr)
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
            archive_path = CODEX_ARCHIVE_ROOT / f"{profile['id']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
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
    if not is_isolated_codex_home(home):
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

    if not (home / "config.toml").exists():
        write_codex_config(
            home,
            selected.get("baseUrl", DEFAULT_CODEX_BASE_URL),
            selected.get("model") or DEFAULT_CODEX_MODEL,
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
    return start_detached_process(
        str(desktop_exe),
        [f"--user-data-dir={desktop_data}"],
        env={
            "CODEX_HOME": str(home),
            "APICODEX_API_KEY": clean_hidden_prefix(api_key),
        },
        env_remove=CODEX_DESKTOP_ENV_REMOVE,
    )


def launch_codex_vscode(
    profiles: list[dict[str, Any]],
    selected: dict[str, Any],
) -> int:
    home = codex_profile_home(selected)
    if not (home / "config.toml").exists():
        write_codex_config(
            home,
            selected.get("baseUrl", DEFAULT_CODEX_BASE_URL),
            selected.get("model") or DEFAULT_CODEX_MODEL,
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
  apicodex --api-profile <name>    Start a specific API profile
  apicodex --api-remove            Unregister/archive a saved API profile
  apicodex --vscode                Choose a profile and open VS Code here
  apicodex --desktop               Choose a profile and open an isolated desktop app
  apicodex --up                    Update the Codex CLI
  apicodex --api-help              Show this help

Any remaining arguments are passed to codex."""
    )


def codex_main(args: list[str]) -> int:
    pass_through: list[str] = []
    requested: str | None = None
    do_add = do_list = do_remove = do_help = do_upgrade = do_vscode = False
    do_desktop = False
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

    if do_help:
        codex_help()
        return 0
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
        show_codex_profiles(load_codex_profiles())
        return 0
    if do_remove:
        return remove_codex_profile()
    if do_add:
        code = add_codex_profile()
        if code != 0 or not pass_through:
            return code

    profiles = load_codex_profiles()
    selected = select_codex_profile(profiles, requested)
    if not selected:
        return 1
    home = codex_profile_home(selected)
    if not (home / "config.toml").exists():
        write_codex_config(
            home,
            selected.get("baseUrl", DEFAULT_CODEX_BASE_URL),
            selected.get("model") or DEFAULT_CODEX_MODEL,
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
    if name in (config.get("nodes") or {}) and input(f"Node '{name}' exists. Overwrite? (y/N): ").strip().lower() != "y":
        print("Cancelled.")
        return 0
    base_url = clean_hidden_prefix(input("ANTHROPIC_BASE_URL: "))
    token = clean_hidden_prefix(getpass("ANTHROPIC_AUTH_TOKEN: "))
    if not base_url or not token:
        print("Error: base URL and token cannot be empty.", file=sys.stderr)
        return 1
    credential_id = claude_credential_id(name)
    SECRET_STORE.set(credential_id, token)
    config.setdefault("nodes", {})[name] = {
        "base_url": base_url,
        "credential_id": credential_id,
    }
    if not config.get("current"):
        config["current"] = name
    save_claude_config(config)
    print(f"Saved Claude node '{name}'.")
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
    credential_id = nodes[name].get("credential_id") or claude_credential_id(name)
    del nodes[name]
    if config.get("current") == name:
        config["current"] = None
    save_claude_config(config)
    SECRET_STORE.clear(credential_id)
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
    config["current"] = name
    save_claude_config(config)
    env = {
        "ANTHROPIC_BASE_URL": clean_hidden_prefix(node.get("base_url", "")),
        "ANTHROPIC_AUTH_TOKEN": get_claude_secret(name, node),
    }
    print(f"Using Claude node '{name}' ({env['ANTHROPIC_BASE_URL']})")
    return run_command("claude", claude_args, env=env)


def claude_help() -> None:
    print(
        """apiclaude commands
  apiclaude                       Select a node, then start Claude Code
  apiclaude add                   Add or update a Claude API node
  apiclaude list                  List saved Claude API nodes
  apiclaude current               Show current node
  apiclaude remove NAME           Remove a Claude API node
  apiclaude run [ARGS]            Run current node without selecting
  apiclaude help                  Show this help

Any other arguments are passed to Claude Code after selecting a node."""
    )


def claude_main(args: list[str]) -> int:
    config = load_claude_config()
    if not args:
        selected = select_claude_node(config)
        return run_claude_node(config, selected, []) if selected else 1
    command = args[0]
    if command == "add":
        return add_claude_node(config)
    if command == "list":
        show_claude_nodes(config)
        return 0
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
        try:
            token = get_claude_secret(current, node)
        except (KeyError, SecureStoreError):
            token = ""
        print(f"ANTHROPIC_AUTH_TOKEN={mask_secret(token)}")
        return 0
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
