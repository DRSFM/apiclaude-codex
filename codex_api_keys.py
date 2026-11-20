"""Named API credentials within one existing profile and conversation home."""
from __future__ import annotations

import argparse
import sys
from typing import Any
import uuid

from codex_accounts import AccountError, operation_lock, registry_profiles
from secure_store import SecureStoreError


class ApiKeyError(ValueError):
    """Safe messages only; never include a credential or a storage exception."""


def saved_keys(profile: dict[str, Any], *, active_field: str = 'credentialId') -> list[dict[str, str]]:
    entries = profile.get('apiKeys', [])
    if not isinstance(entries, list):
        raise ApiKeyError('Invalid saved API key list; profile was retained.')
    names, references = set(), set()
    result = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ApiKeyError('Invalid saved API key entry; profile was retained.')
        name, reference = entry.get('name'), entry.get('credentialId')
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or not isinstance(reference, str) or not reference
                or name.casefold() in names or reference in references):
            raise ApiKeyError('Invalid or duplicate saved API key entry; profile was retained.')
        names.add(name.casefold())
        references.add(reference)
        result.append({'name': name, 'credentialId': reference})
    active = profile.get(active_field)
    if result and (not isinstance(active, str) or active not in references):
        raise ApiKeyError('The active API key is not in the saved list; profile was retained.')
    return result


def _profile(profiles: list[dict[str, Any]], name: str, api: Any) -> dict[str, Any]:
    profile = api.find_profile(profiles, name)
    if (not profile or profile.get('type', 'api_key') != 'api_key'
            or not api.is_safe_api_profile_home(profile)):
        raise ApiKeyError('Select an existing, safe API profile.')
    saved_keys(profile)
    return profile


def validated_key_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ApiKeyError('Choose an API key name of 1-80 printable characters.')
    return name


def _read(reference: str, api: Any) -> str:
    # A selected missing/unreadable credential must not fall back to another key.
    value = api.clean_hidden_prefix(api.SECRET_STORE.get(reference))
    if not value.strip() or value == api.CODEX_API_AUTH_MARKER:
        raise ApiKeyError('The selected API key is empty or invalid; no key was replaced.')
    return value


def _save(profiles: list[dict[str, Any]], profile: dict[str, Any], api: Any) -> None:
    api.save_codex_profiles(profiles)
    verified = _profile(registry_profiles(api), str(profile['id']), api)
    if (verified.get('credentialId') != profile['credentialId']
            or saved_keys(verified) != saved_keys(profile)):
        raise ApiKeyError('Could not verify saved API key metadata; credentials were retained.')


def add(name: str, label: str, api: Any) -> None:
    label = validated_key_name(label)
    initial = _profile(registry_profiles(api), name, api)
    entries = saved_keys(initial)
    if not entries:
        entries = [{'name': 'account-1', 'credentialId': initial.get('credentialId') or api.codex_credential_id(initial)}]
    if any(item['name'].casefold() == label.casefold() for item in entries):
        raise ApiKeyError('That API key name already exists; choose another name.')
    # Check the existing primary credential before asking for a second one.
    _read(str(initial.get('credentialId') or api.codex_credential_id(initial)), api)
    secret = api.clean_hidden_prefix(api.getpass('API key (hidden): '))
    if not secret.strip() or secret == api.CODEX_API_AUTH_MARKER:
        raise ApiKeyError('API key cannot be empty or a placeholder.')
    with operation_lock(api.CODEX_HOME / '.account-operation.lock'):
        profiles = registry_profiles(api)
        profile = _profile(profiles, str(initial['id']), api)
        if (profile.get('home') != initial.get('home')
                or profile.get('credentialId') != initial.get('credentialId')
                or saved_keys(profile) != saved_keys(initial)):
            raise ApiKeyError('API key metadata changed; retry after reloading the profile.')
        reference = f'codex:{profile["id"]}:key:{uuid.uuid4().hex}'
        api.SECRET_STORE.set(reference, secret)
        if _read(reference, api) != secret:
            raise ApiKeyError('Could not verify the new API key; original credentials were retained.')
        profile['apiKeys'] = [*entries, {'name': label, 'credentialId': reference}]
        profile['credentialId'] = initial.get('credentialId') or api.codex_credential_id(initial)
        _save(profiles, profile, api)
    print(f"Saved API key '{label}' for '{profile['name']}'. Existing history and the current key are unchanged.")


