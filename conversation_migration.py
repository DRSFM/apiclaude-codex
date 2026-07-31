#!/usr/bin/env python3
"""Safe visible-history conversion between Claude Code and Codex sessions."""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from codex_conversation_pool import (
    ConversationPoolError,
    SnapshotChangedError,
    SnapshotResult,
    audit_jsonl_no_encrypted_content,
    sanitize_rollout,
)


CLAUDE_SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
CLAUDE_PROJECT_COMPONENT_RE = re.compile(r"[^A-Za-z0-9]")
MIGRATION_FORMAT_VERSION = "apiclaude-migration-v1"
DROP_USER_TEXT_PREFIXES = (
    "## Context Usage",
    "[Image: source:",
    "[Image: original ",
)
DROP_USER_TEXTS = frozenset({"[Request interrupted by user]"})


@dataclass(frozen=True)
class VisiblePart:
    type: str
    text: str | None = None
    image_url: str | None = None


@dataclass(frozen=True)
class VisibleMessage:
    role: str
    content: tuple[VisiblePart, ...]
    timestamp: str


@dataclass(frozen=True)
class ClaudeSession:
    id: str
    path: Path
    title: str
    preview: str
    cwd: Path
    model: str | None
    created_at: float
    updated_at: float
    status: str
    available: bool


@dataclass(frozen=True)
class ClaudeSnapshot:
    session: ClaudeSession
    snapshot: SnapshotResult
    temporary: tempfile.TemporaryDirectory[str]

    def cleanup(self) -> None:
        self.temporary.cleanup()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _valid_session_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if not CLAUDE_SESSION_ID_RE.fullmatch(normalized):
        raise ConversationPoolError(f"invalid Claude session ID: {value!r}")
    try:
        uuid.UUID(normalized)
    except ValueError as exc:
        raise ConversationPoolError(
            f"invalid Claude session ID: {value!r}"
        ) from exc
    return normalized


def _projects_root(home: Path) -> Path:
    resolved_home = home.resolve()
    if resolved_home.exists() and resolved_home.is_symlink():
        raise ConversationPoolError(
            f"Claude config home must not be a symbolic link: {resolved_home}"
        )
    return resolved_home / "projects"


def _validate_claude_path(home: Path, path: Path) -> Path:
    projects = _projects_root(home).resolve()
    resolved = path.resolve()
    if resolved == projects or not resolved.is_relative_to(projects):
        raise ConversationPoolError(
            f"Claude session escapes the selected config home: {resolved}"
        )
    if path.is_symlink() or not resolved.is_file():
        raise ConversationPoolError(
            f"Claude session is not a regular file: {resolved}"
        )
    if resolved.parent.parent != projects:
        raise ConversationPoolError(
            f"Claude session has an unsupported project layout: {resolved}"
        )
    return resolved


def _locate_claude_session(home: Path, session_id: str) -> Path:
    normalized = _valid_session_id(session_id)
    projects = _projects_root(home)
    if not projects.is_dir():
        raise ConversationPoolError(
            f"Claude config home has no local conversations: {home.resolve()}"
        )
    matches: list[Path] = []
    for candidate in projects.glob(f"*/{normalized}.jsonl"):
        try:
            matches.append(_validate_claude_path(home, candidate))
        except ConversationPoolError:
            continue
    if not matches:
        raise ConversationPoolError(
            f"Claude session {normalized!r} was not found in {home.resolve()}"
        )
    unique = {str(path).lower(): path for path in matches}
    if len(unique) != 1:
        raise ConversationPoolError(
            f"Claude session {normalized!r} is ambiguous in {home.resolve()}"
        )
    return next(iter(unique.values()))


