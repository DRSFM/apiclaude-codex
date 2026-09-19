"""Named ChatGPT profiles using the official Codex authentication lifecycle.

No credentials are copied from the default account. Normal launches delegate
refresh/re-authentication to Codex; an explicit login also reuses cached auth.
The CLI adapter is passed in so running apiagent.py never imports it twice.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from codex_app_server import AppServerError, CodexAppServer


class AccountError(ValueError):
    """User-facing errors must never contain credentials or raw server errors."""


class LoginRequired(AccountError):
    """Official authentication reported a permanent failure, not a network error."""

    def __init__(self, message: str, *, unreadable: bool = False) -> None:
        super().__init__(message)
        self.unreadable = unreadable


@contextmanager
def operation_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != path.absolute():
        raise AccountError("Refusing a redirected account operation lock.")
    with path.open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AccountError("Another account operation is running; retry when it finishes.") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def is_chatgpt(profile: dict[str, Any]) -> bool:
    return profile.get("type", "api_key") == "chatgpt"


def registry_profiles(api: Any) -> list[dict[str, Any]]:
    # Account operations must not migrate unrelated API secrets, and previews
    # must not initialize directories or rewrite registry defaults.
    try:
        if not api.CODEX_PROFILES_PATH.exists():
            return []
        data = json.loads(api.CODEX_PROFILES_PATH.read_text(encoding="utf-8-sig"))
        profiles = data.get("profiles", [])
        if not isinstance(profiles, list) or not all(isinstance(p, dict) for p in profiles):
            raise ValueError()
        return profiles
    except (ValueError, AttributeError, OSError):
        raise AccountError("Could not read the account profile registry.") from None


def environment_remove() -> tuple[str, ...]:
    # Start from the OS environment, not the calling API profile's auth/session.
    fixed = {
        "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID",
        "APICODEX_API_KEY", "AZURE_OPENAI_API_KEY", "CHATGPT_BASE_URL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "RUST_LOG", "RUST_BACKTRACE", "OTEL_EXPORTER_OTLP_HEADERS",
    }
    return tuple(sorted(fixed | {k for k in os.environ if k.upper().startswith(
        ("CODEX_", "APICODEX_", "OPENAI_"))}))


def clean_environment(home: Path) -> dict[str, str]:
    result = os.environ.copy()
    for key in environment_remove():
        result.pop(key, None)
    result["CODEX_HOME"] = str(home)
    return result


def launch_default(args: list[str], api: Any, *, desktop: bool = False) -> int:
    """The explicit official menu entry uses the ordinary default account home."""
    home = api.HOME / ".codex"
    if desktop:
        executable = api.find_codex_desktop_executable()
        if not executable or args:
            raise AccountError("Official Desktop was not found or received unsupported CLI arguments.")
        return api.start_detached_process(str(executable), [], env={"CODEX_HOME": str(home)},
                                          env_remove=environment_remove())
    executable = api.find_official_codex_cli_executable()
    if not executable:
        raise AccountError("Official Codex CLI was not found.")
    return api.run_command(executable, args, env={"CODEX_HOME": str(home)}, env_remove=environment_remove())


def sync_resources(profile: dict[str, Any], api: Any, *, dry_run: bool = False) -> dict[str, Any]:
    from codex_account_resources import ResourceError, sync
    try:
        home = profile_home(profile, api) if dry_run else ensure_config(profile, api)
        if dry_run:
            return sync(home, api.HOME / ".codex", api, dry_run=True)
        with operation_lock(api.CODEX_HOME / ".account-resources.lock"):
            report = sync(home, api.HOME / ".codex", api)
        if report["conflictsRetainedInBackup"]:
            print(f"Shared resources use the official defaults; previous local variants are retained in {report['backup']}.")
        if report.get('deferredDirectories'):
            print("Plugin cache is in use; close this account's Desktop and restart it to finish sharing the cache.")
        return report
    except (ResourceError, OSError, subprocess.SubprocessError):
        raise AccountError("Could not synchronize shared account resources; original resources/backups were retained.") from None


def profile_home(profile: dict[str, Any], api: Any) -> Path:
    if not is_chatgpt(profile) or not api.is_safe_api_profile_home(profile):
        raise AccountError("Select a safe ChatGPT account profile.")
    home = api.codex_profile_home(profile)
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}", str(profile.get("id", ""))):
        raise AccountError("Invalid account profile ID.")
    if home.resolve() == api.CODEX_HOME.resolve():
        raise AccountError("ChatGPT profiles require their own directory.")
    return home


def selected_profile(profiles: list[dict[str, Any]], name: str, api: Any) -> dict[str, Any]:
    selected = api.find_profile(profiles, name)
    if selected is None:
        raise AccountError("Account profile not found. Use 'apicodex account add NAME'.")
    profile_home(selected, api)
    return selected


def ensure_config(profile: dict[str, Any], api: Any) -> Path:
    home = profile_home(profile, api)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    if path.is_symlink():
        raise AccountError("Refusing a symlinked account config.")
    if not path.exists():
        lines = ['model_provider = "openai"', 'forced_login_method = "chatgpt"',
                 'cli_auth_credentials_store = "keyring"']
        if profile.get("model"):
            lines.append(f'model = {api.toml_basic_string(profile["model"])}')
        lines += ['', '[windows]', 'sandbox = "unelevated"', '', '[desktop]',
                  'conversationDetailMode = "STEPS_COMMANDS"', '']
        api.write_text_atomic(path, "\n".join(lines))
    else:
        # Do not overwrite saved model choices, trust, MCP, or user preferences.
        # These top-level routing values are the account profile's boundary.
        raw = path.read_text(encoding="utf-8-sig")
        top = raw.split("\n[", 1)[0]
        for key, expected in (("model_provider", "openai"),
                              ("forced_login_method", "chatgpt"),
                              ("cli_auth_credentials_store", "keyring")):
            match = re.search(rf'^\s*{key}\s*=\s*[\"\']([^\"\']+)[\"\']\s*(?:#.*)?$', top, re.M)
            if not match or match[1] != expected:
                raise AccountError(f"Account config must set {key} = {expected!r}.")
        if re.search(r'^\s*(openai_base_url|chatgpt_base_url|model_catalog_json)\s*=', top, re.M):
            raise AccountError("Account config must use the official service and model catalog.")
        if re.search(r'^\s*\[\s*[\"\']?model_providers(?:[\"\']?\.|[\"\']?\s*\])', raw, re.M):
            raise AccountError("Custom model providers are not supported in ChatGPT account profiles.")
    return home


def validate_args(args: list[str]) -> None:
    end = args.index("--") if "--" in args else len(args)
    values = args[:end]
    for i, arg in enumerate(values):
        if arg in {"--with-api-key", "--with-access-token", "--profile", "-p"} or arg.startswith("--profile="):
            raise AccountError("This option can replace account authentication; use a separate API profile.")
        value = None
        if arg in {"-c", "--config"} and i + 1 < len(values):
            value = values[i + 1]
        elif arg.startswith("--config="):
            value = arg.partition("=")[2]
        elif arg.startswith("-c") and len(arg) > 2:
            value = arg[2:]
        if value:
            key = value.partition("=")[0].strip().strip('"\'')
            if key.split(".")[0] in {"model_provider", "model_providers", "cli_auth_credentials_store",
                    "forced_login_method", "chatgpt_base_url", "openai_base_url", "profile", "profiles"}:
                raise AccountError("Authentication/provider overrides are not allowed for account profiles.")


def _auth_revision(home: Path) -> bytes | None:
    """Opaque revision only, for detecting a refresh by another official client."""
    try:
        with (home / "secrets" / "codex_auth.age").open("rb") as stream:
            raw = stream.read(2 * 1024 * 1024 + 4097)
        return hashlib.sha256(raw).digest()
    except OSError:
        return None


def read_account(home: Path, executable: str, *, refresh: bool = False) -> dict[str, Any] | None:
    # Reload from the canonical home on every check. Serialize wrapper checks;
    # this lock is not a lock on running official CLI/Desktop refresh workers.
    with operation_lock(home / ".apicodex-auth.lock"):
        return _read_account(home, executable, refresh=refresh)


def _read_account(home: Path, executable: str, *, refresh: bool,
                  retry_changed: bool = True) -> dict[str, Any] | None:
    before = _auth_revision(home)
    try:
        with CodexAppServer(home, codex_command=executable,
                            extra_env=clean_environment(home)) as client:
            result = client.request("account/read", {"refreshToken": refresh})
    except AppServerError as exc:
        if any(code in str(exc).lower() for code in (
            "refresh_token_reused", "refresh_token_expired", "refresh_token_invalid", "invalid_grant",
            "failed to decrypt secrets file",
        )):
            after = _auth_revision(home)
            if retry_changed and after is not None and after != before:
                # A rotating token can be stale in one process while another
                # has already saved its successor. Re-read once, without asking
                # for another refresh, before declaring the login unusable.
                return _read_account(home, executable, refresh=False, retry_changed=False)
            raise LoginRequired("Stored authentication is no longer usable; official login is required.",
                                unreadable="failed to decrypt secrets file" in str(exc).lower()) from None
        raise AccountError("Could not read account authentication; credentials were retained. Retry when the service is available.") from None
    except OSError:
        raise AccountError("Could not start the official account authentication check.") from None
    account = result.get("account")
    if account is None:
        return None
    if not isinstance(account, dict) or account.get("type") != "chatgpt":
        raise AccountError("This profile does not contain ChatGPT authentication.")
    return account


def mask_identity(email: str) -> str:
    if "@" not in email:
        return "(unknown)"
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain[:1]}***"


def login(profile: dict[str, Any], api: Any, *, device: bool = False) -> int:
    home = ensure_config(profile, api)
    executable = api.find_codex_cli_executable(profile)
    if not executable:
        raise AccountError("Official Codex CLI was not found.")
    # Running official clients own their refresh; do not ask a second process
    # to refresh the same token chain merely to check an already open profile.
    busy = profile_busy(profile, api)
    # account/read performs the official managed refresh when required. Never
    # destroy unreadable credentials to 'repair' an OAuth account with an API key.
    unreadable = False
    try:
        account = read_account(home, executable, refresh=not busy)
    except LoginRequired as exc:
        account = None
        unreadable = exc.unreadable
    if account:
        print(f"Reusing ChatGPT login for '{profile['name']}' ({mask_identity(account.get('email', ''))}).")
        return 0
    args = ["login"] + (["--device-auth"] if device else [])
    if busy or profile_busy(profile, api):
        raise AccountError("Close this profile's sessions before replacing its unusable login.")
    encrypted = home / "secrets" / "codex_auth.age"
    if encrypted.is_file():
        if encrypted.resolve() != encrypted.absolute():
            raise AccountError("Refusing redirected authentication storage.")
        if unreadable:
            api.archive_unreadable_codex_auth(home)
        else:
            shutil.copyfile(encrypted, encrypted.with_name(f"codex_auth.before-login-{uuid.uuid4().hex}.age"))
    return api.run_command(executable, args, env={"CODEX_HOME": str(home)}, env_remove=environment_remove())


@contextmanager
def launch_lease(home: Path) -> Iterator[None]:
    directory = home / ".apicodex-runs"
    directory.mkdir(exist_ok=True)
    path = directory / f"{os.getpid()}-{uuid.uuid4().hex}.json"
    path.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def launch(profile: dict[str, Any], args: list[str], api: Any, *, desktop: bool = False) -> int:
    if args and args[0] in {"login", "logout"}:
        return _launch(profile, args, api, desktop=desktop)
    try:
        # Register the waiting launcher under the same lock used by archive.
        # Otherwise archive could remove a profile just before launch recreates it.
        with operation_lock(api.CODEX_HOME / ".account-operation.lock"):
            current = selected_profile(registry_profiles(api), str(profile["id"]), api)
            home = ensure_config(current, api)
            lease = launch_lease(home)
            lease.__enter__()
        try:
            return _launch(current, args, api, desktop=desktop)
        finally:
            lease.__exit__(None, None, None)
    except (AccountError, OSError) as exc:
        print(f"Error: {exc if isinstance(exc, AccountError) else 'Account launch failed.'}", file=sys.stderr)
        return 1


def _launch(profile: dict[str, Any], args: list[str], api: Any, *, desktop: bool = False) -> int:
    try:
        validate_args(args)
        home = ensure_config(profile, api)
        if args and args[0] == "login":
            if args[1:] == ["status"]:
                return main(["status", profile["name"]], api)
            if args[1:] not in ([], ["--device-auth"]):
                raise AccountError("Use 'apicodex account login NAME [--device-auth]'.")
            return main(["login", profile["name"], *(["--device-auth"] if "--device-auth" in args else [])], api)
        if args and args[0] == "logout":
            return main(["logout", profile["name"]], api)
        shared_change = (
            len(args) > 1 and args[0] == "mcp" and args[1] in {"add", "remove"}
        ) or (
            len(args) > 1 and args[0] == "plugin" and (
                args[1] in {"install", "uninstall", "enable", "disable"}
                or len(args) > 2 and args[1] == "marketplace" and args[2] in {"add", "remove", "upgrade"}
            )
        )
        if shared_change:
            print("Updating shared configuration in the official default account directory.")
            code = launch_default(args, api)
            if code == 0:
                sync_resources(profile, api)
            return code
        sync_resources(profile, api)
        if desktop:
            if os.name != "nt":
                raise AccountError("Account Desktop launch is currently supported only on Windows.")
            if args:
                raise AccountError("Desktop does not accept CLI arguments; set the account default model instead.")
            executable = api.find_codex_desktop_executable()
            if not executable:
                raise AccountError("Official Codex Desktop was not found.")
            data = api.CODEX_DESKTOP_DATA_ROOT / str(profile["id"])
            if data.resolve() != data.absolute():
                raise AccountError("Refusing a redirected Desktop directory.")
            data.mkdir(parents=True, exist_ok=True)
            code = api.start_detached_process(str(executable), [f"--user-data-dir={data}"],
                    env={"CODEX_HOME": str(home)}, env_remove=environment_remove())
            if code == 0:
                with operation_lock(api.CODEX_HOME / ".account-operation.lock"):
                    api.update_codex_last_used(profile)
                api.label_codex_desktop_window(data, str(profile["name"]), executable)
            return code
        executable = api.find_codex_cli_executable(profile)
        if not executable:
            raise AccountError("Official Codex CLI was not found.")
        api.add_current_project_trust(home)
        with operation_lock(api.CODEX_HOME / ".account-operation.lock"):
            api.update_codex_last_used(profile)
        return api.run_command(executable, args, env={"CODEX_HOME": str(home)},
                               env_remove=environment_remove())
    except (AccountError, OSError):
        # OS errors can include source paths; never echo raw auth/server errors.
        error = sys.exc_info()[1]
        print(f"Error: {error if isinstance(error, AccountError) else 'Account launch failed.'}", file=sys.stderr)
        return 1


def create_profile(name: str, model: str | None, api: Any) -> dict[str, Any]:
    profile = new_profile(name, model, api)
    profiles = registry_profiles(api)
    ensure_config(profile, api)
    api.save_codex_profiles([*profiles, profile])
    return profile


def new_profile(name: str, model: str | None, api: Any) -> dict[str, Any]:
    name = name.strip()
    validate_name(name)
    profiles = registry_profiles(api)
    if api.find_profile(profiles, name):
        raise AccountError("A profile with this name already exists; use it or choose another name.")
    identity = "chatgpt-" + uuid.uuid4().hex
    profile = {"id": identity, "name": name, "type": "chatgpt", "home": f"accounts/{identity}",
               "createdAt": api.now_iso(), "lastUsedAt": None, "useCustomCodexCli": False}
    if model:
        profile["model"] = model
    return profile


def validate_name(name: str) -> None:
    if not name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name) or len(name) > 254:
        raise AccountError("Choose an account name of 1–254 printable characters.")


def rename_profile(profile: dict[str, Any], name: str, api: Any) -> None:
    name = name.strip()
    validate_name(name)
    profiles = registry_profiles(api)
    current = selected_profile(profiles, profile["id"], api)
    conflict = api.find_profile(profiles, name)
    if conflict and conflict["id"] != current["id"]:
        raise AccountError("Another profile already uses that name or alias.")
    previous = current["name"]
    if previous == name:
        return
    aliases = [a for a in current.get("aliases", []) if isinstance(a, str) and a.casefold() != name.casefold()]
    current["aliases"] = list(dict.fromkeys([*aliases, previous]))
    current["name"] = name
    api.save_codex_profiles(profiles)
    print(f"Renamed '{previous}' to '{name}'; its directory and login are unchanged.")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="apicodex account", description="Isolated ChatGPT subscription accounts")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="create an account profile without opening a login page")
    add.add_argument("name", nargs="?", help="omit to choose browser login or OAuth import interactively")
    add.add_argument("--model")
    imp = sub.add_parser("import", help="import an explicitly selected OAuth JSON into official encrypted storage")
    imp.add_argument("name", nargs="?", help="defaults to the email in the selected OAuth export")
    imp.add_argument("--file", required=True, type=Path)
    imp.add_argument("--index", type=int)
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--update", action="store_true", help="explicitly replace the selected profile's credentials")
    ls = sub.add_parser("list", help="list named accounts without reading credentials")
    ls.add_argument("--json", action="store_true")
    auth = sub.add_parser("login", help="reuse/refresh stored auth, sign in only when needed")
    auth.add_argument("name")
    auth.add_argument("--device-auth", action="store_true")
    status = sub.add_parser("status", help="ask official Codex to read the account")
    status.add_argument("name")
    status.add_argument("--refresh", action="store_true")
    status.add_argument("--json", action="store_true")
    for name in ("logout", "archive"):
        command = sub.add_parser(name)
        command.add_argument("name")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--yes", action="store_true")
    model = sub.add_parser("model", help="set the default model without changing login")
    model.add_argument("name")
    model.add_argument("model")
    rename = sub.add_parser("rename", help="change a display name while retaining the directory and old alias")
    rename.add_argument("name")
    rename.add_argument("new_name")
    shared = sub.add_parser("sync", help="link shared resources and follow the official default configuration")
    shared.add_argument("name", nargs="?")
    shared.add_argument("--all", action="store_true")
    shared.add_argument("--dry-run", action="store_true")
    return parser


def main(args: list[str], api: Any) -> int:
    # Serialize account registry mutations; CLI sessions themselves remain parallel.
    try:
        if args == ["add"]:
            from codex_account_menu import add_account
            return 0 if add_account(api) else 1
        if args and args[0] in {"add", "model", "archive", "logout", "login", "import", "rename", "sync", "status"} and not any(x in args for x in ("--help", "-h", "--dry-run")):
            with operation_lock(api.CODEX_HOME / ".account-operation.lock"):
                return _main(args, api)
        return _main(args, api)
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 0
    except (AccountError, OSError) as exc:
        print(f"Error: {exc if isinstance(exc, AccountError) else 'Could not lock the account registry.'}", file=sys.stderr)
        return 1


def _main(args: list[str], api: Any) -> int:
    try:
        ns = _parser().parse_args(args)
    except SystemExit as exc:
        return int(exc.code or 0)
    try:
        if ns.command == "import":
            return import_account(ns, api)
        if ns.command == "add":
            if not ns.name:
                raise AccountError("Use 'account add' for the guided flow, or provide a name with --model.")
            profile = create_profile(ns.name, ns.model, api)
            print(f"Created ChatGPT profile '{profile['name']}'. Import credentials or use 'apicodex account login {profile['name']}'.")
            return 0
        profiles = registry_profiles(api)
        if ns.command == "sync":
            if bool(ns.name) == ns.all:
                raise AccountError("Choose one account name or --all.")
            selected = [p for p in profiles if is_chatgpt(p)] if ns.all else [selected_profile(profiles, ns.name, api)]
            for profile in selected:
                print(json.dumps({"name": profile["name"], **sync_resources(profile, api, dry_run=ns.dry_run)}))
            return 0
        if ns.command == "list":
            selected = [p for p in profiles if is_chatgpt(p)]
            if ns.json:
                api.show_codex_profiles_json(selected)
            else:
                api.show_codex_profiles(selected)
            return 0
        profile = selected_profile(profiles, ns.name, api)
        if ns.command == "rename":
            rename_profile(profile, ns.new_name, api)
            return 0
        if ns.command in {"logout", "archive"}:
            return lifecycle(ns, profile, profiles, api)
        home = ensure_config(profile, api)
        if ns.command == "login":
            return login(profile, api, device=ns.device_auth)
        if ns.command == "status":
            if ns.refresh and profile_busy(profile, api):
                raise AccountError("This profile is in use; its official client manages refresh. Use status without --refresh, or close its sessions first.")
            executable = api.find_codex_cli_executable(profile)
            if not executable:
                raise AccountError("Official Codex CLI was not found.")
            account = read_account(home, executable, refresh=ns.refresh)
            result = {"name": profile["name"], "type": "chatgpt", "authenticated": bool(account),
                      "identity": mask_identity((account or {}).get("email", "")),
                      "verification": "official-auth-recognition-only"}
            print(json.dumps(result) if ns.json else f"{profile['name']}: {'ChatGPT' if account else 'not logged in'} ({result['identity']})")
            return 0
        if ns.command == "model":
            raw = (home / "config.toml").read_text(encoding="utf-8-sig")
            top, sep, rest = raw.partition("\n[")
            top = re.sub(r'^\s*model\s*=.*\n?', '', top, flags=re.M)
            api.write_text_atomic(home / "config.toml", f'model = {api.toml_basic_string(ns.model)}\n'+top+sep+rest)
            profile["model"] = ns.model
            api.save_codex_profiles(profiles)
            print(f"Default model saved for '{profile['name']}'.")
            return 0
        raise AccountError("Unsupported account operation.")
    except (AccountError, OSError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc if isinstance(exc, AccountError) else 'Account operation failed.'}", file=sys.stderr)
        return 1


def report_import_reuse(profile: dict[str, Any]) -> None:
    print(f"Reusing existing Profile '{profile['name']}' only; login was not checked or restored.")
    print("If logged out, use 'apicodex account login NAME', or import a current OAuth export "
          "into this existing Profile with --update. Saved credentials were not replaced.")


def import_account(ns: argparse.Namespace, api: Any) -> int:
    import codex_oauth as oauth
    try:
        auth, preview, fingerprint = oauth.parse_file(ns.file, index=ns.index)
        requested_name = ns.name or oauth.account_email(auth) or "openai-" + uuid.uuid4().hex[:8]
        profiles = registry_profiles(api)
        original_profiles = copy.deepcopy(profiles)
        target = api.find_profile(profiles, requested_name)
        if target is not None:
            profile_home(target, api)
        duplicate = next((p for p in profiles if is_chatgpt(p) and p.get("oauthIdentity") == fingerprint), None)
        if duplicate is not None and duplicate is not target:
            report_import_reuse(duplicate)
            return 0
        if target is not None and target.get("oauthIdentity") == fingerprint and not ns.update:
            report_import_reuse(target)
            return 0
        if target is not None and not ns.update:
            raise AccountError("This profile already exists. Use --update to explicitly import credentials into it.")
        target = target or new_profile(requested_name, None, api)
        print(json.dumps({"name": target["name"], "dryRun": ns.dry_run, **preview}))
        if ns.dry_run:
            return 0
        if os.name != "nt":
            raise AccountError("OAuth file import currently supports Windows; use 'account login' on this platform.")
        oauth._crypto()  # Fail before creating the profile when optional dependency is absent.
        if profile_busy(target, api):
            raise AccountError("Close this profile's CLI/Desktop sessions before importing credentials.")
        executable = api.find_codex_cli_executable(target)
        if not executable:
            raise AccountError("Official Codex CLI was not found.")
        home = ensure_config(target, api)
        try:
            api.ensure_private_desktop_directory(home)
        except Exception:
            raise AccountError("Could not protect the account directory; no credentials were imported.") from None
        # Explicit import only. Subsequent starts use the refreshed official store,
        # never replaying the original source file.
        with oauth.save_auth(home, auth):
            account = read_account(home, executable)
            if not account:
                raise AccountError("Official Codex did not recognize the imported login; previous credentials restored.")
            target["oauthIdentity"] = fingerprint
            target["oauthRefreshCapable"] = bool(auth["tokens"]["refresh_token"])
            if not api.find_profile(profiles, target["id"]):
                profiles.append(target)
            try:
                api.save_codex_profiles(profiles)
                if registry_profiles(api) != profiles:
                    raise AccountError("Account registry readback failed.")
            except BaseException:
                api.save_codex_profiles(original_profiles)
                raise
        print("Imported into official encrypted storage; official local auth recognition passed. Network requests and token refresh are not yet verified.")
        return 0
    except (oauth.ImportError, AccountError, OSError, subprocess.SubprocessError) as exc:
        print(f"Error: {exc if isinstance(exc, (oauth.ImportError, AccountError)) else 'OAuth import failed; source file was not changed.'}", file=sys.stderr)
        return 1


def lifecycle(ns: argparse.Namespace, profile: dict[str, Any], profiles: list[dict[str, Any]], api: Any) -> int:
    home = profile_home(profile, api)
    if ns.dry_run:
        print(json.dumps({"operation": ns.command, "name": profile["name"], "home": str(home),
                          "dryRun": True, "preserveCredentials": ns.command == "archive"}))
        return 0
    if not ns.yes and input(f"{ns.command.title()} '{profile['name']}'? Type YES: ") != "YES":
        print("Cancelled.")
        return 0
    if profile_busy(profile, api):
        raise AccountError("Close this profile's CLI/Desktop sessions before changing its login or archiving it.")
    if ns.command == "logout":
        executable = api.find_codex_cli_executable(profile)
        if not executable:
            raise AccountError("Official Codex CLI was not found.")
        return api.run_command(executable, ["logout"], env={"CODEX_HOME": str(home)}, env_remove=environment_remove())
    # Keep the original path identity in the manifest: official keyring entries
    # are scoped to that path. Restoring it restores access to the encrypted auth.
    archive = api.CODEX_ARCHIVE_ROOT / f"{profile['id']}-{uuid.uuid4().hex[:12]}"
    data = api.CODEX_DESKTOP_DATA_ROOT / str(profile["id"])
    if data.resolve() != data.absolute():
        raise AccountError("Refusing a redirected Desktop directory.")
    archive.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    try:
        api.write_json_atomic(archive / "profile.json", {"version": 1, "profile": profile,
                              "originalHome": str(home), "originalDesktopData": str(data)})
        for source, target in ((home, archive / "home"), (data, archive / "desktop")):
            if source.exists():
                source.rename(target)
                moved.append((source, target))
        api.save_codex_profiles([p for p in profiles if p.get("id") != profile["id"]])
    except BaseException:
        for source, target in reversed(moved):
            if not source.exists():
                target.rename(source)
        raise
    print(f"Archived '{profile['name']}' to {archive}. Login and history retained; see profile.json for original paths.")
    return 0


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        dll = ctypes.WinDLL("kernel32", use_last_error=True)
        dll.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        dll.OpenProcess.restype = ctypes.c_void_p
        dll.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = dll.OpenProcess(0x1000, False, pid)
        if handle:
            dll.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5  # access denied is not proof of exit
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def profile_busy(profile: dict[str, Any], api: Any) -> bool:
    home = profile_home(profile, api)
    for marker in (home / ".apicodex-runs").glob("*.json"):
        try:
            if pid_alive(int(json.loads(marker.read_text())["pid"])):
                return True
        except (ValueError, KeyError, OSError):
            raise AccountError("Could not verify this profile's running sessions.") from None
    if os.name != "nt":
        return False
    data = api.CODEX_DESKTOP_DATA_ROOT / str(profile["id"])
    if not data.exists():
        return False
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise AccountError("PowerShell is required to check running Desktop sessions.")
    script = r'''
$ErrorActionPreference = 'Stop'
$wanted = [IO.Path]::GetFullPath($env:APICODEX_CHECK_DATA).TrimEnd('\')
$found = @(Get-CimInstance Win32_Process -Filter "Name='ChatGPT.exe'" | Where-Object {
  $m = [regex]::Match($_.CommandLine, '(?i)(?:^|\s)(?:"--user-data-dir=(?<q>[^"]+)"|--user-data-dir=(?<b>\S+))')
  if (-not $m.Success) { return $false }
  $value = if ($m.Groups['q'].Success) { $m.Groups['q'].Value } else { $m.Groups['b'].Value }
  [IO.Path]::GetFullPath($value).TrimEnd('\').Equals($wanted, [StringComparison]::OrdinalIgnoreCase)
})
if ($found.Count -gt 0) { exit 3 } else { exit 0 }
'''
    env = clean_environment(home)
    env["APICODEX_CHECK_DATA"] = str(data)
    result = subprocess.run([shell, "-NoProfile", "-Command", script], env=env,
                            capture_output=True, timeout=20,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode not in (0, 3):
        raise AccountError("Could not verify this profile's Desktop processes.")
    return result.returncode == 3
