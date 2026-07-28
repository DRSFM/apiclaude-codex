#!/usr/bin/env python3
"""Command-line workflow for ApiCodex portable conversation sharing."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from codex_app_server import (
    AppServerError,
    CodexAppServer,
    detect_fork_path_capability,
)
from codex_conversation_pool import (
    CommitRecord,
    ConversationPool,
    ConversationPoolError,
    LocalMappingStore,
    MappingRecord,
    PoolSecurity,
    SnapshotChangedError,
    audit_jsonl_no_encrypted_content,
    audit_target_runtime_context,
    materialize_snapshot_for_target,
    sanitize_rollout,
    semantic_snapshot_hash,
)


@dataclass(frozen=True)
class ShareTarget:
    id: str
    label: str
    home: Path
    model_provider: str
    model: str | None


@dataclass
class ShareContext:
    account_home: Path
    api_root: Path
    local_state_root: Path
    load_api_profiles: Callable[[], list[dict[str, Any]]]
    codex_command: str = "codex"
    pool_security: PoolSecurity | None = None
    app_server_factory: Callable[..., CodexAppServer] = CodexAppServer


def default_local_state_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "apicodex" / "conversation-pool"
    return Path.home() / ".local" / "share" / "apicodex" / "conversation-pool"


def _parse_config_value(path: Path, name: str, default: str) -> str:
    config_path = path / "config.toml"
    if not config_path.is_file():
        return default
    try:
        raw = config_path.read_text(encoding="utf-8-sig")
    except OSError:
        return default
    match = re.search(
        rf'(?m)^\s*{re.escape(name)}\s*=\s*"([^"]+)"',
        raw,
    )
    return match.group(1) if match else default


def _safe_api_home(api_root: Path, profile: dict[str, Any]) -> Path:
    raw = profile.get("home", ".")
    if not isinstance(raw, str) or not raw:
        raise ConversationPoolError("API Profile has an invalid home")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ConversationPoolError(
            f"API Profile has an unsafe home: {raw!r}"
        )
    candidate = api_root if raw == "." else api_root / relative
    expected = candidate.absolute()
    resolved = candidate.resolve()
    if resolved != expected or (candidate.exists() and candidate.is_symlink()):
        raise ConversationPoolError(
            f"API Profile home is linked or escapes isolation: {candidate}"
        )
    return resolved


def _resolve_windows_extended_path(path: Path) -> Path:
    """Normalize app-server extended-length paths without accepting devices."""

    raw = str(path)
    if os.name == "nt":
        lowered = raw.lower()
        if lowered.startswith("\\\\?\\unc\\"):
            raw = "\\\\" + raw[8:]
        elif lowered.startswith("\\\\?\\"):
            raw = raw[4:]
        elif lowered.startswith("\\\\.\\"):
            raise ConversationPoolError(
                f"thread rollout uses an unsupported device path: {path}"
            )
    return Path(raw).resolve()


def _targets(context: ShareContext, *, include_profiles: bool) -> list[ShareTarget]:
    account_home = context.account_home.resolve()
    targets = [
        ShareTarget(
            id="account",
            label="Account Codex",
            home=account_home,
            model_provider=_parse_config_value(
                account_home,
                "model_provider",
                "openai",
            ),
            model=_parse_config_value(account_home, "model", "") or None,
        )
    ]
    if not include_profiles:
        return targets
    for profile in context.load_api_profiles():
        profile_id = str(profile.get("id") or profile.get("name") or "").strip()
        if not profile_id:
            continue
        profile_home = _safe_api_home(context.api_root.resolve(), profile)
        targets.append(
            ShareTarget(
                id=f"api:{profile_id}",
                label=f"API Profile: {profile.get('name') or profile_id}",
                home=profile_home,
                model_provider=_parse_config_value(
                    profile_home,
                    "model_provider",
                    "apicodex",
                ),
                model=str(profile.get("model") or "").strip()
                or _parse_config_value(profile_home, "model", "")
                or None,
            )
        )
    return targets


def _select_target(args: argparse.Namespace, context: ShareContext) -> ShareTarget:
    if args.account and args.api_profile:
        raise ConversationPoolError(
            "--account and --api-profile are mutually exclusive"
        )
    if args.account:
        target = _targets(context, include_profiles=False)[0]
        if not target.home.is_dir():
            raise ConversationPoolError(
                f"account CODEX_HOME does not exist: {target.home}"
            )
        return target
    targets = _targets(context, include_profiles=True)
    if args.api_profile:
        requested = args.api_profile.lower()
        for target in targets[1:]:
            if (
                target.id.removeprefix("api:").lower() == requested
                or target.label.removeprefix("API Profile: ").lower() == requested
            ):
                if not target.home.is_dir():
                    raise ConversationPoolError(
                        f"API Profile CODEX_HOME does not exist: {target.home}"
                    )
                return target
        raise ConversationPoolError(
            f"API Profile {args.api_profile!r} was not found"
        )
    available = [target for target in targets if target.home.is_dir()]
    if not available:
        raise ConversationPoolError("no account or API Profile CODEX_HOME is available")
    if len(available) == 1:
        return available[0]
    print("Choose source/target Codex Profile", file=sys.stderr)
    for index, target in enumerate(available, 1):
        print(f"[{index}] {target.label}  {target.home}", file=sys.stderr)
    choice = input("Choose number [1]: ").strip()
    if not choice:
        return available[0]
    if choice.isdigit() and 1 <= int(choice) <= len(available):
        return available[int(choice) - 1]
    lowered = choice.lower()
    for target in available:
        if target.id.lower() == lowered or target.label.lower() == lowered:
            return target
    raise ConversationPoolError(f"Codex Profile {choice!r} was not found")


def _thread_status_type(thread: dict[str, Any]) -> str:
    status = thread.get("status")
    if isinstance(status, dict):
        value = status.get("type")
        return str(value or "")
    return str(status or "")


def _thread_title(thread: dict[str, Any]) -> str:
    for key in ("name", "preview"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(thread.get("id") or "Shared conversation")


def _validate_rollout_path(target: ShareTarget, value: Any) -> Path:
    if not isinstance(value, str) or not value:
        raise ConversationPoolError("thread has no local rollout path")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ConversationPoolError("thread rollout path is not absolute")
    resolved = _resolve_windows_extended_path(candidate)
    allowed_roots = [
        target.home / "sessions",
        target.home / "archived_sessions",
    ]
    if not any(
        resolved == root.resolve() or resolved.is_relative_to(root.resolve())
        for root in allowed_roots
    ):
        raise ConversationPoolError(
            f"thread rollout escapes the selected CODEX_HOME: {resolved}"
        )
    if not resolved.is_file() or resolved.is_symlink():
        raise ConversationPoolError(
            f"thread rollout is not a regular file: {resolved}"
        )
    return resolved


def _select_thread(
    client: CodexAppServer,
    target: ShareTarget,
    requested: str | None,
) -> dict[str, Any]:
    if requested:
        thread = client.read_thread(requested, include_turns=False)
    else:
        threads = [
            item
            for item in client.list_threads(limit=100)
            if isinstance(item.get("path"), str) and item.get("path")
        ]
        if not threads:
            raise ConversationPoolError(
                f"{target.label} has no local conversations"
            )
        print(f"Choose a conversation from {target.label}", file=sys.stderr)
        for index, item in enumerate(threads, 1):
            status = _thread_status_type(item) or "-"
            preview = _thread_title(item).replace("\n", " ")[:80]
            print(
                f"[{index}] {preview}  {str(item.get('id'))[:12]}  {status}",
                file=sys.stderr,
            )
        choice = input("Choose number [1]: ").strip()
        if not choice:
            thread = threads[0]
        elif choice.isdigit() and 1 <= int(choice) <= len(threads):
            thread = threads[int(choice) - 1]
        else:
            matches = [
                item
                for item in threads
                if str(item.get("id") or "").startswith(choice)
            ]
            if len(matches) != 1:
                raise ConversationPoolError(
                    f"conversation selector {choice!r} is not unique"
                )
            thread = matches[0]
        thread = client.read_thread(str(thread["id"]), include_turns=False)
    status = _thread_status_type(thread)
    if status == "active":
        raise SnapshotChangedError(
            "the selected conversation has an active turn; wait for it to finish"
        )
    if status == "systemError":
        raise ConversationPoolError(
            "the selected conversation is in a system error state"
        )
    _validate_rollout_path(target, thread.get("path"))
    return thread


def _commit_payload(commit: CommitRecord) -> dict[str, Any]:
    payload = asdict(commit)
    payload["shortId"] = commit.id[:12]
    return payload


def _mapping_payload(mapping: MappingRecord) -> dict[str, Any]:
    payload = asdict(mapping)
    payload["baseCommitShort"] = mapping.base_commit[:12]
    return payload


def _emit(payload: Any, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    if isinstance(payload, str):
        print(payload)
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _state_and_pool(
    args: argparse.Namespace,
    context: ShareContext,
) -> tuple[LocalMappingStore, ConversationPool]:
    state = LocalMappingStore(context.local_state_root)
    pool_path = Path(args.pool).resolve() if args.pool else state.get_pool_path()
    pool = ConversationPool(pool_path, security=context.pool_security)
    pool.verify_initialized()
    return state, pool


def _new_app_server(
    context: ShareContext,
    target: ShareTarget,
) -> CodexAppServer:
    return context.app_server_factory(
        target.home,
        codex_command=context.codex_command,
    )


def _target_by_id(context: ShareContext, target_id: str) -> ShareTarget:
    requested = str(target_id or "").strip().lower()
    for target in _targets(context, include_profiles=True):
        if target.id.lower() == requested:
            if not target.home.is_dir():
                raise ConversationPoolError(
                    f"{target.label} CODEX_HOME does not exist: {target.home}"
                )
            return target
    raise ConversationPoolError(f"Codex target {target_id!r} was not found")


def list_share_targets(context: ShareContext) -> list[dict[str, Any]]:
    """Return non-sensitive account/Profile choices for a migration UI."""

    targets: list[dict[str, Any]] = []
    for target in _targets(context, include_profiles=True):
        kind = "account" if target.id == "account" else "api"
        name = (
            "Account Codex"
            if kind == "account"
            else target.label.removeprefix("API Profile: ")
        )
        targets.append(
            {
                "id": target.id,
                "name": name,
                "label": target.label,
                "kind": kind,
                "modelProvider": target.model_provider,
                "model": target.model,
                "available": target.home.is_dir(),
            }
        )
    return targets


def list_share_threads(
    context: ShareContext,
    target_id: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List safe local thread metadata without exposing rollout paths."""

    target = _target_by_id(context, target_id)
    with _new_app_server(context, target) as client:
        threads = client.list_threads(limit=max(1, min(limit, 200)))
    result: list[dict[str, Any]] = []
    for thread in threads:
        try:
            _validate_rollout_path(target, thread.get("path"))
        except ConversationPoolError:
            continue
        status = _thread_status_type(thread) or "idle"
        result.append(
            {
                "id": str(thread.get("id") or ""),
                "title": _thread_title(thread),
                "preview": str(thread.get("preview") or ""),
                "status": status,
                "available": status not in {"active", "systemError"},
                "cwd": str(thread.get("cwd") or ""),
                "createdAt": thread.get("createdAt"),
                "updatedAt": thread.get("updatedAt"),
            }
        )
    return result


