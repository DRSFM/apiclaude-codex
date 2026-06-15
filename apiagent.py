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


HOME = Path.home()
CODEX_HOME = HOME / ".codex-api"
CODEX_PROFILES_PATH = CODEX_HOME / "profiles.json"
CODEX_ARCHIVE_ROOT = CODEX_HOME / "archived-profiles"
CLAUDE_CONFIG_PATH = HOME / ".apiclaude_config.json"
DEFAULT_CODEX_BASE_URL = "https://api.openai.com/v1"
DEFAULT_CODEX_MODEL = "gpt-5.5"
HIDDEN_PREFIX_CHARS = "\ufeff\u200b\u200c\u200d\u2060\ufffd"


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


def run_command(command: str, args: list[str], env: dict[str, str] | None = None, input_text: str | None = None) -> int:
    exe = shutil.which(command)
    if not exe:
        print(f"Error: command not found: {command}", file=sys.stderr)
        return 1

    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)

    if os.name == "nt":
        cmdline = subprocess.list2cmdline([exe, *args])
        cmd = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", cmdline]
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
    return list(data.get("profiles") or [])


def save_codex_profiles(profiles: list[dict[str, Any]]) -> None:
    write_json(CODEX_PROFILES_PATH, {"version": 1, "profiles": profiles})


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
auth_credentials_store = "file"

[windows]
sandbox = "unelevated"

[model_providers.apicodex]
name = "API Codex"
base_url = "{base_url}"
wire_api = "responses"
requires_openai_auth = true
'''
    (home / "config.toml").write_text(config, encoding="utf-8")


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


def clean_saved_codex_auth(home: Path) -> None:
    auth_path = home / "auth.json"
    if not auth_path.exists():
        return
    data = read_json(auth_path, {})
    key_name = next((name for name in data if "API_KEY" in name.upper() or "OPENAI" in name.upper()), None)
    if not key_name or not isinstance(data.get(key_name), str):
        return
    cleaned = clean_hidden_prefix(data[key_name])
    if cleaned != data[key_name]:
        data[key_name] = cleaned
        write_json(auth_path, data)


def codex_login_with_key(home: Path, api_key: str) -> int:
    api_key = clean_hidden_prefix(api_key)
    code = run_command("codex", ["login", "--with-api-key"], env={"CODEX_HOME": str(home)}, input_text=api_key + "\n")
    clean_saved_codex_auth(home)
    return code


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

    write_codex_config(home, base_url)
    api_key = getpass("API key: ")
    if not api_key.strip():
        print("Error: API key cannot be empty.", file=sys.stderr)
        return 1
    code = codex_login_with_key(home, api_key)
    if code != 0:
        return code

    profile["name"] = name
    profile["baseUrl"] = base_url
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


def codex_help() -> None:
    print(
        """apicodex commands
  apicodex                         Select a saved API profile, then start Codex
  apicodex --api-add               Add or update an API profile
  apicodex --setup                 Alias for --api-add
  apicodex --api-list              List saved API profiles
  apicodex --api-profile <name>    Start a specific API profile
  apicodex --api-remove            Unregister/archive a saved API profile
  apicodex --api-help              Show this help

Any remaining arguments are passed to codex."""
    )


def codex_main(args: list[str]) -> int:
    pass_through: list[str] = []
    requested: str | None = None
    do_add = do_list = do_remove = do_help = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in ("--api-add", "--setup"):
            do_add = True
        elif arg == "--api-list":
            do_list = True
        elif arg == "--api-remove":
            do_remove = True
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
    if do_list:
        show_codex_profiles(load_codex_profiles())
        return 0
    if do_remove:
        return remove_codex_profile()
    if do_add:
        code = add_codex_profile()
        if code != 0 or not pass_through:
            return code

    selected = select_codex_profile(load_codex_profiles(), requested)
    if not selected:
        return 1
    home = codex_profile_home(selected)
    if not (home / "config.toml").exists():
        write_codex_config(home, selected.get("baseUrl", DEFAULT_CODEX_BASE_URL))
    if not (home / "auth.json").exists():
        print(f"Profile '{selected.get('name')}' has no saved API key yet.")
        api_key = getpass("API key: ")
        code = codex_login_with_key(home, api_key)
        if code != 0:
            return code

    add_current_project_trust(home)
    update_codex_last_used(selected)
    return run_command("codex", pass_through, env={"CODEX_HOME": str(home)})


def load_claude_config() -> dict[str, Any]:
    return read_json(CLAUDE_CONFIG_PATH, {"nodes": {}, "current": None})


def save_claude_config(config: dict[str, Any]) -> None:
    write_json(CLAUDE_CONFIG_PATH, config)


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
        print(f"    Token: {mask_secret(node.get('token', ''))}")


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
    config.setdefault("nodes", {})[name] = {"base_url": base_url, "token": token}
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
    del nodes[name]
    if config.get("current") == name:
        config["current"] = None
    save_claude_config(config)
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
        "ANTHROPIC_AUTH_TOKEN": clean_hidden_prefix(node.get("token", "")),
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
        print(f"ANTHROPIC_AUTH_TOKEN={mask_secret(node.get('token', ''))}")
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
