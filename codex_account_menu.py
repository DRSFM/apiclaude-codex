"""Interactive account/API selection; identities are labels, homes stay stable."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import uuid

import codex_accounts as accounts

DEFAULT = {"id": "official-default", "name": "Official login", "type": "official_default"}
CANCEL = {"type": "menu_cancelled"}


def choose(profiles: list[dict[str, Any]], api: Any) -> dict[str, Any]:
    while True:
        apis = [p for p in profiles if p.get("type", "api_key") == "api_key"]
        print("Choose Codex profile")
        print("[0] Account login")
        api.show_codex_profiles(apis)
        print("[a] Add API profile   [q] Quit")
        recent = max(profiles, key=lambda p: p.get("lastUsedAt") or "", default=None)
        default = str(apis.index(recent) + 1) if recent in apis else "0"
        choice = input(f"Choose number or name [{default}]: ").strip() or default
        if choice.lower() == "q":
            return CANCEL
        if choice == "0":
            selected = choose_account(api)
            if selected is not None:
                return selected
            profiles = accounts.registry_profiles(api)
            continue
        if choice.lower() == "a":
            if api.add_codex_profile() == 0:
                profiles = accounts.registry_profiles(api)
            continue
        if choice.isdigit() and 1 <= int(choice) <= len(apis):
            return apis[int(choice) - 1]
        selected = api.find_profile(profiles, choice)
        if selected:
            return selected
        print("Profile not found; choose a listed number or name.")


def choose_account(api: Any) -> dict[str, Any] | None:
    while True:
        profiles = [p for p in accounts.registry_profiles(api) if accounts.is_chatgpt(p)]
        print("Account login")
        print("[1] Official login (same as ordinary codex)")
        for i, profile in enumerate(profiles, 2):
            print(f"[{i}] {profile['name']}")
        print("[a] Add profile   [b] Back   [q] Quit")
        choice = input("Choose number or name [1]: ").strip() or "1"
        if choice == "1":
            return DEFAULT
        if choice.lower() == "b":
            return None
        if choice.lower() == "q":
            return CANCEL
        if choice.lower() == "a":
            try:
                selected = add_account(api)
                if selected:
                    return selected
            except accounts.AccountError as exc:
                print(f"Error: {exc}")
            continue
        if choice.isdigit() and 2 <= int(choice) <= len(profiles) + 1:
            return profiles[int(choice) - 2]
        selected = api.find_profile(profiles, choice)
        if selected:
            return selected
        print("Account not found; choose a listed number or name.")


def _name(default: str, api: Any, *, current_id: str | None = None) -> str:
    while True:
        name = input(f"Profile name [{default}]: ").strip() or default
        try:
            accounts.validate_name(name)
            existing = api.find_profile(accounts.registry_profiles(api), name)
            if existing and existing.get("id") != current_id:
                print("That name is already in use. Choose a different name.")
                continue
            return name
        except accounts.AccountError as exc:
            print(f"Error: {exc}")


def add_account(api: Any) -> dict[str, Any] | None:
    print("Add account: [1] Official browser login  [2] OAuth JSON file  [3] Device login  [b] Back")
    method = input("Choose login method [1]: ").strip().lower() or "1"
    if method == "b":
        return None
    if method == "2":
        import codex_oauth as oauth
        path = Path(input("OAuth JSON file path: ").strip().strip('"')).expanduser()
        index = None
        try:
            try:
                auth, _, fingerprint = oauth.parse_file(path)
            except oauth.ImportError as exc:
                if "--index" not in str(exc):
                    raise
                value = input("Account number in the file (starting at 1): ").strip()
                if not value.isdigit() or int(value) < 1:
                    raise accounts.AccountError("Choose a positive account number.")
                index = int(value)
                auth, _, fingerprint = oauth.parse_file(path, index=index)
        except oauth.ImportError as exc:
            raise accounts.AccountError(str(exc)) from None
        # Reuse the current refreshed login instead of replaying an old export.
        duplicate = next((p for p in accounts.registry_profiles(api)
                          if accounts.is_chatgpt(p) and p.get("oauthIdentity") == fingerprint), None)
        if duplicate:
            accounts.report_import_reuse(duplicate)
            return duplicate
        default = oauth.account_email(auth) or "openai-" + uuid.uuid4().hex[:8]
        name = _name(default, api)
        args = ["import", name, "--file", str(path)]
        if index is not None:
            args += ["--index", str(index)]
        if accounts.main(args, api):
            return None
        return accounts.selected_profile(accounts.registry_profiles(api), name, api)
    if method not in {"1", "3"}:
        raise accounts.AccountError("Choose browser login, an OAuth file, or device login.")
    pending = "openai-" + uuid.uuid4().hex[:8]
    if accounts.main(["add", pending], api):
        return None
    profile = accounts.selected_profile(accounts.registry_profiles(api), pending, api)
    if accounts.main(["login", pending, *(["--device-auth"] if method == "3" else [])], api):
        print(f"Profile '{pending}' was retained. Retry with 'apicodex account login {pending}'.")
        return None
    account = accounts.read_account(accounts.profile_home(profile, api), api.find_codex_cli_executable(profile))
    if not account:
        raise accounts.AccountError("Official login has not completed; the profile was retained for retry.")
    name = _name(account.get("email") or pending, api, current_id=profile["id"])
    if accounts.main(["rename", pending, name], api):
        return None
    return accounts.selected_profile(accounts.registry_profiles(api), name, api)