def copy_share_thread(
    context: ShareContext,
    *,
    source_target_id: str,
    target_target_id: str,
    thread_id: str,
    lineage_name: str | None = None,
    cwd: Path | None = None,
    title: str | None = None,
    message: str = "Copy conversation between Codex targets",
) -> dict[str, Any]:
    """Publish one idle thread and clone it into another Codex target."""

    source = _target_by_id(context, source_target_id)
    target = _target_by_id(context, target_target_id)
    if source.id == target.id or source.home == target.home:
        raise ConversationPoolError("source and target Codex profiles must be different")

    state = LocalMappingStore(context.local_state_root)
    pool = ConversationPool(
        state.get_pool_path(),
        security=context.pool_security,
    )
    pool.verify_initialized()
    shared_name = lineage_name or (
        f"transfer-{str(thread_id)[:8]}-{uuid.uuid4().hex[:8]}"
    )

    with _new_app_server(context, source) as client:
        thread, snapshot, temporary = _sanitize_thread(
            client,
            source,
            thread_id,
        )
        try:
            source_title = _thread_title(thread)
            commit = pool.publish(
                shared_name,
                snapshot,
                title=source_title,
                source_target=source.id,
                message=message,
            )
            state.register(
                pool_id=pool.pool_id(),
                target_id=source.id,
                target_home=source.home,
                thread_id=str(thread["id"]),
                lineage_id=commit.lineage_id,
                lineage_name=commit.lineage_name,
                ref_name="main",
                base_commit=commit.id,
                rollout_path=_validate_rollout_path(
                    source,
                    thread.get("path"),
                ),
            )
        finally:
            temporary.cleanup()

    target_cwd = Path(cwd).resolve() if cwd else Path(commit.cwd).resolve()
    target_title = title or f"[shared] {source_title}"
    clone = _clone_commit(
        commit=commit,
        pool=pool,
        state=state,
        target=target,
        cwd=target_cwd,
        title=target_title,
        context=context,
    )
    public_clone = {
        key: clone[key]
        for key in ("threadId", "title", "cwd", "modelProvider", "model")
        if key in clone
    }
    return {
        "ok": True,
        "operation": "copy",
        "sharedName": commit.lineage_name,
        "commit": _commit_payload(commit),
        "source": {
            "targetId": source.id,
            "targetLabel": source.label,
            "threadId": str(thread["id"]),
            "title": source_title,
        },
        "target": {
            "targetId": target.id,
            "targetLabel": target.label,
            **public_clone,
        },
    }