def use(name: str, label: str, api: Any, *, expected_home: str | None = None) -> dict[str, Any]:
    with operation_lock(api.CODEX_HOME / '.account-operation.lock'):
        profiles = registry_profiles(api)
        profile = _profile(profiles, name, api)
        if expected_home is not None and profile.get('home') != expected_home:
            raise ApiKeyError('The profile home changed; reload before selecting a key.')
        entry = next((item for item in saved_keys(profile) if item['name'].casefold() == label.casefold()), None)
        if entry is None:
            raise ApiKeyError('Saved API key name not found.')
        _read(entry['credentialId'], api)
        if profile.get('credentialId') != entry['credentialId']:
            profile['credentialId'] = entry['credentialId']
            _save(profiles, profile, api)
        return profile


def prompt_key_choice(
    name: str, entries: list[dict[str, str]], active: str, restart_message: str,
) -> dict[str, str] | None:
    default = next(i for i, item in enumerate(entries, 1) if item['credentialId'] == active)
    print(f"Choose API key for '{name}' (same conversation history)")
    print(restart_message)
    for i, item in enumerate(entries, 1):
        marker = ' (current)' if i == default else ''
        print(f"[{i}] {item['name']}{marker}")
    while True:
        choice = input(f'Choose number or name [{default}], q to cancel: ').strip() or str(default)
        if choice.casefold() == 'q':
            return None
        if choice.isdecimal() and len(choice) <= 80:
            index = int(choice)
            if 1 <= index <= len(entries):
                return entries[index - 1]
        for item in entries:
            if item['name'].casefold() == choice.casefold():
                return item
        print('Choose a listed API key number or name.')


def choose_for_launch(profile: dict[str, Any], profiles: list[dict[str, Any]], api: Any) -> bool:
    try:
        entries = saved_keys(profile)
        if len(entries) < 2 or not sys.stdin.isatty():
            return True
        entry = prompt_key_choice(profile['name'], entries, profile['credentialId'],
                                  "Restart this profile's Desktop to apply a different key.")
        if entry is None:
            return False
        latest = use(str(profile['id']), entry['name'], api, expected_home=profile.get('home'))
        profile.update(latest)
        # Later preparation must not save an outdated key list over this choice.
        for other in profiles:
            if other.get('id') == profile['id']:
                other.update(latest)
        return True
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.')
        return False
    except (ApiKeyError, AccountError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
    except (KeyError, SecureStoreError, OSError):
        print('Error: could not read or save the selected API key; credentials were retained.', file=sys.stderr)
    return False


def main(args: list[str], api: Any) -> int:
    parser = argparse.ArgumentParser(prog='apicodex keys', description='Save named API keys within one profile and conversation history.')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('list', 'add', 'use'):
        item = sub.add_parser(command)
        item.add_argument('profile')
        if command != 'list':
            item.add_argument('name')
    try:
        ns = parser.parse_args(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if ns.command == 'add':
            add(ns.profile, ns.name, api)
        elif ns.command == 'use':
            use(ns.profile, ns.name, api)
            print('API key selected. Restart this profile\'s running clients to apply the change.')
        else:
            profile = _profile(registry_profiles(api), ns.profile, api)
            entries = saved_keys(profile) or [{'name': 'account-1', 'credentialId': profile.get('credentialId') or api.codex_credential_id(profile)}]
            for i, entry in enumerate(entries, 1):
                marker = ' (current)' if entry['credentialId'] == (profile.get('credentialId') or api.codex_credential_id(profile)) else ''
                print(f"[{i}] {entry['name']}{marker}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.')
        return 1
    except (ApiKeyError, AccountError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
    except (KeyError, SecureStoreError, OSError):
        print('Error: could not access saved API keys or profile metadata; credentials were retained.', file=sys.stderr)
    return 1