def _text_from_claude_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def _summary_from_claude_path(home: Path, path: Path) -> ClaudeSession | None:
    resolved = _validate_claude_path(home, path)
    session_id = _valid_session_id(resolved.stem)
    before = _file_identity(resolved)
    title = ""
    first_user = ""
    last_user = ""
    cwd: Path | None = None
    model: str | None = None
    first_timestamp = ""
    last_visible_role = ""
    last_assistant_stop = ""
    with resolved.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise SnapshotChangedError(
                    f"Claude session ends with a partial line at {line_number}"
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ConversationPoolError(
                    f"invalid Claude JSON at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise ConversationPoolError(
                    f"invalid Claude row at line {line_number}"
                )
            row_session = row.get("sessionId") or row.get("session_id")
            if isinstance(row_session, str) and row_session.lower() != session_id:
                raise ConversationPoolError(
                    f"Claude session ID mismatch at line {line_number}"
                )
            if not first_timestamp and isinstance(row.get("timestamp"), str):
                first_timestamp = row["timestamp"]
            if cwd is None and isinstance(row.get("cwd"), str) and row["cwd"]:
                candidate_cwd = Path(row["cwd"])
                if candidate_cwd.is_absolute():
                    cwd = candidate_cwd.resolve()
            row_type = row.get("type")
            if row_type == "custom-title":
                candidate = row.get("customTitle")
                if isinstance(candidate, str) and candidate.strip():
                    title = candidate.strip()
                continue
            if row_type == "ai-title" and not title:
                candidate = row.get("aiTitle")
                if isinstance(candidate, str) and candidate.strip():
                    title = candidate.strip()
                continue
            if row.get("isSidechain") is True or row.get("isMeta") is True:
                continue
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            if row_type == "user":
                if row.get("toolUseResult") is not None or row.get(
                    "sourceToolAssistantUUID"
                ):
                    continue
                text = _text_from_claude_content(message.get("content"))
                if (
                    not text
                    or text in DROP_USER_TEXTS
                    or text.startswith(DROP_USER_TEXT_PREFIXES)
                ):
                    continue
                first_user = first_user or text
                last_user = text
                last_visible_role = "user"
            elif row_type == "assistant":
                last_assistant_stop = str(message.get("stop_reason") or "")
                candidate_model = message.get("model")
                if (
                    isinstance(candidate_model, str)
                    and candidate_model
                    and candidate_model != "<synthetic>"
                ):
                    model = candidate_model
                text = _text_from_claude_content(message.get("content"))
                if text:
                    last_visible_role = "assistant"
    if before != _file_identity(resolved):
        raise SnapshotChangedError(
            f"Claude session changed while it was inspected: {resolved}"
        )
    if not first_user or cwd is None:
        return None
    stat = resolved.stat()
    created_at = stat.st_ctime
    if first_timestamp:
        try:
            created_at = datetime.fromisoformat(
                first_timestamp.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            pass
    return ClaudeSession(
        id=session_id,
        path=resolved,
        title=(title or first_user)[:160],
        preview=last_user[:240],
        cwd=cwd,
        model=model,
        created_at=created_at,
        updated_at=stat.st_mtime,
        status=(
            "interrupted"
            if last_visible_role == "user"
            or (
                last_visible_role == "assistant"
                and last_assistant_stop not in {"end_turn", "stop_sequence"}
            )
            else "idle"
        ),
        available=True,
    )


def list_claude_sessions(
    home: Path,
    *,
    limit: int = 100,
) -> list[ClaudeSession]:
    projects = _projects_root(home)
    if not projects.is_dir():
        return []
    bounded_limit = max(1, min(int(limit), 200))
    candidates = sorted(
        (
            candidate
            for candidate in projects.glob("*/*.jsonl")
            if candidate.is_file() and not candidate.is_symlink()
        ),
        key=lambda candidate: candidate.stat().st_mtime_ns,
        reverse=True,
    )
    result: list[ClaudeSession] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.stem.lower() in seen:
            continue
        try:
            session = _summary_from_claude_path(home, candidate)
        except (ConversationPoolError, OSError, ValueError):
            continue
        if session is None:
            continue
        result.append(session)
        seen.add(session.id)
        if len(result) >= bounded_limit:
            break
    return result


def _visible_parts_from_claude_user(content: Any) -> tuple[VisiblePart, ...]:
    if isinstance(content, str):
        return (VisiblePart(type="text", text=content),) if content.strip() else ()
    if not isinstance(content, list):
        return ()
    parts: list[VisiblePart] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "tool_result":
            return ()
        if block_type == "text" and isinstance(block.get("text"), str):
            if block["text"].strip():
                parts.append(VisiblePart(type="text", text=block["text"]))
        elif block_type == "image":
            source = block.get("source")
            if not isinstance(source, dict):
                continue
            if (
                source.get("type") == "base64"
                and isinstance(source.get("media_type"), str)
                and isinstance(source.get("data"), str)
                and source["data"]
            ):
                parts.append(
                    VisiblePart(
                        type="image",
                        image_url=(
                            f"data:{source['media_type']};base64,{source['data']}"
                        ),
                    )
                )
    return tuple(parts)


def _visible_messages_from_claude(
    home: Path,
    session_id: str,
) -> tuple[ClaudeSession, list[VisibleMessage]]:
    path = _locate_claude_session(home, session_id)
    session = _summary_from_claude_path(home, path)
    if session is None:
        raise ConversationPoolError(
            f"Claude session {session_id!r} has no portable visible history"
        )
    before = _file_identity(path)
    messages: list[VisibleMessage] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise SnapshotChangedError(
                    f"Claude session ends with a partial line at {line_number}"
                )
            row = json.loads(line)
            if (
                not isinstance(row, dict)
                or row.get("isSidechain") is True
                or row.get("isMeta") is True
            ):
                continue
            row_type = row.get("type")
            message = row.get("message")
            if not isinstance(message, dict):
                continue
            timestamp = (
                str(row.get("timestamp") or "").strip()
                or _now_iso()
            )
            if row_type == "user":
                if row.get("toolUseResult") is not None or row.get(
                    "sourceToolAssistantUUID"
                ):
                    continue
                parts = _visible_parts_from_claude_user(message.get("content"))
                text = "\n".join(
                    part.text or "" for part in parts if part.type == "text"
                ).strip()
                if (
                    not parts
                    or text in DROP_USER_TEXTS
                    or text.startswith(DROP_USER_TEXT_PREFIXES)
                ):
                    continue
                if (
                    messages
                    and messages[-1].role == "user"
                    and all(part.type == "text" for part in messages[-1].content)
                    and text.startswith(
                        "\n".join(
                            part.text or ""
                            for part in messages[-1].content
                        ).strip()
                    )
                ):
                    messages[-1] = VisibleMessage("user", parts, timestamp)
                else:
                    messages.append(VisibleMessage("user", parts, timestamp))
            elif row_type == "assistant":
                texts = tuple(
                    VisiblePart(type="text", text=str(block.get("text")))
                    for block in (message.get("content") or [])
                    if isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                    and block["text"].strip()
                )
                if texts:
                    if messages and messages[-1].role == "assistant":
                        messages[-1] = VisibleMessage(
                            "assistant",
                            messages[-1].content + texts,
                            timestamp,
                        )
                    else:
                        messages.append(
                            VisibleMessage("assistant", texts, timestamp)
                        )
    if before != _file_identity(path):
        raise SnapshotChangedError(
            f"Claude session changed while it was converted: {path}"
        )
    if not messages or messages[0].role != "user":
        raise ConversationPoolError(
            f"Claude session {session_id!r} has no portable user history"
        )
    return session, messages


def _codex_content(parts: Iterable[VisiblePart], role: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for part in parts:
        if part.type == "text" and part.text:
            result.append(
                {
                    "type": "input_text" if role == "user" else "output_text",
                    "text": part.text,
                }
            )
        elif role == "user" and part.type == "image" and part.image_url:
            result.append({"type": "input_image", "image_url": part.image_url})
    return result


def _write_synthetic_codex_source(
    path: Path,
    *,
    session: ClaudeSession,
    messages: list[VisibleMessage],
) -> None:
    rows: list[dict[str, Any]] = [
        {
            "timestamp": messages[0].timestamp,
            "type": "session_meta",
            "payload": {
                "session_id": session.id,
                "id": session.id,
                "timestamp": messages[0].timestamp,
                "cwd": str(session.cwd),
                "originator": "apiclaude-session-import",
                "source": "appServer",
                "thread_source": "user",
                "model_provider": "claude-code",
            },
        }
    ]
    interaction: list[VisibleMessage] = []

    def flush() -> None:
        if not interaction:
            return
        turn_id = str(uuid.uuid4())
        user = interaction[0]
        assistants = interaction[1:]
        user_text = "\n".join(
            part.text or "" for part in user.content if part.type == "text"
        ).strip()
        rows.extend(
            [
                {
                    "timestamp": user.timestamp,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id},
                },
                {
                    "timestamp": user.timestamp,
                    "type": "turn_context",
                    "payload": {"turn_id": turn_id, "cwd": str(session.cwd)},
                },
                {
                    "timestamp": user.timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": _codex_content(user.content, "user"),
                    },
                },
                {
                    "timestamp": user.timestamp,
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": user_text,
                        "images": [],
                        "local_images": [],
                    },
                },
            ]
        )
        assistant_text = "\n\n".join(
            part.text or ""
            for message in assistants
            for part in message.content
            if part.type == "text"
        ).strip()
        terminal_timestamp = (
            assistants[-1].timestamp if assistants else user.timestamp
        )
        if assistant_text:
            rows.extend(
                [
                    {
                        "timestamp": terminal_timestamp,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": assistant_text}
                            ],
                        },
                    },
                    {
                        "timestamp": terminal_timestamp,
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": assistant_text,
                            "phase": "final_answer",
                        },
                    },
                ]
            )
        rows.append(
            {
                "timestamp": terminal_timestamp,
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    "last_agent_message": assistant_text,
                },
            }
        )

    for message in messages:
        if message.role == "user":
            flush()
            interaction = [message]
        elif interaction:
            interaction.append(message)
    flush()
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


