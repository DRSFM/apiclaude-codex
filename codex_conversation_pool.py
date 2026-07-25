#!/usr/bin/env python3
"""Portable Codex conversation snapshots and Git-like local pool storage."""

from __future__ import annotations

import ctypes
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Protocol


SCHEMA_VERSION = 1
DEFAULT_POOL_PATH = Path(r"E:\CodexConversationPool")
OBJECT_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
FILE_ATTRIBUTE_ENCRYPTED = 0x00004000
FILE_SUPPORTS_ENCRYPTION = 0x00020000
PORTABLE_THREAD_ID = "01900000-0000-7000-8000-000000000000"
PORTABLE_TIMESTAMP = "1970-01-01T00:00:00Z"
PORTABLE_MODEL_ID = "apicodex-portable"

KNOWN_RESPONSE_TYPES = frozenset(
    {
        "message",
        "reasoning",
        "agent_reasoning",
        "compaction",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "computer_call",
        "computer_call_output",
        "web_search_call",
        "file_search_call",
        "local_shell_call",
        "shell_call",
        "shell_call_output",
        "mcp_call",
        "mcp_list_tools",
        "mcp_approval_request",
        "mcp_approval_response",
        "image_generation_call",
        "code_interpreter_call",
        "apply_patch_call",
        "apply_patch_call_output",
    }
)
DROP_RESPONSE_TYPES = frozenset({"reasoning", "agent_reasoning", "compaction"})
TECHNICAL_MESSAGE_PREFIXES = (
    "<app-context>",
    "<permissions instructions>",
    "<collaboration_mode>",
    "<multi_agent_mode>",
    "<apps_instructions>",
    "<plugins_instructions>",
    "<skills_instructions>",
    "<environment_context>",
    "<recommended_plugins>",
    "# agents.md instructions",
)
DROP_EVENT_TYPES = frozenset(
    {
        "agent_reasoning",
        "agent_reasoning_delta",
        "agent_reasoning_raw_content",
        "agent_reasoning_raw_content_delta",
        "token_count",
        "thread_settings_applied",
        "raw_response_item",
    }
)
SECRET_KEY_RE = re.compile(
    r"(?i)^(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|"
    r"auth[_-]?token|client[_-]?token|session[_-]?token|id[_-]?token|"
    r"authorization|password|passwd|secret|client[_-]?secret|private[_-]?key|"
    r"credential|cookie|set-cookie)$"
)
DROP_KEY_RE = re.compile(
    r"(?i)^(?:encrypted_content|internal_chat_message_metadata_passthrough|"
    r"token_count|token_usage|usage|rate_limits?|context_window|"
    r"base_instructions|developer_instructions|dynamic_tools|"
    r"approval_policy|approvals_reviewer|sandbox_policy|permission_profile|"
    r"workspace_roots|runtime_workspace_roots|skills?|plugins?|agents_md)$"
)
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"',}]+"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|"
        r"client[_-]?secret|session[_-]?token|id[_-]?token|token|cookie)"
        r"\s*[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


class ConversationPoolError(RuntimeError):
    """Base error for a safe, user-facing pool failure."""


class SnapshotCompatibilityError(ConversationPoolError):
    """The source contains history this build cannot preserve safely."""


class SnapshotChangedError(ConversationPoolError):
    """The source changed during snapshotting or is not a stable file."""


class PoolSecurityError(ConversationPoolError):
    """The pool cannot meet the required Windows ACL/EFS policy."""


class PoolConflictError(ConversationPoolError):
    """A lineage or reference would be updated non-fast-forward."""


class PoolIntegrityError(ConversationPoolError):
    """An immutable object or metadata record failed verification."""


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class SnapshotResult:
    path: Path
    sha256: str
    size: int
    source_thread_id: str
    source_cwd: str
    source_model_provider: str
    source_timestamp: str
    kept_rows: int
    dropped_rows: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class CommitRecord:
    id: str
    lineage_id: str
    lineage_name: str
    parent_id: str | None
    snapshot_hash: str
    created_at: str
    message: str
    source_thread_id: str
    source_target: str
    title: str
    cwd: str
    ref_name: str | None = None


@dataclass(frozen=True)
class MappingRecord:
    pool_id: str
    target_id: str
    target_home_hash: str
    thread_id: str
    lineage_id: str
    lineage_name: str
    ref_name: str
    base_commit: str
    rollout_path: str
    updated_at: str


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


def _file_identity(path: Path) -> FileIdentity:
    stat = path.stat()
    return FileIdentity(
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size=int(stat.st_size),
        modified_ns=int(stat.st_mtime_ns),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + "[REDACTED]" if match.lastindex else "[REDACTED]", redacted)
    return redacted


