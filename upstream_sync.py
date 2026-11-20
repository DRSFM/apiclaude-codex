"""Transfer API upstream settings between devices, using local credential stores."""
from __future__ import annotations

import argparse
from contextlib import suppress
from datetime import datetime
from getpass import GetPassWarning
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
from typing import Any
from urllib.parse import urlsplit
import uuid
import warnings

from codex_accounts import operation_lock, registry_profiles
from codex_api_keys import saved_keys, validated_key_name
from secure_store import SecureStoreError


class SyncError(ValueError):
    """Messages suitable for display without exposing credential values."""


FIELDS = {
    "codex": ("baseUrl", "credentialId"),
    "claude": ("base_url", "credential_id"),
}
CREDENTIAL_PREFIX = "upstream-sync:"
BUNDLE_MAGIC = b"APIAGENT-UPSTREAM\x01"
SALT_SIZE = 16
NONCE_SIZE = 12
MAX_BUNDLE = 16 * 1024 * 1024
DEVICE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}")


def portable_url(value: Any) -> str:
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
    except (AttributeError, TypeError, ValueError):
        raise SyncError("Invalid upstream URL.") from None
    if (not isinstance(value, str) or not host or url.scheme not in ("http", "https")
            or url.username is not None or url.query or url.fragment
            or any(char.isspace() for char in value) or "\\" in value or port == 0):
        raise SyncError("Upstream URLs must be HTTP(S) URLs without credentials, queries or fragments.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    if host.rstrip(".").lower() in ("localhost", "localhost.localdomain") or (
        address and (address.is_loopback or address.is_unspecified)
    ):
        return ""
    return value.rstrip("/")