def snapshot_claude_session(home: Path, session_id: str) -> ClaudeSnapshot:
    session, messages = _visible_messages_from_claude(home, session_id)
    temporary = tempfile.TemporaryDirectory(prefix="apiclaude-share-")
    root = Path(temporary.name)
    synthetic = root / "claude-source.jsonl"
    portable = root / "portable.jsonl"
    try:
        _write_synthetic_codex_source(
            synthetic,
            session=session,
            messages=messages,
        )
        snapshot = sanitize_rollout(
            synthetic,
            portable,
            expected_thread_id=session.id,
        )
        return ClaudeSnapshot(
            session=session,
            snapshot=snapshot,
            temporary=temporary,
        )
    except BaseException:
        temporary.cleanup()
        raise


def _portable_payloads(path: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    before = _file_identity(path)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise SnapshotChangedError(
                    f"portable snapshot ends with a partial line at {line_number}"
                )
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ConversationPoolError(
                    f"invalid portable JSON at line {line_number}: {exc}"
                ) from exc
            if not isinstance(row, dict):
                raise ConversationPoolError(
                    f"invalid portable row at line {line_number}"
                )
            if row.get("type") == "compacted":
                compacted = row.get("payload")
                history = (
                    compacted.get("replacement_history")
                    if isinstance(compacted, dict)
                    else None
                )
                if not isinstance(history, list):
                    raise ConversationPoolError(
                        f"invalid compacted history at line {line_number}"
                    )
                payloads = [
                    payload for payload in history if isinstance(payload, dict)
                ]
            elif row.get("type") == "response_item":
                payload = row.get("payload")
                if isinstance(payload, dict):
                    payloads.append(payload)
    if before != _file_identity(path):
        raise SnapshotChangedError(
            f"portable snapshot changed while it was converted: {path}"
        )
    return payloads


def _visible_messages_from_portable(path: Path) -> list[VisibleMessage]:
    messages: list[VisibleMessage] = []
    for payload in _portable_payloads(path):
        if payload.get("type") != "message":
            continue
        role = str(payload.get("role") or "").lower()
        if role not in {"user", "assistant"}:
            continue
        parts: list[VisiblePart] = []
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"input_text", "output_text", "text"}:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(VisiblePart(type="text", text=text))
            elif role == "user" and block_type == "input_image":
                image_url = block.get("image_url")
                if isinstance(image_url, str) and image_url.startswith("data:image/"):
                    parts.append(
                        VisiblePart(type="image", image_url=image_url)
                    )
        if not parts:
            continue
        messages.append(
            VisibleMessage(
                role=role,
                content=tuple(parts),
                timestamp=_now_iso(),
            )
        )
    if not messages or messages[0].role != "user":
        raise ConversationPoolError(
            "portable snapshot has no Claude-compatible visible user history"
        )
    return messages


