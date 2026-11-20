"""Named credentials for native Claude nodes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
from getpass import GetPassWarning
import sys
from typing import Any, Callable, Iterator
import uuid
import warnings

from codex_api_keys import ApiKeyError, prompt_key_choice, saved_keys, validated_key_name
from secure_store import SecureStore, SecureStoreError


def node_keys(node: dict[str, Any]) -> list[dict[str, str]]:
    return saved_keys(node, active_field="credential_id")


def _native_node(config: dict[str, Any], name: str, api: Any) -> dict[str, Any]:
    node = config["nodes"].get(name)
    if node is None:
        raise ApiKeyError("Select an existing Claude API node.")
    if api.is_claude_codex_bridge(node):
        raise ApiKeyError("Manage bridge credentials with 'apicodex keys' on the linked Codex profile.")
    return node


def read_hidden_key(getpass: Callable[[str], str], prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            return getpass(prompt)
        except GetPassWarning:
            raise ApiKeyError("Use an interactive terminal that can hide API key input.") from None


@contextmanager
def staged_credential(store: SecureStore, token: str) -> Iterator[str]:
    reference = f"claude-key:{uuid.uuid4().hex}"
    try:
        store.set(reference, token)
        if store.get(reference) != token:
            raise ApiKeyError("Could not verify the new API key.")
        yield reference
    except (KeyError, OSError, SecureStoreError, ValueError):
        with suppress(OSError, SecureStoreError):
            store.clear(reference)
        raise


def _usable_key(store: SecureStore, reference: str) -> None:
    if not store.get(reference).strip():
        raise ApiKeyError("The selected API key is empty.")


def _add(name: str, label: str, api: Any) -> None:
    label = validated_key_name(label)
    initial = _native_node(api.load_claude_config(), name, api)
    active = initial.get("credential_id") or api.claude_credential_id(name)
    entries = node_keys(initial) or [{"name": "account-1", "credentialId": active}]
    if any(item["name"].casefold() == label.casefold() for item in entries):
        raise ApiKeyError("That API key name already exists.")
    _usable_key(api.SECRET_STORE, active)
    token = api.clean_hidden_prefix(read_hidden_key(api.getpass, "API key (hidden): "))
    if not token.strip():
        raise ApiKeyError("API key cannot be empty.")
    with api._claude_config_lock():
        config = api._read_claude_config()
        node = _native_node(config, name, api)
        if node != initial:
            raise ApiKeyError("Claude node changed during key entry. Reload and retry.")
        with staged_credential(api.SECRET_STORE, token) as reference:
            node["apiKeys"] = [*entries, {"name": label, "credentialId": reference}]
            node["credential_id"] = active
            api.write_json_atomic(api.CLAUDE_CONFIG_PATH, config)
    print(f"Saved API key '{label}' for Claude node '{name}'.")


def _select(
    name: str, label: str, api: Any, *, expected: dict[str, Any] | None = None,
) -> str:
    with api._claude_config_lock():
        config = api._read_claude_config()
        node = _native_node(config, name, api)
        if expected is not None and node != expected:
            raise ApiKeyError("Claude node changed during key selection. Reload and retry.")
        active = node.get("credential_id") or api.claude_credential_id(name)
        entries = node_keys(node) or [{"name": "account-1", "credentialId": active}]
        entry = next((item for item in entries if item["name"].casefold() == label.casefold()), None)
        if entry is None:
            raise ApiKeyError("Saved API key name not found.")
        selected = entry["credentialId"]
        _usable_key(api.SECRET_STORE, selected)
        if selected != active:
            node["credential_id"] = selected
            api.write_json_atomic(api.CLAUDE_CONFIG_PATH, config)
        return selected


def choose_for_launch(config: dict[str, Any], name: str, api: Any) -> bool:
    try:
        node = config["nodes"][name]
        entries = node_keys(node)
        if len(entries) < 2 or not sys.stdin.isatty():
            return True
        entry = prompt_key_choice(
            name, entries, node["credential_id"],
            "Restart an existing Claude client to apply a different key.",
        )
        if entry is None:
            return False
        selected = _select(name, entry["name"], api, expected=node)
        node["credential_id"] = selected
        # The selection is already committed. Subsequent launch metadata saves
        # must not write it back over a later selection from another process.
        if isinstance(config, api._ClaudeConfig):
            config.baseline["nodes"][name]["credential_id"] = selected
        return True
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
    except ApiKeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
    except (KeyError, OSError, SecureStoreError, ValueError):
        print("Error: could not read or select a saved Claude API key.", file=sys.stderr)
    return False


def main(args: list[str], api: Any) -> int:
    parser = argparse.ArgumentParser(prog="apiclaude keys", description="Manage named API keys for a Claude node.")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("add", "list", "use"):
        sub = commands.add_parser(command)
        sub.add_argument("node")
        if command != "list":
            sub.add_argument("name")
    try:
        options = parser.parse_args(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if options.command == "add":
            _add(options.node, options.name, api)
        elif options.command == "use":
            api.load_claude_config()
            _select(options.node, options.name, api)
            print("API key selected. Restart this node's running clients to apply the change.")
        else:
            node = _native_node(api._read_claude_config(), options.node, api)
            active = node.get("credential_id") or api.claude_credential_id(options.node)
            entries = node_keys(node) or [{"name": "account-1", "credentialId": active}]
            for index, entry in enumerate(entries, 1):
                marker = " (current)" if entry["credentialId"] == active else ""
                print(f"[{index}] {entry['name']}{marker}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
    except ApiKeyError as exc:
        print(f"Error: {exc}", file=sys.stderr)
    except (KeyError, OSError, SecureStoreError, ValueError):
        print("Error: could not access the saved Claude API keys or node configuration.", file=sys.stderr)
    return 1