def _scrub_value(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            if DROP_KEY_RE.match(key):
                continue
            if SECRET_KEY_RE.match(key):
                cleaned[key] = "[REDACTED]"
                continue
            cleaned[key] = _scrub_value(item)
        return cleaned
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == "encrypted_content":
                return True
            if _contains_forbidden_key(item):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def audit_jsonl_no_encrypted_content(path: Path) -> None:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PoolIntegrityError(
                    f"invalid JSON in {path} at line {line_number}: {exc}"
                ) from exc
            if _contains_forbidden_key(row):
                raise PoolIntegrityError(
                    f"encrypted_content remains in {path} at line {line_number}"
                )


def materialize_snapshot_for_target(
    source_path: Path,
    destination_path: Path,
    *,
    model_provider: str,
    model: str | None,
    cwd: Path,
) -> Path:
    """Build a temporary fork input containing only target runtime settings."""

    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    if not source_path.is_file() or source_path.is_symlink():
        raise PoolIntegrityError(
            f"portable snapshot is not a regular file: {source_path}"
        )
    if destination_path.exists():
        raise ConversationPoolError(
            f"target snapshot destination already exists: {destination_path}"
        )
    provider = str(model_provider).strip()
    if not provider:
        raise ConversationPoolError("target model provider is empty")
    target_model = str(model).strip() if model else ""
    target_cwd = str(cwd.resolve())
    before = _file_identity(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source_path.open("r", encoding="utf-8") as source, destination_path.open(
            "x",
            encoding="utf-8",
            newline="\n",
        ) as destination:
            for line_number, line in enumerate(source, 1):
                if not line.endswith("\n"):
                    raise PoolIntegrityError(
                        f"portable snapshot ends with a partial line at {line_number}"
                    )
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PoolIntegrityError(
                        f"invalid portable JSON at line {line_number}: {exc}"
                    ) from exc
                if not isinstance(row, dict):
                    raise PoolIntegrityError(
                        f"portable row {line_number} is not an object"
                    )
                row_type = row.get("type")
                if row_type in {"session_meta", "turn_context"}:
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        raise PoolIntegrityError(
                            f"portable {row_type} at line {line_number} is invalid"
                        )
                    payload = dict(payload)
                    payload["cwd"] = target_cwd
                    if row_type == "session_meta":
                        payload["model_provider"] = provider
                    elif target_model:
                        payload["model"] = target_model
                    else:
                        payload.pop("model", None)
                    row = dict(row)
                    row["payload"] = payload
                if _contains_forbidden_key(row):
                    raise PoolIntegrityError(
                        f"forbidden content in portable row {line_number}"
                    )
                destination.write(
                    json.dumps(
                        row,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            destination.flush()
            os.fsync(destination.fileno())
        if before != _file_identity(source_path):
            raise PoolIntegrityError(
                "portable snapshot changed while target settings were materialized"
            )
        audit_jsonl_no_encrypted_content(destination_path)
        return destination_path
    except BaseException:
        destination_path.unlink(missing_ok=True)
        raise


def audit_target_runtime_context(
    path: Path,
    *,
    expected_model_provider: str,
    expected_model: str | None,
) -> None:
    """Reject a clone that retained portable or wrong target runtime settings."""

    provider = str(expected_model_provider).strip()
    target_model = str(expected_model).strip() if expected_model else ""
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PoolIntegrityError(
                    f"invalid target rollout JSON at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise PoolIntegrityError(
                    f"target rollout row {line_number} is not an object"
                )
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if row.get("type") == "session_meta":
                actual_provider = payload.get("model_provider")
                if actual_provider != provider:
                    raise PoolIntegrityError(
                        "target rollout retained the wrong model provider at "
                        f"line {line_number}: {actual_provider!r}"
                    )
            elif row.get("type") == "turn_context":
                actual_model = payload.get("model")
                if actual_model == PORTABLE_MODEL_ID:
                    raise PoolIntegrityError(
                        f"portable model leaked into target rollout at line {line_number}"
                    )
                if target_model and actual_model != target_model:
                    raise PoolIntegrityError(
                        "target rollout retained the wrong model at "
                        f"line {line_number}: {actual_model!r}"
                    )


def semantic_snapshot_hash(path: Path) -> str:
    """Hash conversation content while ignoring fork-generated bookkeeping."""

    digest = hashlib.sha256()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PoolIntegrityError(
                    f"invalid JSON in {path} at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise PoolIntegrityError(
                    f"invalid row in {path} at line {line_number}"
                )
            normalized = dict(row)
            normalized.pop("timestamp", None)
            payload = normalized.get("payload")
            if isinstance(payload, dict):
                payload = dict(payload)
                if normalized.get("type") == "session_meta":
                    payload.pop("git", None)
                if normalized.get("type") == "turn_context":
                    payload.pop("cwd", None)
                    payload.pop("model", None)
                if normalized.get("type") == "event_msg":
                    for key in (
                        "started_at",
                        "completed_at",
                        "duration_ms",
                        "time_to_first_token_ms",
                    ):
                        payload.pop(key, None)
                    if payload.get("type") == "user_message":
                        for key in ("audio", "local_audio"):
                            if payload.get(key) == []:
                                payload.pop(key, None)
                    if payload.get("type") == "web_search_end":
                        payload.pop("results", None)
                normalized["payload"] = payload
            encoded = (
                json.dumps(
                    normalized,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            digest.update(encoded)
    return digest.hexdigest()


def _message_text_parts(payload: dict[str, Any]) -> list[str]:
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        for key in ("text", "input_text", "output_text"):
            value = item.get(key)
            if isinstance(value, str):
                parts.append(value)
                break
    return parts


def _is_technical_message(payload: dict[str, Any]) -> bool:
    role = str(payload.get("role") or "").lower()
    if role not in {"user", "assistant"}:
        return True
    if role == "assistant":
        return False
    parts = _message_text_parts(payload)
    if not parts:
        return False
    return all(
        _is_technical_user_text(part)
        for part in parts
    )


def _is_technical_user_text(text: str) -> bool:
    return (
        text.lstrip("\ufeff\u200b\u200c\u200d\u2060\ufffd \t\r\n")
        .lower()
        .startswith(TECHNICAL_MESSAGE_PREFIXES)
    )


def _sanitize_response_payload(
    payload: Any,
    *,
    location: str,
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        raise SnapshotCompatibilityError(
            f"{location}: response_item payload is not an object"
        )
    item_type = payload.get("type")
    if not isinstance(item_type, str) or item_type not in KNOWN_RESPONSE_TYPES:
        raise SnapshotCompatibilityError(
            f"{location}: unknown response_item type {item_type!r}"
        )
    if item_type in DROP_RESPONSE_TYPES:
        return None
    if item_type == "message" and _is_technical_message(payload):
        return None
    turn_metadata = payload.get("internal_chat_message_metadata_passthrough")
    structural_turn_id = (
        turn_metadata.get("turn_id")
        if isinstance(turn_metadata, dict)
        else None
    )
    cleaned = _scrub_value(payload)
    if not isinstance(cleaned, dict):
        raise AssertionError("scrubbing a response payload must return a dictionary")
    cleaned.pop("id", None)
    if isinstance(structural_turn_id, str) and structural_turn_id:
        cleaned["internal_chat_message_metadata_passthrough"] = {
            "turn_id": structural_turn_id
        }
    if _contains_forbidden_key(cleaned):
        raise SnapshotCompatibilityError(
            f"{location}: encrypted_content survived response cleaning"
        )
    return cleaned


def _sanitized_git(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, str] = {}
    for key in ("commit_hash", "branch"):
        item = value.get(key)
        if isinstance(item, str) and item:
            result[key] = _redact_text(item)
    repository_url = value.get("repository_url")
    if isinstance(repository_url, str) and repository_url:
        try:
            parsed = urllib.parse.urlsplit(repository_url)
            hostname = parsed.hostname or ""
            if parsed.port:
                hostname = f"{hostname}:{parsed.port}"
            result["repository_url"] = urllib.parse.urlunsplit(
                (parsed.scheme, hostname, parsed.path, "", "")
            )
        except ValueError:
            pass
    return result or None


def _sanitize_session_meta(
    payload: Any,
    *,
    source_path: Path,
) -> tuple[dict[str, Any], str, str, str, str]:
    if not isinstance(payload, dict):
        raise SnapshotCompatibilityError("session_meta payload is not an object")
    thread_id = payload.get("id") or payload.get("session_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise SnapshotCompatibilityError("session_meta has no thread id")
    timestamp = payload.get("timestamp")
    cwd = payload.get("cwd")
    provider = payload.get("model_provider")
    if not isinstance(timestamp, str):
        timestamp = ""
    if not isinstance(cwd, str) or not cwd:
        cwd = str(source_path.parent)
    if not isinstance(provider, str) or not provider:
        provider = "openai"

    portable_cwd = "C:\\" if re.match(r"^[A-Za-z]:[\\/]", cwd) else "/"
    cleaned: dict[str, Any] = {
        "session_id": PORTABLE_THREAD_ID,
        "id": PORTABLE_THREAD_ID,
        "timestamp": PORTABLE_TIMESTAMP,
        "cwd": portable_cwd,
        "originator": "apicodex_conversation_pool",
        "cli_version": "0.0.0",
        "source": "vscode",
        "thread_source": "user",
        "model_provider": "openai",
    }
    git = _sanitized_git(payload.get("git"))
    if git:
        cleaned["git"] = git
    if _contains_forbidden_key(cleaned):
        raise SnapshotCompatibilityError(
            "encrypted_content survived session metadata cleaning"
        )
    return cleaned, thread_id, cwd, provider, timestamp


def _sanitize_turn_context(payload: Any, source_cwd: str) -> dict[str, Any] | None:
    """Keep only the structural turn marker needed for replay projection."""

    if not isinstance(payload, dict):
        raise SnapshotCompatibilityError("turn_context payload is not an object")
    turn_id = payload.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id:
        return None
    portable_cwd = (
        "C:\\" if re.match(r"^[A-Za-z]:[\\/]", source_cwd) else "/"
    )
    return {
        "turn_id": turn_id,
        "cwd": portable_cwd,
        "approval_policy": "on-request",
        "sandbox_policy": {"type": "read-only"},
        "summary": "auto",
    }


def _sanitize_event_payload(
    payload: Any,
    *,
    location: str,
) -> dict[str, Any] | None:
    """Keep replay-bearing UI events while removing private runtime state.

    Legacy app-server history projection is event-driven: user/assistant
    messages and tool lifecycle events are reconstructed from ``event_msg``
    rows, while the matching ``response_item`` rows carry model context.
    """

    if not isinstance(payload, dict):
        raise SnapshotCompatibilityError(f"{location}: event payload is not an object")
    event_type = payload.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise SnapshotCompatibilityError(f"{location}: event payload has no type")
    if event_type in DROP_EVENT_TYPES or "reasoning" in event_type.lower():
        return None

    if event_type in {"item_started", "item_completed"}:
        item = payload.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if isinstance(item_type, str) and "reasoning" in item_type.lower():
            return None

    if event_type == "user_message":
        message = payload.get("message")
        if isinstance(message, str) and _is_technical_user_text(message):
            return None

    cleaned = _scrub_value(payload)
    if not isinstance(cleaned, dict):
        raise SnapshotCompatibilityError(f"{location}: event cleaning failed")
    cleaned["type"] = event_type
    if _contains_forbidden_key(cleaned):
        raise SnapshotCompatibilityError(
            f"{location}: encrypted_content survived event cleaning"
        )
    return cleaned


CALL_TYPES = {
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
    "computer_call": "computer_call_output",
    "shell_call": "shell_call_output",
    "apply_patch_call": "apply_patch_call_output",
}
OUTPUT_TYPES = {output: call for call, output in CALL_TYPES.items()}


def _validate_tool_pairs(
    payloads: Iterable[dict[str, Any]],
    *,
    location: str,
) -> None:
    calls: dict[str, str] = {}
    for index, payload in enumerate(payloads, 1):
        _track_tool_pair(
            payload,
            calls,
            location=f"{location}[{index}]",
        )


def _track_tool_pair(
    payload: dict[str, Any],
    calls: dict[str, str],
    *,
    location: str,
) -> None:
    item_type = payload.get("type")
    call_id = payload.get("call_id")
    if item_type in CALL_TYPES:
        if not isinstance(call_id, str) or not call_id:
            raise SnapshotCompatibilityError(
                f"{location}: {item_type} has no call_id"
            )
        previous = calls.get(call_id)
        if previous is not None and previous != item_type:
            raise SnapshotCompatibilityError(
                f"{location}: call_id {call_id!r} changes type"
            )
        calls[call_id] = str(item_type)
    elif item_type in OUTPUT_TYPES:
        expected_call = OUTPUT_TYPES[str(item_type)]
        if not isinstance(call_id, str) or calls.get(call_id) != expected_call:
            raise SnapshotCompatibilityError(
                f"{location}: orphan {item_type} for call_id {call_id!r}"
            )


def sanitize_rollout(
    source_path: Path,
    destination_path: Path,
    *,
    expected_thread_id: str | None = None,
) -> SnapshotResult:
    """Stream one stable rollout into a portable, secret-scrubbed JSONL file."""

    source_path = source_path.resolve()
    destination_path = destination_path.resolve()
    if not source_path.is_file() or source_path.is_symlink():
        raise SnapshotChangedError(f"source rollout is not a regular file: {source_path}")
    if destination_path.exists():
        raise ConversationPoolError(
            f"snapshot destination already exists: {destination_path}"
        )
    before = _file_identity(source_path)
    if before.size <= 0:
        raise SnapshotCompatibilityError("source rollout is empty")

    metadata_seen = False
    thread_id = ""
    cwd = ""
    provider = ""
    source_timestamp = ""
    kept_rows = 0
    dropped_rows = 0
    warnings: list[str] = []
    top_level_tool_calls: dict[str, str] = {}
    last_turn_id = ""
    terminal_turns: set[str] = set()
    digest = hashlib.sha256()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source_path.open("r", encoding="utf-8") as source, destination_path.open(
            "x",
            encoding="utf-8",
            newline="\n",
        ) as destination:
            for line_number, line in enumerate(source, 1):
                if not line.endswith("\n"):
                    raise SnapshotChangedError(
                        f"source rollout ends with a partial line at {line_number}"
                    )
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SnapshotCompatibilityError(
                        f"invalid JSON at source line {line_number}: {exc}"
                    ) from exc
                if not isinstance(row, dict):
                    raise SnapshotCompatibilityError(
                        f"source line {line_number} is not an object"
                    )
                row_type = row.get("type")
                timestamp = row.get("timestamp")
                output_row: dict[str, Any] | None = None
                raw_payload = row.get("payload")
                if row_type == "turn_context" and isinstance(raw_payload, dict):
                    raw_turn_id = raw_payload.get("turn_id")
                    if isinstance(raw_turn_id, str) and raw_turn_id:
                        last_turn_id = raw_turn_id
                elif row_type == "event_msg" and isinstance(raw_payload, dict):
                    event_type = raw_payload.get("type")
                    raw_turn_id = raw_payload.get("turn_id")
                    if (
                        event_type
                        in {"task_complete", "turn_complete", "turn_aborted"}
                        and isinstance(raw_turn_id, str)
                        and raw_turn_id
                    ):
                        terminal_turns.add(raw_turn_id)
                if row_type == "session_meta":
                    (
                        clean_meta,
                        candidate_thread_id,
                        candidate_cwd,
                        candidate_provider,
                        candidate_timestamp,
                    ) = _sanitize_session_meta(
                        row.get("payload"),
                        source_path=source_path,
                    )
                    if metadata_seen:
                        if (
                            candidate_thread_id == PORTABLE_THREAD_ID
                            and thread_id != PORTABLE_THREAD_ID
                        ):
                            warnings.append(
                                "deduplicated portable fork session_meta at "
                                f"line {line_number}"
                            )
                            dropped_rows += 1
                            continue
                        if (
                            thread_id == PORTABLE_THREAD_ID
                            and candidate_thread_id != PORTABLE_THREAD_ID
                        ):
                            thread_id = candidate_thread_id
                            cwd = candidate_cwd
                            provider = candidate_provider
                            source_timestamp = candidate_timestamp
                            warnings.append(
                                "replaced portable session_meta with fork target "
                                f"metadata at line {line_number}"
                            )
                            dropped_rows += 1
                            continue
                        if (
                            candidate_thread_id != thread_id
                            or candidate_cwd != cwd
                            or candidate_provider != provider
                            or candidate_timestamp != source_timestamp
                        ):
                            raise SnapshotCompatibilityError(
                                "conflicting duplicate session_meta at source "
                                f"line {line_number}"
                            )
                        warnings.append(
                            f"deduplicated repeated session_meta at line {line_number}"
                        )
                        dropped_rows += 1
                        continue
                    thread_id = candidate_thread_id
                    cwd = candidate_cwd
                    provider = candidate_provider
                    source_timestamp = candidate_timestamp
                    metadata_seen = True
                    output_row = {
                        "timestamp": PORTABLE_TIMESTAMP,
                        "type": "session_meta",
                        "payload": clean_meta,
                    }
                elif row_type == "response_item":
                    clean_payload = _sanitize_response_payload(
                        row.get("payload"),
                        location=f"source line {line_number}",
                    )
                    if clean_payload is not None:
                        _track_tool_pair(
                            clean_payload,
                            top_level_tool_calls,
                            location=f"source line {line_number}",
                        )
                        output_row = {
                            "timestamp": timestamp or now_iso(),
                            "type": "response_item",
                            "payload": clean_payload,
                        }
                elif row_type == "compacted":
                    payload = row.get("payload")
                    if not isinstance(payload, dict):
                        raise SnapshotCompatibilityError(
                            f"source line {line_number}: compacted payload is invalid"
                        )
                    replacement = payload.get("replacement_history")
                    if not isinstance(replacement, list):
                        raise SnapshotCompatibilityError(
                            f"source line {line_number}: compacted history is invalid"
                        )
                    cleaned_history: list[dict[str, Any]] = []
                    for history_index, history_item in enumerate(replacement, 1):
                        clean_item = _sanitize_response_payload(
                            history_item,
                            location=(
                                f"source line {line_number} "
                                f"replacement_history[{history_index}]"
                            ),
                        )
                        if clean_item is not None:
                            cleaned_history.append(clean_item)
                    _validate_tool_pairs(
                        cleaned_history,
                        location=f"source line {line_number} replacement_history",
                    )
                    clean_compacted = {
                        "message": _redact_text(
                            str(payload.get("message") or "")
                        ),
                        "replacement_history": cleaned_history,
                    }
                    output_row = {
                        "timestamp": timestamp or now_iso(),
                        "type": "compacted",
                        "payload": clean_compacted,
                    }
                elif row_type == "turn_context":
                    clean_context = _sanitize_turn_context(row.get("payload"), cwd)
                    if clean_context is not None:
                        output_row = {
                            "timestamp": timestamp or now_iso(),
                            "type": "turn_context",
                            "payload": clean_context,
                        }
                elif row_type == "event_msg":
                    clean_event = _sanitize_event_payload(
                        row.get("payload"),
                        location=f"source line {line_number}",
                    )
                    if clean_event is not None:
                        output_row = {
                            "timestamp": timestamp or now_iso(),
                            "type": "event_msg",
                            "payload": clean_event,
                        }
                elif row_type == "world_state":
                    dropped_rows += 1
                    continue
                else:
                    warnings.append(
                        f"dropped unknown non-response row type {row_type!r} "
                        f"at line {line_number}"
                    )
                    dropped_rows += 1
                    continue

                if output_row is None:
                    dropped_rows += 1
                    continue
                if _contains_forbidden_key(output_row):
                    raise SnapshotCompatibilityError(
                        f"encrypted_content survived cleaning at line {line_number}"
                    )
                encoded = (
                    json.dumps(
                        output_row,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                destination.write(encoded.decode("utf-8"))
                digest.update(encoded)
                kept_rows += 1
            destination.flush()
            os.fsync(destination.fileno())

        after = _file_identity(source_path)
        if before != after:
            raise SnapshotChangedError(
                "source rollout changed while the portable snapshot was built"
            )
        if not metadata_seen:
            raise SnapshotCompatibilityError("source rollout has no session_meta")
        if expected_thread_id and thread_id != expected_thread_id:
            raise SnapshotCompatibilityError(
                f"rollout thread id {thread_id!r} does not match "
                f"requested thread {expected_thread_id!r}"
            )
        if last_turn_id and last_turn_id not in terminal_turns:
            raise SnapshotChangedError(
                f"the last turn {last_turn_id} has no terminal event"
            )
        audit_jsonl_no_encrypted_content(destination_path)
        size = destination_path.stat().st_size
        return SnapshotResult(
            path=destination_path,
            sha256=digest.hexdigest(),
            size=size,
            source_thread_id=thread_id,
            source_cwd=cwd,
            source_model_provider=provider,
            source_timestamp=source_timestamp,
            kept_rows=kept_rows,
            dropped_rows=dropped_rows,
            warnings=tuple(warnings),
        )
    except BaseException:
        try:
            destination_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class PoolSecurity(Protocol):
    def prepare(self, root: Path) -> None: ...

    def verify(self, root: Path) -> dict[str, Any]: ...

    def verify_encrypted_file(self, path: Path) -> None: ...


class WindowsEfsPoolSecurity:
    """Dedicated Windows ACL plus EFS; there is deliberately no plaintext mode."""

    def __init__(self, *, powershell: str = "pwsh") -> None:
        self.powershell = powershell

    @staticmethod
    def _volume_supports_efs(root: Path) -> bool:
        if os.name != "nt":
            return False
        volume_path = Path(root.anchor or str(root)).resolve()
        flags = ctypes.c_uint32()
        maximum_component = ctypes.c_uint32()
        serial = ctypes.c_uint32()
        success = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(str(volume_path)),
            None,
            0,
            ctypes.byref(serial),
            ctypes.byref(maximum_component),
            ctypes.byref(flags),
            None,
            0,
        )
        return bool(success and flags.value & FILE_SUPPORTS_ENCRYPTION)

    @staticmethod
    def _is_encrypted(path: Path) -> bool:
        if os.name != "nt":
            return False
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attributes != 0xFFFFFFFF and bool(
            attributes & FILE_ATTRIBUTE_ENCRYPTED
        )

    def _acl_report(self, root: Path) -> dict[str, Any]:
        script = r"""
$acl = Get-Acl -LiteralPath $env:APICODEX_POOL_SECURITY_PATH
$rules = @($acl.Access | ForEach-Object {
    $sid = $_.IdentityReference.Translate(
        [System.Security.Principal.SecurityIdentifier]
    ).Value
    [pscustomobject]@{
        Sid = $sid
        Type = $_.AccessControlType.ToString()
        Rights = $_.FileSystemRights.ToString()
        Inherited = $_.IsInherited
    }
})
[pscustomobject]@{
    Protected = $acl.AreAccessRulesProtected
    CurrentUserSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Rules = $rules
} | ConvertTo-Json -Depth 6 -Compress
"""
        environment = os.environ.copy()
        environment["APICODEX_POOL_SECURITY_PATH"] = str(root)
        result = subprocess.run(
            [
                self.powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise PoolSecurityError(
                f"failed to inspect pool ACL: {(result.stderr or result.stdout).strip()}"
            )
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PoolSecurityError("PowerShell returned an invalid ACL report") from exc
        if not isinstance(report, dict):
            raise PoolSecurityError("PowerShell returned an invalid ACL report")
        return report

    def _apply_dedicated_acl(self, root: Path) -> None:
        def run_icacls(*arguments: str) -> None:
            result = subprocess.run(
                ["icacls", str(root), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise PoolSecurityError(
                    "failed to apply the dedicated pool ACL: "
                    f"{(result.stderr or result.stdout).strip()}"
                )

        run_icacls("/inheritance:r")
        report = self._acl_report(root)
        current_sid = str(report.get("CurrentUserSid") or "")
        if not current_sid:
            raise PoolSecurityError("could not determine the current Windows SID")
        authorized = {current_sid, "S-1-5-18", "S-1-5-32-544"}
        rules = report.get("Rules")
        if not isinstance(rules, list):
            raise PoolSecurityError("pool ACL rules could not be inspected")
        unexpected_sids = {
            str(rule.get("Sid"))
            for rule in rules
            if isinstance(rule, dict)
            and isinstance(rule.get("Sid"), str)
            and rule.get("Sid") not in authorized
        }
        for sid in sorted(unexpected_sids):
            run_icacls("/remove", f"*{sid}")
        for sid in sorted(authorized):
            run_icacls("/remove:d", f"*{sid}")
        run_icacls(
            "/grant:r",
            f"*{current_sid}:(OI)(CI)F",
            "*S-1-5-18:(OI)(CI)F",
            "*S-1-5-32-544:(OI)(CI)F",
        )

    def prepare(self, root: Path) -> None:
        if os.name != "nt":
            raise PoolSecurityError("EFS conversation pools require Windows")
        root = root.resolve()
        if not self._volume_supports_efs(root):
            raise PoolSecurityError(
                f"the volume containing {root} does not support EFS"
            )
        root.mkdir(parents=True, exist_ok=True)
        self._apply_dedicated_acl(root)
        encrypted = subprocess.run(
            ["cipher", "/E", "/A", "/H", str(root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
        if encrypted.returncode != 0:
            raise PoolSecurityError(
                f"failed to enable EFS: "
                f"{(encrypted.stderr or encrypted.stdout).strip()}"
            )
        # EFS can add an OWNER RIGHTS ACE while enabling encryption. Reapply
        # the exact allow-list so the final directory policy remains dedicated.
        self._apply_dedicated_acl(root)
        probe = root / f".efs-probe-{uuid.uuid4().hex}"
        try:
            probe.write_bytes(b"apicodex-efs-probe")
            self.verify_encrypted_file(probe)
            self.verify(root)
        finally:
            probe.unlink(missing_ok=True)

    def verify(self, root: Path) -> dict[str, Any]:
        root = root.resolve()
        if not root.is_dir():
            raise PoolSecurityError(f"pool directory is missing: {root}")
        if not self._volume_supports_efs(root):
            raise PoolSecurityError("pool volume no longer reports EFS support")
        if not self._is_encrypted(root):
            raise PoolSecurityError("pool directory is not marked for EFS encryption")
        report = self._acl_report(root)
        if not report.get("Protected"):
            raise PoolSecurityError("pool ACL inheritance is enabled")
        authorized = {
            str(report.get("CurrentUserSid") or ""),
            "S-1-5-18",
            "S-1-5-32-544",
        }
        rules = report.get("Rules")
        if not isinstance(rules, list):
            raise PoolSecurityError("pool ACL rules could not be verified")
        unexpected = [
            rule
            for rule in rules
            if isinstance(rule, dict)
            and rule.get("Type") == "Allow"
            and rule.get("Sid") not in authorized
        ]
        if unexpected:
            raise PoolSecurityError(
                "pool ACL grants access to identities outside the allowed set"
            )
        if not any(
            isinstance(rule, dict)
            and rule.get("Type") == "Allow"
            and rule.get("Sid") == report.get("CurrentUserSid")
            for rule in rules
        ):
            raise PoolSecurityError("the current user has no explicit pool ACL")
        return {
            "efs": True,
            "aclProtected": True,
            "allowedSids": sorted(authorized),
        }

    def verify_encrypted_file(self, path: Path) -> None:
        if not path.is_file() or not self._is_encrypted(path):
            raise PoolSecurityError(f"file is not EFS encrypted: {path}")


class ConversationPool:
    """SQLite metadata plus immutable, content-addressed rollout objects."""

    def __init__(
        self,
        root: Path,
        *,
        security: PoolSecurity | None = None,
    ) -> None:
        self.root = root.resolve()
        self.database_path = self.root / "pool.sqlite3"
        self.metadata_path = self.root / "pool.json"
        self.objects_root = self.root / "objects"
        self.security: PoolSecurity = security or WindowsEfsPoolSecurity()

    @classmethod
    def initialize(
        cls,
        root: Path,
        *,
        security: PoolSecurity | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        pool = cls(root, security=security)
        if pool.metadata_path.exists() or pool.database_path.exists():
            pool.verify_initialized()
            return {
                "created": False,
                "pool": str(pool.root),
                "poolId": pool.pool_id(),
                "dryRun": dry_run,
            }
        if pool.root.exists() and any(pool.root.iterdir()):
            raise ConversationPoolError(
                f"refusing to initialize a non-empty directory: {pool.root}"
            )
        if dry_run:
            if isinstance(pool.security, WindowsEfsPoolSecurity):
                if not pool.security._volume_supports_efs(pool.root):
                    raise PoolSecurityError(
                        f"the volume containing {pool.root} does not support EFS"
                    )
            return {
                "created": True,
                "pool": str(pool.root),
                "poolId": None,
                "dryRun": True,
            }

        pool.security.prepare(pool.root)
        pool.objects_root.mkdir(parents=True, exist_ok=True)
        pool_id = str(uuid.uuid4())
        metadata = {
            "schemaVersion": SCHEMA_VERSION,
            "poolId": pool_id,
            "createdAt": now_iso(),
            "format": "apicodex-portable-conversation-pool",
        }
        temporary_metadata = pool.root / f".pool-{uuid.uuid4().hex}.tmp"
        try:
            with temporary_metadata.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        metadata,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_metadata, pool.metadata_path)
            pool._create_database()
            pool.security.verify_encrypted_file(pool.metadata_path)
            pool.security.verify_encrypted_file(pool.database_path)
            pool.security.verify(pool.root)
            return {
                "created": True,
                "pool": str(pool.root),
                "poolId": pool_id,
                "dryRun": False,
            }
        except BaseException:
            temporary_metadata.unlink(missing_ok=True)
            for created_path in (
                pool.metadata_path,
                pool.database_path,
                Path(str(pool.database_path) + "-journal"),
                Path(str(pool.database_path) + "-wal"),
                Path(str(pool.database_path) + "-shm"),
            ):
                created_path.unlink(missing_ok=True)
            try:
                pool.objects_root.rmdir()
            except OSError:
                pass
            raise

    @contextlib.contextmanager
    def _connect(
        self,
        *,
        readonly: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        if readonly:
            uri = self.database_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
        else:
            connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            if not readonly:
                connection.commit()
        except BaseException:
            if not readonly:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _create_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = DELETE;
                CREATE TABLE schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE lineages (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_cwd TEXT NOT NULL
                );
                CREATE TABLE commits (
                    id TEXT PRIMARY KEY,
                    lineage_id TEXT NOT NULL REFERENCES lineages(id),
                    parent_id TEXT REFERENCES commits(id),
                    snapshot_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source_thread_id TEXT NOT NULL,
                    source_target TEXT NOT NULL,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    UNIQUE(lineage_id, parent_id, snapshot_hash)
                );
                CREATE TABLE refs (
                    lineage_id TEXT NOT NULL REFERENCES lineages(id),
                    name TEXT NOT NULL COLLATE NOCASE,
                    commit_id TEXT NOT NULL REFERENCES commits(id),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(lineage_id, name)
                );
                CREATE INDEX commits_lineage_created
                    ON commits(lineage_id, created_at DESC);
                """
            )
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def verify_initialized(self) -> None:
        if not self.metadata_path.is_file() or not self.database_path.is_file():
            raise ConversationPoolError(
                f"conversation pool is not initialized: {self.root}"
            )
        metadata = self._read_metadata()
        if metadata.get("schemaVersion") != SCHEMA_VERSION:
            raise PoolIntegrityError("unsupported pool metadata schema")
        if not isinstance(metadata.get("poolId"), str):
            raise PoolIntegrityError("pool metadata has no pool id")
        with self._connect(readonly=True) as connection:
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or row["value"] != str(SCHEMA_VERSION):
                raise PoolIntegrityError("unsupported pool database schema")
        self.security.verify_encrypted_file(self.metadata_path)
        self.security.verify_encrypted_file(self.database_path)
        self.security.verify(self.root)

    def _read_metadata(self) -> dict[str, Any]:
        try:
            value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PoolIntegrityError(f"invalid pool metadata: {exc}") from exc
        if not isinstance(value, dict):
            raise PoolIntegrityError("invalid pool metadata")
        return value

    def pool_id(self) -> str:
        pool_id = self._read_metadata().get("poolId")
        if not isinstance(pool_id, str) or not pool_id:
            raise PoolIntegrityError("pool metadata has no pool id")
        return pool_id

    @staticmethod
    def _validate_ref_name(name: str) -> str:
        if (
            not REF_RE.fullmatch(name)
            or ".." in name
            or "//" in name
            or name.endswith((".", "/"))
            or any(part in {"", ".", ".."} for part in name.split("/"))
        ):
            raise ConversationPoolError(f"invalid reference name: {name!r}")
        return name

    @staticmethod
    def _validate_lineage_name(name: str) -> str:
        normalized = name.strip()
        if (
            not normalized
            or len(normalized) > 120
            or any(ord(character) < 32 for character in normalized)
        ):
            raise ConversationPoolError("lineage name must be 1-120 printable characters")
        return normalized

    def object_path(self, snapshot_hash: str) -> Path:
        if not OBJECT_HASH_RE.fullmatch(snapshot_hash):
            raise PoolIntegrityError(f"invalid snapshot hash: {snapshot_hash!r}")
        candidate = self.objects_root / snapshot_hash[:2] / f"{snapshot_hash}.jsonl"
        resolved_parent = candidate.parent.resolve()
        objects_root = self.objects_root.resolve()
        if resolved_parent != objects_root / snapshot_hash[:2]:
            raise PoolIntegrityError("snapshot path escaped the object store")
        return candidate

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    def verify_object(self, snapshot_hash: str) -> Path:
        path = self.object_path(snapshot_hash)
        if not path.is_file() or path.is_symlink():
            raise PoolIntegrityError(f"snapshot object is missing: {snapshot_hash}")
        actual_hash, _ = self._hash_file(path)
        if actual_hash != snapshot_hash:
            raise PoolIntegrityError(
                f"snapshot object hash mismatch: {snapshot_hash}"
            )
        audit_jsonl_no_encrypted_content(path)
        return path

    def _install_object(
        self,
        snapshot: SnapshotResult,
        *,
        dry_run: bool,
    ) -> Path:
        if snapshot.path.is_symlink() or not snapshot.path.is_file():
            raise PoolIntegrityError("snapshot staging path is not a regular file")
        actual_hash, actual_size = self._hash_file(snapshot.path)
        if actual_hash != snapshot.sha256 or actual_size != snapshot.size:
            raise PoolIntegrityError("staged snapshot changed before object installation")
        audit_jsonl_no_encrypted_content(snapshot.path)
        destination = self.object_path(snapshot.sha256)
        if destination.exists():
            self.verify_object(snapshot.sha256)
            return destination
        if dry_run:
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{snapshot.sha256}.{uuid.uuid4().hex}.tmp"
        try:
            digest = hashlib.sha256()
            with snapshot.path.open("rb") as source, temporary.open("xb") as target:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            if digest.hexdigest() != snapshot.sha256:
                raise PoolIntegrityError("object hash changed during atomic installation")
            self.security.verify_encrypted_file(temporary)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)
            self.verify_object(snapshot.sha256)
            return destination
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _commit_id(fields: dict[str, Any]) -> str:
        return hashlib.sha256(_canonical_json(fields).encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_commit(row: sqlite3.Row, ref_name: str | None = None) -> CommitRecord:
        return CommitRecord(
            id=row["id"],
            lineage_id=row["lineage_id"],
            lineage_name=row["lineage_name"],
            parent_id=row["parent_id"],
            snapshot_hash=row["snapshot_hash"],
            created_at=row["created_at"],
            message=row["message"],
            source_thread_id=row["source_thread_id"],
            source_target=row["source_target"],
            title=row["title"],
            cwd=row["cwd"],
            ref_name=ref_name,
        )

    def publish(
        self,
        lineage_name: str,
        snapshot: SnapshotResult,
        *,
        title: str,
        source_target: str,
        message: str = "Initial shared conversation",
        dry_run: bool = False,
    ) -> CommitRecord:
        self.verify_initialized()
        lineage_name = self._validate_lineage_name(lineage_name)
        title = _redact_text(str(title))
        message = _redact_text(str(message))
        self._install_object(snapshot, dry_run=dry_run)
        portable_source_target = "local-portable-snapshot"
        created_at = now_iso()
        lineage_id = str(uuid.uuid4())
        fields = {
            "lineageId": lineage_id,
            "parentId": None,
            "snapshotHash": snapshot.sha256,
            "createdAt": created_at,
            "message": message,
            "sourceThreadId": PORTABLE_THREAD_ID,
            "sourceTarget": portable_source_target,
            "title": title,
            "cwd": snapshot.source_cwd,
        }
        commit_id = self._commit_id(fields)
        record = CommitRecord(
            id=commit_id,
            lineage_id=lineage_id,
            lineage_name=lineage_name,
            parent_id=None,
            snapshot_hash=snapshot.sha256,
            created_at=created_at,
            message=message,
            source_thread_id=PORTABLE_THREAD_ID,
            source_target=portable_source_target,
            title=title,
            cwd=snapshot.source_cwd,
            ref_name="main",
        )
        if dry_run:
            with self._connect(readonly=True) as connection:
                existing = connection.execute(
                    "SELECT 1 FROM lineages WHERE name = ? COLLATE NOCASE",
                    (lineage_name,),
                ).fetchone()
                if existing:
                    raise PoolConflictError(
                        f"conversation {lineage_name!r} already exists"
                    )
            return record
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM lineages WHERE name = ? COLLATE NOCASE",
                (lineage_name,),
            ).fetchone()
            if existing:
                raise PoolConflictError(
                    f"conversation {lineage_name!r} already exists"
                )
            connection.execute(
                """
                INSERT INTO lineages(id, name, created_at, title, source_cwd)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    lineage_id,
                    lineage_name,
                    created_at,
                    title,
                    snapshot.source_cwd,
                ),
            )
            connection.execute(
                """
                INSERT INTO commits(
                    id, lineage_id, parent_id, snapshot_hash, created_at,
                    message, source_thread_id, source_target, title, cwd
                ) VALUES(?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id,
                    lineage_id,
                    snapshot.sha256,
                    created_at,
                    message,
                    PORTABLE_THREAD_ID,
                    portable_source_target,
                    title,
                    snapshot.source_cwd,
                ),
            )
            connection.execute(
                """
                INSERT INTO refs(lineage_id, name, commit_id, updated_at)
                VALUES(?, 'main', ?, ?)
                """,
                (lineage_id, commit_id, created_at),
            )
        return record

    def resolve(
        self,
        lineage_name: str,
        *,
        ref_name: str = "main",
        commit_id: str | None = None,
    ) -> CommitRecord:
        self.verify_initialized()
        with self._connect(readonly=True) as connection:
            lineage = connection.execute(
                "SELECT id, name FROM lineages WHERE name = ? COLLATE NOCASE",
                (lineage_name,),
            ).fetchone()
            if lineage is None:
                raise ConversationPoolError(
                    f"shared conversation {lineage_name!r} was not found"
                )
            if commit_id:
                if not re.fullmatch(r"[0-9a-f]{4,64}", commit_id):
                    raise ConversationPoolError(f"invalid commit id: {commit_id!r}")
                rows = connection.execute(
                    """
                    SELECT c.*, l.name AS lineage_name
                    FROM commits c JOIN lineages l ON l.id = c.lineage_id
                    WHERE c.lineage_id = ? AND c.id LIKE ?
                    """,
                    (lineage["id"], f"{commit_id}%"),
                ).fetchall()
                if not rows:
                    raise ConversationPoolError(
                        f"commit {commit_id!r} was not found"
                    )
                if len(rows) > 1:
                    raise ConversationPoolError(
                        f"commit prefix {commit_id!r} is ambiguous"
                    )
                record = self._row_to_commit(rows[0])
            else:
                ref_name = self._validate_ref_name(ref_name)
                row = connection.execute(
                    """
                    SELECT c.*, l.name AS lineage_name
                    FROM refs r
                    JOIN commits c ON c.id = r.commit_id
                    JOIN lineages l ON l.id = r.lineage_id
                    WHERE r.lineage_id = ? AND r.name = ? COLLATE NOCASE
                    """,
                    (lineage["id"], ref_name),
                ).fetchone()
                if row is None:
                    raise ConversationPoolError(
                        f"reference {ref_name!r} was not found"
                    )
                record = self._row_to_commit(row, ref_name)
        self.verify_object(record.snapshot_hash)
        return record

    def push(
        self,
        *,
        lineage_id: str,
        ref_name: str,
        base_commit: str,
        snapshot: SnapshotResult,
        source_target: str,
        title: str,
        message: str = "Update shared conversation",
        new_branch: str | None = None,
        dry_run: bool = False,
    ) -> CommitRecord:
        self.verify_initialized()
        title = _redact_text(str(title))
        message = _redact_text(str(message))
        portable_source_target = "local-portable-snapshot"
        ref_name = self._validate_ref_name(ref_name)
        branch_name = (
            self._validate_ref_name(new_branch) if new_branch is not None else ref_name
        )
        with self._connect(readonly=True) as connection:
            lineage = connection.execute(
                "SELECT id, name FROM lineages WHERE id = ?",
                (lineage_id,),
            ).fetchone()
            if lineage is None:
                raise ConversationPoolError("mapped conversation lineage is missing")
            base = connection.execute(
                "SELECT id, snapshot_hash FROM commits WHERE id = ? AND lineage_id = ?",
                (base_commit, lineage_id),
            ).fetchone()
            if base is None:
                raise ConversationPoolError("mapped base commit is missing")
            current = connection.execute(
                """
                SELECT r.commit_id, c.snapshot_hash
                FROM refs r JOIN commits c ON c.id = r.commit_id
                WHERE r.lineage_id = ? AND r.name = ? COLLATE NOCASE
                """,
                (lineage_id, ref_name),
            ).fetchone()
            if current is None:
                raise ConversationPoolError(f"mapped reference {ref_name!r} is missing")
            if new_branch is not None:
                existing_branch = connection.execute(
                    """
                    SELECT 1 FROM refs
                    WHERE lineage_id = ? AND name = ? COLLATE NOCASE
                    """,
                    (lineage_id, branch_name),
                ).fetchone()
                if existing_branch:
                    raise PoolConflictError(
                        f"reference {branch_name!r} already exists"
                    )
        if new_branch is None:
            current_object = self.verify_object(str(current["snapshot_hash"]))
            if semantic_snapshot_hash(current_object) == semantic_snapshot_hash(
                snapshot.path
            ):
                return self.resolve(str(lineage["name"]), ref_name=ref_name)
            if current["commit_id"] != base_commit:
                raise PoolConflictError(
                    f"{ref_name} moved from {base_commit[:12]} to "
                    f"{current['commit_id'][:12]}; push is not fast-forward"
                )
        self._install_object(snapshot, dry_run=dry_run)

        created_at = now_iso()
        fields = {
            "lineageId": lineage_id,
            "parentId": base_commit,
            "snapshotHash": snapshot.sha256,
            "createdAt": created_at,
            "message": message,
            "sourceThreadId": PORTABLE_THREAD_ID,
            "sourceTarget": portable_source_target,
            "title": title,
            "cwd": snapshot.source_cwd,
        }
        commit_id = self._commit_id(fields)
        record = CommitRecord(
            id=commit_id,
            lineage_id=lineage_id,
            lineage_name=str(lineage["name"]),
            parent_id=base_commit,
            snapshot_hash=snapshot.sha256,
            created_at=created_at,
            message=message,
            source_thread_id=PORTABLE_THREAD_ID,
            source_target=portable_source_target,
            title=title,
            cwd=snapshot.source_cwd,
            ref_name=branch_name,
        )
        if dry_run:
            return record
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT commit_id FROM refs
                WHERE lineage_id = ? AND name = ? COLLATE NOCASE
                """,
                (lineage_id, ref_name),
            ).fetchone()
            if current is None:
                raise PoolConflictError("mapped reference disappeared during push")
            if new_branch is None and current["commit_id"] != base_commit:
                raise PoolConflictError("reference moved during push")
            if new_branch is not None:
                existing_branch = connection.execute(
                    """
                    SELECT 1 FROM refs
                    WHERE lineage_id = ? AND name = ? COLLATE NOCASE
                    """,
                    (lineage_id, branch_name),
                ).fetchone()
                if existing_branch:
                    raise PoolConflictError(
                        f"reference {branch_name!r} already exists"
                    )
            connection.execute(
                """
                INSERT INTO commits(
                    id, lineage_id, parent_id, snapshot_hash, created_at,
                    message, source_thread_id, source_target, title, cwd
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id,
                    lineage_id,
                    base_commit,
                    snapshot.sha256,
                    created_at,
                    message,
                    PORTABLE_THREAD_ID,
                    portable_source_target,
                    title,
                    snapshot.source_cwd,
                ),
            )
            if new_branch is None:
                updated = connection.execute(
                    """
                    UPDATE refs SET commit_id = ?, updated_at = ?
                    WHERE lineage_id = ? AND name = ? COLLATE NOCASE
                      AND commit_id = ?
                    """,
                    (
                        commit_id,
                        created_at,
                        lineage_id,
                        ref_name,
                        base_commit,
                    ),
                )
                if updated.rowcount != 1:
                    raise PoolConflictError("reference moved during push")
            else:
                connection.execute(
                    """
                    INSERT INTO refs(lineage_id, name, commit_id, updated_at)
                    VALUES(?, ?, ?, ?)
                    """,
                    (lineage_id, branch_name, commit_id, created_at),
                )
        return record

    def list_lineages(self) -> list[dict[str, Any]]:
        self.verify_initialized()
        with self._connect(readonly=True) as connection:
            rows = connection.execute(
                """
                SELECT l.id, l.name, l.created_at, l.title, l.source_cwd,
                       r.name AS ref_name, r.commit_id, r.updated_at
                FROM lineages l
                LEFT JOIN refs r ON r.lineage_id = l.id
                ORDER BY l.name COLLATE NOCASE, r.name COLLATE NOCASE
                """
            ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(
                row["id"],
                {
                    "id": row["id"],
                    "name": row["name"],
                    "createdAt": row["created_at"],
                    "title": row["title"],
                    "cwd": row["source_cwd"],
                    "refs": {},
                },
            )
            if row["ref_name"] is not None:
                item["refs"][row["ref_name"]] = {
                    "commit": row["commit_id"],
                    "updatedAt": row["updated_at"],
                }
        return list(grouped.values())

    def log(
        self,
        lineage_name: str,
        *,
        ref_name: str = "main",
    ) -> list[CommitRecord]:
        head = self.resolve(lineage_name, ref_name=ref_name)
        records: list[CommitRecord] = []
        current_id: str | None = head.id
        with self._connect(readonly=True) as connection:
            while current_id:
                row = connection.execute(
                    """
                    SELECT c.*, l.name AS lineage_name
                    FROM commits c JOIN lineages l ON l.id = c.lineage_id
                    WHERE c.id = ?
                    """,
                    (current_id,),
                ).fetchone()
                if row is None:
                    raise PoolIntegrityError(
                        f"commit chain is missing {current_id}"
                    )
                record = self._row_to_commit(
                    row,
                    ref_name if current_id == head.id else None,
                )
                records.append(record)
                current_id = record.parent_id
        return records

    def doctor(self) -> dict[str, Any]:
        self.verify_initialized()
        security = self.security.verify(self.root)
        with self._connect(readonly=True) as connection:
            hashes = [
                row["snapshot_hash"]
                for row in connection.execute(
                    "SELECT DISTINCT snapshot_hash FROM commits"
                )
            ]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise PoolIntegrityError(f"SQLite integrity check failed: {integrity}")
        for snapshot_hash in hashes:
            self.verify_object(snapshot_hash)
        return {
            "pool": str(self.root),
            "poolId": self.pool_id(),
            "schemaVersion": SCHEMA_VERSION,
            "objectsVerified": len(hashes),
            "sqliteIntegrity": "ok",
            "security": security,
        }


class LocalMappingStore:
    """Private per-user working-copy mappings; never written to pool objects."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.database_path = self.root / "mappings.sqlite3"
        self.config_path = self.root / "config.json"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        try:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mappings (
                        pool_id TEXT NOT NULL,
                        target_id TEXT NOT NULL,
                        target_home_hash TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        lineage_id TEXT NOT NULL,
                        lineage_name TEXT NOT NULL,
                        ref_name TEXT NOT NULL,
                        base_commit TEXT NOT NULL,
                        rollout_path TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(pool_id, target_home_hash, thread_id)
                    )
                    """
                )
        finally:
            connection.close()

    @staticmethod
    def target_home_hash(target_home: Path) -> str:
        normalized = os.path.normcase(str(target_home.resolve()))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def set_pool_path(self, pool_path: Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schemaVersion": SCHEMA_VERSION,
            "poolPath": str(pool_path.resolve()),
            "updatedAt": now_iso(),
        }
        temporary = self.root / f".config-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    )
                    + "\n"
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
        finally:
            temporary.unlink(missing_ok=True)

    def get_pool_path(self) -> Path:
        if not self.config_path.is_file():
            return DEFAULT_POOL_PATH
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PoolIntegrityError(f"invalid local pool config: {exc}") from exc
        value = payload.get("poolPath") if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            raise PoolIntegrityError("local pool config has no poolPath")
        return Path(value).resolve()

    def register(
        self,
        *,
        pool_id: str,
        target_id: str,
        target_home: Path,
        thread_id: str,
        lineage_id: str,
        lineage_name: str,
        ref_name: str,
        base_commit: str,
        rollout_path: Path,
    ) -> MappingRecord:
        self.initialize()
        home_hash = self.target_home_hash(target_home)
        timestamp = now_iso()
        connection = sqlite3.connect(self.database_path)
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO mappings(
                        pool_id, target_id, target_home_hash, thread_id,
                        lineage_id, lineage_name, ref_name, base_commit,
                        rollout_path, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pool_id, target_home_hash, thread_id) DO UPDATE SET
                        target_id = excluded.target_id,
                        lineage_id = excluded.lineage_id,
                        lineage_name = excluded.lineage_name,
                        ref_name = excluded.ref_name,
                        base_commit = excluded.base_commit,
                        rollout_path = excluded.rollout_path,
                        updated_at = excluded.updated_at
                    """,
                    (
                        pool_id,
                        target_id,
                        home_hash,
                        thread_id,
                        lineage_id,
                        lineage_name,
                        ref_name,
                        base_commit,
                        str(rollout_path.resolve()),
                        timestamp,
                    ),
                )
        finally:
            connection.close()
        return MappingRecord(
            pool_id=pool_id,
            target_id=target_id,
            target_home_hash=home_hash,
            thread_id=thread_id,
            lineage_id=lineage_id,
            lineage_name=lineage_name,
            ref_name=ref_name,
            base_commit=base_commit,
            rollout_path=str(rollout_path.resolve()),
            updated_at=timestamp,
        )

    @staticmethod
    def _row_to_mapping(row: sqlite3.Row) -> MappingRecord:
        return MappingRecord(
            pool_id=row["pool_id"],
            target_id=row["target_id"],
            target_home_hash=row["target_home_hash"],
            thread_id=row["thread_id"],
            lineage_id=row["lineage_id"],
            lineage_name=row["lineage_name"],
            ref_name=row["ref_name"],
            base_commit=row["base_commit"],
            rollout_path=row["rollout_path"],
            updated_at=row["updated_at"],
        )

    def find(
        self,
        *,
        pool_id: str,
        target_home: Path,
        thread_id: str | None = None,
    ) -> list[MappingRecord]:
        if not self.database_path.is_file():
            return []
        home_hash = self.target_home_hash(target_home)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            if thread_id:
                rows = connection.execute(
                    """
                    SELECT * FROM mappings
                    WHERE pool_id = ? AND target_home_hash = ? AND thread_id = ?
                    ORDER BY updated_at DESC
                    """,
                    (pool_id, home_hash, thread_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM mappings
                    WHERE pool_id = ? AND target_home_hash = ?
                    ORDER BY updated_at DESC
                    """,
                    (pool_id, home_hash),
                ).fetchall()
            return [self._row_to_mapping(row) for row in rows]
        finally:
            connection.close()


@contextlib.contextmanager
def temporary_snapshot(
    source_path: Path,
    *,
    expected_thread_id: str | None = None,
) -> Iterator[SnapshotResult]:
    """Context-manager style generator used by the CLI and tests."""

    with tempfile.TemporaryDirectory(prefix="apicodex-share-snapshot-") as directory:
        destination = Path(directory) / "portable.jsonl"
        yield sanitize_rollout(
            source_path,
            destination,
            expected_thread_id=expected_thread_id,
        )