def _text(value: Any, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise SyncError("Invalid upstream name, model or key.")
    return value


def _local_keys(api: Any, family: str, name: str, record: dict) -> tuple[dict[str, str], str]:
    field = FIELDS[family][1]
    entries = saved_keys(record, active_field=field)
    if entries:
        keys = {entry["name"]: entry["credentialId"] for entry in entries}
        active = next(label for label, ref in keys.items() if ref == record[field])
        return keys, active
    fallback = api.codex_credential_id(record) if family == "codex" else api.claude_credential_id(name)
    return {"account-1": record.get(field) or fallback}, "account-1"


def validate(api: Any, entries: Any) -> None:
    if not isinstance(entries, list) or not entries or len(entries) > 512:
        raise SyncError("The bundle must contain between 1 and 512 upstreams.")
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("family") not in ("codex", "claude"):
            raise SyncError("Invalid upstream record.")
        family = entry["family"]
        name = _text(entry.get("name"), 120)
        identity = family, name.casefold()
        endpoint = entry.get("base_url")
        if identity in seen or not endpoint or portable_url(endpoint) != endpoint:
            raise SyncError("Duplicate or local-only upstream in bundle.")
        seen.add(identity)
        keys = entry.get("keys")
        active = entry.get("active")
        if (not isinstance(keys, dict) or not keys or len(keys) > 64
                or not isinstance(active, str) or active not in keys):
            raise SyncError("Invalid API key list in bundle.")
        labels: set[str] = set()
        for label, token in keys.items():
            if validated_key_name(label) != label or label.casefold() in labels:
                raise SyncError("Invalid or duplicate API key name.")
            labels.add(label.casefold())
            if token != api.clean_hidden_prefix(_text(token, 16384)) or token == api.CODEX_API_AUTH_MARKER:
                raise SyncError("Invalid API key in bundle.")
        if family == "codex":
            _text(entry.get("model"), 240)
            _text(entry.get("effort"), 32)
        else:
            if entry.get("model") is not None:
                _text(entry["model"], 240)
            api.normalize_claude_desktop_models(entry.get("desktop_models"))
            for flag in ("desktop_models_support_1m", "cli_force_default_model"):
                if flag in entry and not isinstance(entry[flag], bool):
                    raise SyncError("Invalid Claude model setting.")


def _source_records(api: Any) -> list[tuple[str, str, dict]]:
    profiles = registry_profiles(api)
    nodes = api._read_claude_config()["nodes"]
    return [
        ("codex", p.get("name") or p.get("id"), p)
        for p in profiles if p.get("type", "api_key") == "api_key"
    ] + [
        ("claude", name, node) for name, node in nodes.items() if not api.is_claude_codex_bridge(node)
    ]


def _export_entry(api: Any, family: str, name: str, record: dict, url: str) -> dict:
    try:
        refs, active = _local_keys(api, family, name, record)
        keys = {label: api.clean_hidden_prefix(api.SECRET_STORE.get(ref)) for label, ref in refs.items()}
    except (KeyError, SecureStoreError, OSError, ValueError):
        raise SyncError(f"Cannot export {family} {name!r}. Check its saved API keys.") from None
    entry = {"family": family, "name": name, "base_url": url, "keys": keys, "active": active}
    if family == "codex":
        entry["model"] = record.get("model") or api.DEFAULT_CODEX_MODEL
        entry["effort"] = record.get("reasoningEffort") or api.DEFAULT_CODEX_REASONING_EFFORT
    else:
        for field in ("model", "desktop_models", "desktop_models_support_1m", "cli_force_default_model"):
            if record.get(field) is not None:
                entry[field] = record[field]
    return entry


def collect(api: Any, requested: list[str]) -> list[dict]:
    selectors = {name.casefold() for name in requested}
    found: set[str] = set()
    entries: list[dict] = []
    for family, name, record in _source_records(api):
        name = _text(name, 120)
        if selectors and name.casefold() not in selectors:
            continue
        try:
            url = portable_url(record.get(FIELDS[family][0]))
        except SyncError:
            raise SyncError(f"Cannot export {family} {name!r}. Check its upstream URL.") from None
        if not url:
            print(f"Skipped {family} {name!r}: local-only endpoint.", file=sys.stderr)
            continue
        entries.append(_export_entry(api, family, name, record, url))
        found.add(name.casefold())
    if selectors - found:
        raise SyncError("A requested upstream was unavailable or local-only.")
    validate(api, entries)
    return entries


def _target(api: Any, family: str, name: str) -> tuple[Any, dict | None, str]:
    if family == "codex":
        profiles = registry_profiles(api)
        # Prefer a name over an ID or an official-account alias.
        existing = next((p for p in profiles if str(p.get("name", "")).casefold() == name.casefold()), None)
        return profiles, existing if existing is not None else api.find_profile(profiles, name), name
    config = api._read_claude_config()
    actual = next((key for key in config["nodes"] if key.casefold() == name.casefold()), name)
    return config, config["nodes"].get(actual), actual


def _plan(api: Any, entry: dict, existing: dict | None, local_name: str,
          update_keys: bool, *, dry_run: bool = False) -> tuple[str, dict, str, dict]:
    if existing is None:
        return "Add", {}, entry["active"], entry["keys"]
    if not update_keys:
        return "Keep existing", {}, "", {}
    family, name = entry["family"], entry["name"]
    if (family == "codex" and (existing.get("type", "api_key") != "api_key"
            or str(existing.get("name", "")).casefold() != name.casefold())
            or family == "claude" and api.is_claude_codex_bridge(existing)):
        return "Keep conflicting entry", {}, "", {}
    if str(existing.get(FIELDS[family][0], "")).rstrip("/") != entry["base_url"]:
        return "Keep different endpoint", {}, "", {}
    refs, active = _local_keys(api, family, local_name, existing)
    if dry_run:
        return "Eligible for key updates", refs, active, entry["keys"]
    known = {label.casefold(): label for label in refs}
    changes = {}
    for incoming, token in entry["keys"].items():
        label = known.get(incoming.casefold(), incoming)
        current = None
        if label in refs:
            with suppress(KeyError):
                current = api.SECRET_STORE.get(refs[label])
        if token != current:
            changes[label] = token
    return ("Update keys" if changes else "Already synced"), refs, active, changes


def _new_codex(api: Any, profiles: list[dict], entry: dict) -> dict:
    base = api.slugify(entry["name"])[:80].rstrip(".")
    # A prefix also prevents Windows device names such as CON from becoming a directory.
    profile_id = f"sync-{base}"
    suffix = 2
    while (api.find_profile(profiles, profile_id)
           or api.codex_profile_home({"home": f"profiles/{profile_id}"}).exists()
           or api.codex_profile_home({"home": f"profiles/{profile_id}"}).is_symlink()):
        profile_id = f"sync-{base}-{suffix}"
        suffix += 1
    return {
        "id": profile_id, "name": entry["name"], "home": f"profiles/{profile_id}",
        "baseUrl": entry["base_url"], "model": entry["model"], "reasoningEffort": entry["effort"],
        "createdAt": api.now_iso(), "lastUsedAt": None, "useCustomCodexCli": False,
    }


def _new_claude(api: Any, config: dict, entry: dict) -> dict:
    name = entry["name"]
    home = api.default_claude_node_home(config, name)
    root = api.CLAUDE_NODES_ROOT
    occupied = {str(node.get("home") or api.default_claude_node_home(config, other)).casefold()
                for other, node in config["nodes"].items()}
    if (root / home).exists() or (root / home).is_symlink() or home.casefold() in occupied:
        home = f"nodes/sync-{uuid.uuid4().hex}"
    # Windows reserves certain path components even when no directory exists.
    basename = Path(home).name.split(".", 1)[0]
    if os.name == "nt" and re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", basename, re.IGNORECASE):
        home = f"nodes/sync-{uuid.uuid4().hex}"
    record = {
        "base_url": entry["base_url"], "home": home, "isolation": "isolated",
        "proxy_enabled": False, "proxy_url": api.DEFAULT_CLAUDE_PROXY_URL, "lastUsedAt": None,
    }
    for field in ("model", "desktop_models", "desktop_models_support_1m", "cli_force_default_model"):
        if field in entry:
            record[field] = entry[field]
    if not api.is_safe_claude_node_home(name, record):
        raise SyncError("Refusing to use a redirected Claude node home.")
    return record


def _commit(api: Any, entry: dict, registry: Any, existing: dict | None,
            refs: dict[str, str], active: str, changes: dict[str, str]) -> None:
    family, name = entry["family"], entry["name"]
    if family == "codex":
        if any(p.get("api_key") or p.get("apiKey") for p in registry):
            raise SyncError("Migrate local Codex legacy credentials before syncing.")
        record = dict(existing) if existing is not None else _new_codex(api, registry, entry)
        if not api.is_safe_api_profile_home(record):
            raise SyncError("Refusing to use a redirected Codex profile home.")
    else:
        if any(node.get("token") for node in registry["nodes"].values()):
            raise SyncError("Migrate local Claude legacy credentials before syncing.")
        record = dict(existing) if existing is not None else _new_claude(api, registry, entry)
    store = api.SECRET_STORE
    new_refs = {}
    created_home = None
    try:
        for label, token in changes.items():
            reference = f"{CREDENTIAL_PREFIX}{family}:{uuid.uuid4().hex}"
            new_refs[label] = reference
            store.set(reference, token)
            if store.get(reference) != token:
                raise SyncError("Could not verify the saved API key.")
        combined = {**refs, **new_refs}
        record[FIELDS[family][1]] = combined[active]
        if len(combined) > 1 or (existing or {}).get("apiKeys") or active != "account-1":
            record["apiKeys"] = [{"name": label, "credentialId": ref} for label, ref in combined.items()]
        if family == "codex":
            if existing is None:
                home = api.codex_profile_home(record)
                home.mkdir(parents=True, exist_ok=False)
                created_home = home
                api.write_codex_config(home, record["baseUrl"], record["model"], record["reasoningEffort"])
                registry.append(record)
            else:
                registry[registry.index(existing)] = record
            api.save_codex_profiles(registry)
        else:
            actual = next((key for key in registry["nodes"] if key.casefold() == name.casefold()), name)
            registry["nodes"][actual] = record
            if not registry.get("current"):
                registry["current"] = actual
            api._save_claude_config_locked(registry)
    except (OSError, SecureStoreError, ValueError, KeyError):
        for reference in new_refs.values():
            with suppress(OSError, SecureStoreError):
                store.clear(reference)
        if created_home is not None:
            with suppress(OSError):
                (created_home / "config.toml").unlink(missing_ok=True)
                created_home.rmdir()
        raise
    _, saved, _ = _target(api, family, name)
    if saved != record:
        raise SyncError("Could not verify the registry write. Both sets of credentials were retained.")
    for label in changes:
        reference = refs.get(label, "")
        if reference.startswith(f"{CREDENTIAL_PREFIX}{family}:") and reference not in combined.values():
            try:
                store.clear(reference)
            except (OSError, SecureStoreError):
                print("Warning: a superseded sync credential could not be cleared.", file=sys.stderr)


def apply(api: Any, entries: list[dict], *, dry_run: bool, update_keys: bool) -> int:
    validate(api, entries)
    count = 0
    for entry in entries:
        family, name = entry["family"], entry["name"]
        registry, existing, local_name = _target(api, family, name)
        status, refs, active, changes = _plan(
            api, entry, existing, local_name, update_keys, dry_run=dry_run,
        )
        if changes and not dry_run:
            lock = (
                operation_lock(api.CODEX_HOME / ".account-operation.lock")
                if family == "codex" else api._claude_config_lock()
            )
            with lock:
                registry, existing, local_name = _target(api, family, name)
                status, refs, active, changes = _plan(api, entry, existing, local_name, update_keys)
                if changes:
                    _commit(api, entry, registry, existing, refs, active, changes)
        count += bool(changes)
        print(f"{status}: {family} {name!r}")
    return count


def _cipher(passphrase: bytes, salt: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise SyncError("Upstream sync requires the optional 'cryptography' package in this Python environment.") from None
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(passphrase)
    return AESGCM(key)


def seal(entries: list[dict], passphrase: bytes) -> bytes:
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = BUNDLE_MAGIC + secrets.token_bytes(SALT_SIZE + NONCE_SIZE)
    if len(header) + len(payload) + 16 > MAX_BUNDLE:
        raise SyncError("The upstream bundle is too large.")
    cipher = _cipher(passphrase, header[len(BUNDLE_MAGIC):-NONCE_SIZE])
    return header + cipher.encrypt(header[-NONCE_SIZE:], payload, header)


def unseal(bundle: bytes, passphrase: bytes) -> list[dict]:
    header_size = len(BUNDLE_MAGIC) + SALT_SIZE + NONCE_SIZE
    if not bundle.startswith(BUNDLE_MAGIC) or not header_size + 16 < len(bundle) <= MAX_BUNDLE:
        raise SyncError("Unsupported or damaged upstream bundle.")
    header = bundle[:header_size]
    cipher = _cipher(passphrase, header[len(BUNDLE_MAGIC):-NONCE_SIZE])
    from cryptography.exceptions import InvalidTag
    try:
        payload = cipher.decrypt(header[-NONCE_SIZE:], bundle[header_size:], header)
        return json.loads(payload.decode("utf-8"))
    except (InvalidTag, UnicodeDecodeError, ValueError, RecursionError):
        raise SyncError("Could not unlock the bundle. Check the passphrase and the file.") from None


def _passphrase(api: Any, *, confirm: bool) -> bytes:
    with warnings.catch_warnings():
        warnings.simplefilter("error", GetPassWarning)
        try:
            value = api.getpass("Bundle passphrase: ")
            if not value or len(value) > 1024:
                raise SyncError("Enter a passphrase of at most 1024 characters.")
            if confirm:
                if len(value) < 16:
                    raise SyncError("Choose a strong passphrase of at least 16 characters.")
                if value != api.getpass("Repeat bundle passphrase: "):
                    raise SyncError("The passphrases did not match.")
        except (EOFError, GetPassWarning):
            raise SyncError("Use an interactive terminal that can hide the passphrase.") from None
    return value.encode("utf-8")


def _tailscale_client() -> tuple[str | None, dict[str, str] | None]:
    tailscale = shutil.which("tailscale")
    environment = None
    if sys.platform == "darwin":
        app_cli = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
        if not tailscale and app_cli.is_file():
            tailscale = str(app_cli)
        environment = {**os.environ, "TAILSCALE_BE_CLI": "1"}
    return tailscale, environment


def _taildrop_targets() -> list[str]:
    tailscale, environment = _tailscale_client()
    if not tailscale:
        return []
    try:
        result = subprocess.run(
            [tailscale, "file", "cp", "--targets"],
            timeout=8, check=False, env=environment, capture_output=True, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    targets = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2 or not DEVICE_NAME.fullmatch(parts[1]):
            continue
        address, name = parts
        try:
            ipaddress.ip_address(address)
        except ValueError:
            continue
        targets.append(name)
    return list(dict.fromkeys(targets))


def _send(path: Path, devices: list[str]) -> None:
    tailscale, environment = _tailscale_client()
    if not tailscale:
        raise SyncError("Tailscale is required to send the bundle. The encrypted file was saved.")
    failed = []
    for device in dict.fromkeys(devices):
        try:
            result = subprocess.run(
                [tailscale, "file", "cp", "--update-interval=0", str(path.resolve()), device + ":"],
                timeout=60, check=False, env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            failed.append(device)
            continue
        if result.returncode:
            failed.append(device)
        else:
            print(f"Sent encrypted bundle to {device}.")
    if failed:
        raise SyncError(f"Taildrop failed for {', '.join(failed)}. The encrypted file was saved.")


def _choose_numbers(title: str, labels: list[str], *, save_locally: bool = False) -> list[int] | None:
    print(title)
    for index, label in enumerate(labels, 1):
        print(f"  {index}. {label}")
    if save_locally:
        print("  0. Save the encrypted bundle locally")
    while True:
        choice = input("Choose numbers separated by commas (q to cancel): ").strip()
        if choice.casefold() in ("", "q"):
            return None
        if save_locally and choice == "0":
            return []
        numbers = re.split(r"[,，\s]+", choice)
        if all(re.fullmatch(r"[0-9]{1,6}", number) and 1 <= int(number) <= len(labels)
               for number in numbers):
            return list(dict.fromkeys(int(number) - 1 for number in numbers))
        print("Choose only numbers listed in the menu.")


def _interactive_export(api: Any) -> None:
    candidates = []
    for family, name, record in _source_records(api):
        try:
            name = _text(name, 120)
            url = portable_url(record.get(FIELDS[family][0]))
        except SyncError:
            continue
        if url:
            candidates.append((family, name, record, url))
    if not candidates:
        raise SyncError("No portable API upstreams are available to export.")
    families = {"codex": "ApiCodex", "claude": "ApiClaude"}
    selected = _choose_numbers(
        "Choose API upstreams to sync",
        [f"{families[family]} / {name}" for family, name, _, _ in candidates],
    )
    if selected is None:
        return
    devices = _taildrop_targets()
    recipients = _choose_numbers(
        "Choose Taildrop destinations" if devices else "No online Taildrop destinations are available",
        devices, save_locally=True,
    )
    if recipients is None:
        return
    entries = [_export_entry(api, *candidates[index]) for index in selected]
    validate(api, entries)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = api.HOME / ".apiagent-sync" / f"upstreams-{stamp}-{secrets.token_hex(4)}.apisync"
    _write_bundle(api, entries, path, [devices[index] for index in recipients], create_parent=True)


def _write_bundle(api: Any, entries: list[dict], path: Path, devices: list[str], *,
                  create_parent: bool = False) -> None:
    bundle = seal(entries, _passphrase(api, confirm=True))
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("xb")
    try:
        with file:
            file.write(bundle)
    except (OSError, KeyboardInterrupt):
        with suppress(OSError):
            path.unlink()
        raise
    print(f"Saved {len(entries)} upstream(s) to {path}.")
    if devices:
        _send(path, devices)


def main(args: list[str], api: Any) -> int:
    parser = argparse.ArgumentParser(
        prog="apiagent sync",
        description="Choose upstreams from a numbered menu, or use export and import with encrypted bundles.",
    )
    commands = parser.add_subparsers(dest="command")
    exporter = commands.add_parser("export", help="Create an encrypted bundle")
    exporter.add_argument("file", type=Path)
    exporter.add_argument("--name", action="append", default=[], help="Include this upstream name (repeatable)")
    exporter.add_argument("--to", action="append", default=[], help="Send the bundle to this Taildrop device (repeatable)")
    importer = commands.add_parser("import", help="Read an encrypted bundle")
    importer.add_argument("file", type=Path)
    importer.add_argument("--dry-run", action="store_true", help="Preview changes without writing them")
    importer.add_argument("--update-keys", action="store_true", help="Merge keys into existing upstreams with the same URL")
    options = parser.parse_args(args)
    if not options.command and not sys.stdin.isatty():
        parser.error("The numbered menu requires an interactive terminal. Use export or import in scripts.")
    try:
        if not options.command:
            _interactive_export(api)
        elif options.command == "export":
            path = options.file.expanduser()
            if any(not DEVICE_NAME.fullmatch(device) for device in options.to):
                raise SyncError("Use Tailscale machine names or IPv4 addresses for --to.")
            if path.exists() or path.is_symlink():
                raise SyncError("The destination file already exists. Choose a new bundle filename.")
            _write_bundle(api, collect(api, options.name), path, options.to)
        else:
            path = options.file.expanduser()
            with path.open("rb") as file:
                bundle = file.read(MAX_BUNDLE + 1)
            entries = unseal(bundle, _passphrase(api, confirm=False))
            count = apply(api, entries, dry_run=options.dry_run, update_keys=options.update_keys)
            print(f"{'Eligible for import' if options.dry_run else 'Changed'}: {count} upstream(s).")
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (OSError, SecureStoreError, ValueError, KeyError):
        print("Error: could not read or save the bundle or local configuration.", file=sys.stderr)
        return 1
    except EOFError:
        print("\nCancelled.", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    return 0