def _claude_project_key(cwd: Path) -> str:
    value = CLAUDE_PROJECT_COMPONENT_RE.sub("-", str(cwd.resolve()))
    return value or "project"


def _claude_image_source(image_url: str) -> dict[str, str] | None:
    match = re.fullmatch(
        r"data:(image/[A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)",
        image_url,
    )
    if not match:
        return None
    return {
        "type": "base64",
        "media_type": match.group(1),
        "data": match.group(2).replace("\r", "").replace("\n", ""),
    }


def _claude_user_content(parts: tuple[VisiblePart, ...]) -> str | list[dict[str, Any]]:
    if len(parts) == 1 and parts[0].type == "text":
        return parts[0].text or ""
    content: list[dict[str, Any]] = []
    for part in parts:
        if part.type == "text" and part.text:
            content.append({"type": "text", "text": part.text})
        elif part.type == "image" and part.image_url:
            source = _claude_image_source(part.image_url)
            if source:
                content.append({"type": "image", "source": source})
    return content


def _reject_private_claude_content(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {
                "thinking",
                "signature",
                "encrypted_content",
                "api_key",
                "authorization",
                "credential",
            }:
                raise ConversationPoolError(
                    f"private field survived Claude migration: {key}"
                )
            _reject_private_claude_content(item)
    elif isinstance(value, list):
        for item in value:
            _reject_private_claude_content(item)


def _validate_materialized_claude_session(
    path: Path,
    *,
    expected_session_id: str,
    expected_cwd: Path,
) -> None:
    expected = _valid_session_id(expected_session_id)
    uuids: set[str] = set()
    previous_uuid: str | None = None
    user_messages = 0
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.endswith("\n"):
                raise ConversationPoolError(
                    f"Claude target ends with a partial line at {line_number}"
                )
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ConversationPoolError(
                    f"Claude target row {line_number} is invalid"
                )
            _reject_private_claude_content(row)
            row_session = row.get("sessionId")
            if row_session != expected:
                raise ConversationPoolError(
                    f"Claude target session ID mismatch at line {line_number}"
                )
            if row.get("type") not in {"user", "assistant"}:
                continue
            if Path(str(row.get("cwd") or "")).resolve() != expected_cwd.resolve():
                raise ConversationPoolError(
                    f"Claude target cwd mismatch at line {line_number}"
                )
            row_uuid = str(row.get("uuid") or "")
            if not CLAUDE_SESSION_ID_RE.fullmatch(row_uuid) or row_uuid in uuids:
                raise ConversationPoolError(
                    f"Claude target message UUID is invalid at line {line_number}"
                )
            if row.get("parentUuid") != previous_uuid:
                raise ConversationPoolError(
                    f"Claude target parent chain is invalid at line {line_number}"
                )
            uuids.add(row_uuid)
            previous_uuid = row_uuid
            if row.get("type") == "user":
                user_messages += 1
    if user_messages <= 0:
        raise ConversationPoolError("Claude target has no user messages")
    audit_jsonl_no_encrypted_content(path)


def materialize_claude_session(
    portable_path: Path,
    *,
    target_home: Path,
    cwd: Path,
    title: str,
    model: str | None,
    target_node: str,
    claude_version: str | None = None,
) -> dict[str, Any]:
    portable_path = portable_path.resolve()
    cwd = cwd.resolve()
    if not cwd.is_dir():
        raise ConversationPoolError(
            f"target working directory does not exist: {cwd}; choose another cwd"
        )
    messages = _visible_messages_from_portable(portable_path)
    session_id = str(uuid.uuid4())
    projects = _projects_root(target_home)
    project_dir = projects / _claude_project_key(cwd)
    projects.mkdir(parents=True, exist_ok=True)
    if projects.is_symlink() or projects.resolve() != projects.absolute():
        raise ConversationPoolError(
            f"Claude projects directory is linked or unsafe: {projects}"
        )
    project_dir.mkdir(parents=True, exist_ok=True)
    if project_dir.is_symlink() or project_dir.resolve().parent != projects.resolve():
        raise ConversationPoolError(
            f"Claude project directory is linked or unsafe: {project_dir}"
        )
    if any(projects.glob(f"*/{session_id}.jsonl")):
        raise ConversationPoolError(
            f"generated Claude session ID already exists: {session_id}"
        )
    destination = project_dir / f"{session_id}.jsonl"
    version_parts = str(claude_version or "").strip().split()
    version = version_parts[0] if version_parts else MIGRATION_FORMAT_VERSION
    rows: list[dict[str, Any]] = [
        {
            "type": "custom-title",
            "customTitle": str(title).strip()[:200],
            "sessionId": session_id,
        }
    ]
    parent_uuid: str | None = None
    for message in messages:
        row_uuid = str(uuid.uuid4())
        common = {
            "parentUuid": parent_uuid,
            "isSidechain": False,
            "type": message.role,
            "uuid": row_uuid,
            "timestamp": message.timestamp,
            "userType": "external",
            "entrypoint": "cli",
            "cwd": str(cwd),
            "sessionId": session_id,
            "version": version,
            "gitBranch": "HEAD",
        }
        if message.role == "user":
            common["message"] = {
                "role": "user",
                "content": _claude_user_content(message.content),
            }
            common["permissionMode"] = "default"
        else:
            assistant_text = "\n\n".join(
                part.text or ""
                for part in message.content
                if part.type == "text"
            ).strip()
            if not assistant_text:
                continue
            common["message"] = {
                "model": model or "claude-migrated-history",
                "id": f"msg_migrated_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        rows.append(common)
        parent_uuid = row_uuid
    temporary = project_dir / f".{session_id}.{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(
                    json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        _validate_materialized_claude_session(
            temporary,
            expected_session_id=session_id,
            expected_cwd=cwd,
        )
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ConversationPoolError(
                f"Claude target session already exists: {destination}"
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        _validate_materialized_claude_session(
            destination,
            expected_session_id=session_id,
            expected_cwd=cwd,
        )
        return {
            "threadId": session_id,
            "sessionId": session_id,
            "title": str(title).strip()[:200],
            "cwd": str(cwd),
            "modelProvider": "claude-code",
            "model": model,
            "node": target_node,
            "resumeCommand": (
                f"apiclaude --api-profile {target_node} --resume {session_id}"
            ),
            "messageCount": len(messages),
        }
    except BaseException:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