def _sanitize_thread(
    client: CodexAppServer,
    target: ShareTarget,
    thread_id: str | None,
) -> tuple[dict[str, Any], Any, tempfile.TemporaryDirectory[str]]:
    thread = _select_thread(client, target, thread_id)
    rollout_path = _validate_rollout_path(target, thread.get("path"))
    temporary = tempfile.TemporaryDirectory(prefix="apicodex-share-")
    try:
        snapshot = sanitize_rollout(
            rollout_path,
            Path(temporary.name) / "portable.jsonl",
            expected_thread_id=str(thread.get("id") or ""),
        )
    except BaseException:
        temporary.cleanup()
        raise
    return thread, snapshot, temporary


def _handle_init(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    state = LocalMappingStore(context.local_state_root)
    pool_path = Path(args.pool).resolve() if args.pool else state.get_pool_path()
    result = ConversationPool.initialize(
        pool_path,
        security=context.pool_security,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        state.initialize()
        state.set_pool_path(pool_path)
    result["efsCertificateReminder"] = (
        "Back up the current user's EFS certificate and private key."
    )
    return result


def _handle_publish(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    state, pool = _state_and_pool(args, context)
    target = _select_target(args, context)
    with _new_app_server(context, target) as client:
        thread, snapshot, temporary = _sanitize_thread(
            client,
            target,
            args.thread,
        )
        try:
            commit = pool.publish(
                args.name,
                snapshot,
                title=_thread_title(thread),
                source_target=target.id,
                message=args.message,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                state.register(
                    pool_id=pool.pool_id(),
                    target_id=target.id,
                    target_home=target.home,
                    thread_id=str(thread["id"]),
                    lineage_id=commit.lineage_id,
                    lineage_name=commit.lineage_name,
                    ref_name="main",
                    base_commit=commit.id,
                    rollout_path=_validate_rollout_path(target, thread.get("path")),
                )
            return {
                "ok": True,
                "operation": "publish",
                "dryRun": args.dry_run,
                "target": target.label,
                "threadId": thread["id"],
                "snapshot": {
                    "sha256": snapshot.sha256,
                    "bytes": snapshot.size,
                    "keptRows": snapshot.kept_rows,
                    "droppedRows": snapshot.dropped_rows,
                    "warnings": list(snapshot.warnings),
                },
                "commit": _commit_payload(commit),
            }
        finally:
            temporary.cleanup()


def _clone_commit(
    *,
    commit: CommitRecord,
    pool: ConversationPool,
    state: LocalMappingStore,
    target: ShareTarget,
    cwd: Path,
    title: str,
    context: ShareContext,
) -> dict[str, Any]:
    capability = detect_fork_path_capability(
        codex_command=context.codex_command,
        codex_home=target.home,
    )
    if not capability.available:
        raise ConversationPoolError(
            f"clone is disabled: {capability.detail}"
        )
    if not cwd.is_absolute():
        cwd = cwd.resolve()
    if not cwd.is_dir():
        raise ConversationPoolError(
            f"target working directory does not exist: {cwd}; use --cwd"
        )
    object_path = pool.verify_object(commit.snapshot_hash)
    new_thread_id: str | None = None
    with tempfile.TemporaryDirectory(
        prefix="apicodex-target-snapshot-",
        ignore_cleanup_errors=True,
    ) as directory:
        target_snapshot = materialize_snapshot_for_target(
            object_path,
            Path(directory) / "target.jsonl",
            model_provider=target.model_provider,
            model=target.model,
            cwd=cwd,
        )
        with _new_app_server(context, target) as client:
            try:
                thread = client.fork_path(
                    source_thread_id=commit.source_thread_id,
                    rollout_path=target_snapshot,
                    model_provider=target.model_provider,
                    cwd=cwd,
                    model=target.model,
                )
                new_thread_id = str(thread.get("id") or "")
                if not new_thread_id or new_thread_id == commit.source_thread_id:
                    raise ConversationPoolError(
                        "app-server did not create an independent thread id"
                    )
                client.set_thread_name(new_thread_id, title)
                verified = client.read_thread(new_thread_id, include_turns=True)
                if str(verified.get("id") or "") != new_thread_id:
                    raise ConversationPoolError("cloned thread id verification failed")
                if str(verified.get("modelProvider") or "") != target.model_provider:
                    raise ConversationPoolError(
                        "cloned thread model provider verification failed"
                    )
                actual_cwd = Path(str(verified.get("cwd") or "")).resolve()
                if os.path.normcase(str(actual_cwd)) != os.path.normcase(
                    str(cwd.resolve())
                ):
                    raise ConversationPoolError("cloned thread cwd verification failed")
                if str(verified.get("name") or "") != title:
                    raise ConversationPoolError("cloned thread title verification failed")
                if new_thread_id not in {
                    str(item.get("id") or "")
                    for item in client.list_threads(limit=100)
                }:
                    raise ConversationPoolError(
                        "cloned thread was not returned by thread/list"
                    )
                rollout_path = _validate_rollout_path(target, verified.get("path"))
                audit_jsonl_no_encrypted_content(rollout_path)
                audit_target_runtime_context(
                    rollout_path,
                    expected_model_provider=target.model_provider,
                    expected_model=target.model,
                )
                mapping = state.register(
                    pool_id=pool.pool_id(),
                    target_id=target.id,
                    target_home=target.home,
                    thread_id=new_thread_id,
                    lineage_id=commit.lineage_id,
                    lineage_name=commit.lineage_name,
                    ref_name=commit.ref_name or "main",
                    base_commit=commit.id,
                    rollout_path=rollout_path,
                )
                return {
                    "threadId": new_thread_id,
                    "title": title,
                    "cwd": str(actual_cwd),
                    "modelProvider": target.model_provider,
                    "model": target.model,
                    "rolloutPath": str(rollout_path),
                    "mapping": _mapping_payload(mapping),
                    "capability": asdict(capability),
                }
            except BaseException as exc:
                if new_thread_id:
                    try:
                        client.delete_thread(new_thread_id)
                    except BaseException as rollback_exc:
                        raise ConversationPoolError(
                            f"clone failed and rollback also failed: {exc}; "
                            f"rollback: {rollback_exc}"
                        ) from exc
                raise


def _handle_clone(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    state, pool = _state_and_pool(args, context)
    commit = pool.resolve(
        args.name,
        ref_name=args.ref,
        commit_id=args.commit,
    )
    target = _select_target(args, context)
    cwd = Path(args.cwd).resolve() if args.cwd else Path(commit.cwd).resolve()
    title = args.title or f"{commit.title} [shared]"
    if args.dry_run:
        capability = detect_fork_path_capability(
            codex_command=context.codex_command,
            codex_home=target.home,
        )
        if not capability.available:
            raise ConversationPoolError(
                f"clone is disabled: {capability.detail}"
            )
        pool.verify_object(commit.snapshot_hash)
        if not cwd.is_dir():
            raise ConversationPoolError(
                f"target working directory does not exist: {cwd}; use --cwd"
            )
        clone = {
            "planned": True,
            "title": title,
            "cwd": str(cwd),
            "modelProvider": target.model_provider,
            "model": target.model,
            "capability": asdict(capability),
        }
    else:
        clone = _clone_commit(
            commit=commit,
            pool=pool,
            state=state,
            target=target,
            cwd=cwd,
            title=title,
            context=context,
        )
    return {
        "ok": True,
        "operation": "clone",
        "dryRun": args.dry_run,
        "target": target.label,
        "commit": _commit_payload(commit),
        "clone": clone,
    }


def _select_mapping(
    args: argparse.Namespace,
    context: ShareContext,
    state: LocalMappingStore,
    pool: ConversationPool,
) -> tuple[ShareTarget, MappingRecord]:
    target = _select_target(args, context)
    mappings = state.find(
        pool_id=pool.pool_id(),
        target_home=target.home,
        thread_id=args.thread,
    )
    if not mappings:
        raise ConversationPoolError(
            "no local shared-conversation mapping matches this Profile/thread"
        )
    if len(mappings) == 1:
        return target, mappings[0]
    print(f"Choose a mapped conversation from {target.label}", file=sys.stderr)
    for index, mapping in enumerate(mappings, 1):
        print(
            f"[{index}] {mapping.lineage_name}/{mapping.ref_name} "
            f"{mapping.thread_id[:12]} base={mapping.base_commit[:12]}",
            file=sys.stderr,
        )
    choice = input("Choose number [1]: ").strip()
    if not choice:
        return target, mappings[0]
    if choice.isdigit() and 1 <= int(choice) <= len(mappings):
        return target, mappings[int(choice) - 1]
    raise ConversationPoolError(f"mapping selector {choice!r} is invalid")


def _snapshot_mapping(
    client: CodexAppServer,
    target: ShareTarget,
    mapping: MappingRecord,
) -> tuple[dict[str, Any], Any, tempfile.TemporaryDirectory[str]]:
    return _sanitize_thread(client, target, mapping.thread_id)


def _mapping_status(
    *,
    mapping: MappingRecord,
    snapshot: Any,
    pool: ConversationPool,
) -> tuple[str, CommitRecord]:
    head = pool.resolve(mapping.lineage_name, ref_name=mapping.ref_name)
    if snapshot.sha256 == head.snapshot_hash or semantic_snapshot_hash(
        pool.verify_object(head.snapshot_hash)
    ) == semantic_snapshot_hash(snapshot.path):
        return "clean", head
    if mapping.base_commit == head.id:
        return "ahead", head
    return "diverged", head


def _handle_push(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    state, pool = _state_and_pool(args, context)
    target, mapping = _select_mapping(args, context, state, pool)
    with _new_app_server(context, target) as client:
        thread, snapshot, temporary = _snapshot_mapping(client, target, mapping)
        try:
            commit = pool.push(
                lineage_id=mapping.lineage_id,
                ref_name=mapping.ref_name,
                base_commit=mapping.base_commit,
                snapshot=snapshot,
                source_target=target.id,
                title=_thread_title(thread),
                message=args.message,
                new_branch=args.new_branch,
                dry_run=args.dry_run,
            )
            if not args.dry_run:
                state.register(
                    pool_id=pool.pool_id(),
                    target_id=target.id,
                    target_home=target.home,
                    thread_id=mapping.thread_id,
                    lineage_id=mapping.lineage_id,
                    lineage_name=mapping.lineage_name,
                    ref_name=commit.ref_name or mapping.ref_name,
                    base_commit=commit.id,
                    rollout_path=_validate_rollout_path(target, thread.get("path")),
                )
            return {
                "ok": True,
                "operation": "push",
                "dryRun": args.dry_run,
                "target": target.label,
                "threadId": mapping.thread_id,
                "commit": _commit_payload(commit),
            }
        finally:
            temporary.cleanup()


def _handle_status(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    state, pool = _state_and_pool(args, context)
    target = _select_target(args, context)
    mappings = state.find(
        pool_id=pool.pool_id(),
        target_home=target.home,
        thread_id=args.thread,
    )
    if not mappings:
        raise ConversationPoolError("no mapped shared conversations were found")
    statuses: list[dict[str, Any]] = []
    with _new_app_server(context, target) as client:
        for mapping in mappings:
            thread, snapshot, temporary = _snapshot_mapping(
                client,
                target,
                mapping,
            )
            try:
                status, head = _mapping_status(
                    mapping=mapping,
                    snapshot=snapshot,
                    pool=pool,
                )
                statuses.append(
                    {
                        "threadId": mapping.thread_id,
                        "conversation": mapping.lineage_name,
                        "ref": mapping.ref_name,
                        "status": status,
                        "baseCommit": mapping.base_commit,
                        "headCommit": head.id,
                        "snapshotHash": snapshot.sha256,
                        "title": _thread_title(thread),
                    }
                )
            finally:
                temporary.cleanup()
    return {
        "ok": True,
        "operation": "status",
        "target": target.label,
        "items": statuses,
    }


def _handle_list(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    _, pool = _state_and_pool(args, context)
    return {
        "ok": True,
        "operation": "list",
        "pool": str(pool.root),
        "conversations": pool.list_lineages(),
    }


def _handle_log(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    _, pool = _state_and_pool(args, context)
    return {
        "ok": True,
        "operation": "log",
        "conversation": args.name,
        "ref": args.ref,
        "commits": [_commit_payload(item) for item in pool.log(args.name, ref_name=args.ref)],
    }


def _handle_doctor(
    args: argparse.Namespace,
    context: ShareContext,
) -> dict[str, Any]:
    _, pool = _state_and_pool(args, context)
    target: ShareTarget | None = None
    if args.account or args.api_profile:
        target = _select_target(args, context)
    capability = detect_fork_path_capability(
        codex_command=context.codex_command,
        codex_home=target.home if target else None,
    )
    report = pool.doctor()
    report["cloneCapability"] = asdict(capability)
    report["cloneEnabled"] = capability.available
    if target:
        report["target"] = {
            "id": target.id,
            "label": target.label,
            "home": str(target.home),
            "modelProvider": target.model_provider,
        }
    return {"ok": capability.available, "operation": "doctor", "report": report}


def _add_common(
    parser: argparse.ArgumentParser,
    *,
    selectors: bool = True,
    dry_run: bool = True,
) -> None:
    parser.add_argument(
        "--pool",
        help="override the configured pool path",
    )
    if selectors:
        parser.add_argument("--account", action="store_true")
        parser.add_argument("--api-profile", metavar="NAME")
        parser.add_argument("--thread", metavar="ID")
    if dry_run:
        parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apicodex share",
        description="Portable local Codex conversation sharing",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="initialize the EFS-protected pool")
    _add_common(init, selectors=False)

    publish = commands.add_parser("publish", help="publish a new shared conversation")
    publish.add_argument("name")
    publish.add_argument(
        "-m",
        "--message",
        default="Initial shared conversation",
    )
    _add_common(publish)

    clone = commands.add_parser("clone", help="clone a pool snapshot as a new thread")
    clone.add_argument("name")
    revision = clone.add_mutually_exclusive_group()
    revision.add_argument("--ref", default="main")
    revision.add_argument("--commit")
    clone.add_argument("--cwd")
    clone.add_argument("--title")
    _add_common(clone)

    push = commands.add_parser("push", help="publish a mapped thread update")
    push.add_argument(
        "-m",
        "--message",
        default="Update shared conversation",
    )
    push.add_argument("--new-branch", metavar="NAME")
    _add_common(push)

    status = commands.add_parser("status", help="compare local threads with pool heads")
    _add_common(status)

    list_parser = commands.add_parser("list", help="list shared conversations")
    _add_common(list_parser, selectors=False, dry_run=False)

    log = commands.add_parser("log", help="show a shared conversation history")
    log.add_argument("name")
    log.add_argument("--ref", default="main")
    _add_common(log, selectors=False, dry_run=False)

    doctor = commands.add_parser("doctor", help="verify pool and clone compatibility")
    _add_common(doctor, dry_run=False)
    return parser


HANDLERS: dict[str, Callable[[argparse.Namespace, ShareContext], dict[str, Any]]] = {
    "init": _handle_init,
    "publish": _handle_publish,
    "clone": _handle_clone,
    "push": _handle_push,
    "status": _handle_status,
    "list": _handle_list,
    "log": _handle_log,
    "doctor": _handle_doctor,
}


def main(arguments: Sequence[str], context: ShareContext) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(arguments))
        result = HANDLERS[args.command](args, context)
        _emit(result, as_json=args.json)
        if args.command == "doctor" and not result.get("ok"):
            return 1
        return 0
    except (ConversationPoolError, AppServerError, OSError, ValueError) as exc:
        wants_json = "--json" in arguments
        if wants_json:
            _emit({"ok": False, "error": str(exc)}, as_json=True)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1
