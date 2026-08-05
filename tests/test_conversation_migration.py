from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from codex_conversation_pool import sanitize_rollout
from conversation_migration import (
    list_claude_sessions,
    materialize_claude_session,
    snapshot_claude_session,
)
from tests.test_codex_conversation_pool import (
    base_rows,
    response,
    write_rollout,
)


def write_claude_rows(
    home: Path,
    *,
    session_id: str,
    cwd: Path,
    rows: list[dict[str, object]],
) -> Path:
    project = home / "projects" / "C--work-demo"
    path = project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payloads: list[dict[str, object]] = [
        {
            "type": "custom-title",
            "customTitle": "Claude source title",
            "sessionId": session_id,
        }
    ]
    payloads.extend(rows)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in payloads
        ),
        encoding="utf-8",
    )
    return path


def claude_message(
    *,
    session_id: str,
    cwd: Path,
    row_type: str,
    row_uuid: str,
    parent_uuid: str | None,
    content,
    timestamp: str,
    **extra,
) -> dict[str, object]:
    message: dict[str, object] = {
        "role": row_type,
        "content": content,
    }
    if row_type == "assistant":
        message.update(
            {
                "model": "claude-fable-5",
                "id": f"msg_{row_uuid.replace('-', '')}",
                "type": "message",
                "stop_reason": "end_turn",
            }
        )
    return {
        "parentUuid": parent_uuid,
        "isSidechain": False,
        "type": row_type,
        "message": message,
        "uuid": row_uuid,
        "timestamp": timestamp,
        "cwd": str(cwd),
        "sessionId": session_id,
        "version": "2.1.220",
        **extra,
    }


class ConversationMigrationTests(unittest.TestCase):
    def test_claude_snapshot_keeps_visible_messages_and_drops_private_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "claude"
            cwd = root / "work"
            cwd.mkdir()
            session_id = "11111111-1111-4111-8111-111111111111"
            source = write_claude_rows(
                home,
                session_id=session_id,
                cwd=cwd,
                rows=[
                    claude_message(
                        session_id=session_id,
                        cwd=cwd,
                        row_type="user",
                        row_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        parent_uuid=None,
                        timestamp="2026-01-01T00:00:00Z",
                        content=[
                            {"type": "text", "text": "inspect this"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "aGVsbG8=",
                                },
                            },
                        ],
                    ),
                    claude_message(
                        session_id=session_id,
                        cwd=cwd,
                        row_type="assistant",
                        row_uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        parent_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                        timestamp="2026-01-01T00:00:01Z",
                        content=[
                            {
                                "type": "thinking",
                                "thinking": "private chain of thought",
                                "signature": "private-signature",
                            },
                            {"type": "text", "text": "visible answer"},
                            {
                                "type": "tool_use",
                                "id": "tool-secret",
                                "name": "Read",
                                "input": {"api_key": "sk-secret-value"},
                            },
                        ],
                    ),
                    claude_message(
                        session_id=session_id,
                        cwd=cwd,
                        row_type="user",
                        row_uuid="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                        parent_uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                        timestamp="2026-01-01T00:00:02Z",
                        content=[
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-secret",
                                "content": "raw tool result",
                            }
                        ],
                        toolUseResult={"content": "raw tool result"},
                        sourceToolAssistantUUID=(
                            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                        ),
                    ),
                    claude_message(
                        session_id=session_id,
                        cwd=cwd,
                        row_type="assistant",
                        row_uuid="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                        parent_uuid="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                        timestamp="2026-01-01T00:00:03Z",
                        content=[{"type": "text", "text": "tool work complete"}],
                    ),
                    claude_message(
                        session_id=session_id,
                        cwd=cwd,
                        row_type="user",
                        row_uuid="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                        parent_uuid="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                        timestamp="2026-01-01T00:00:04Z",
                        content="continue",
                    ),
                ],
            )
            before = source.read_bytes()

            converted = snapshot_claude_session(home, session_id)
            try:
                portable = converted.snapshot.path.read_text(encoding="utf-8")
                rows = [
                    json.loads(line)
                    for line in portable.splitlines()
                ]
                messages = [
                    row["payload"]
                    for row in rows
                    if row["type"] == "response_item"
                    and row["payload"]["type"] == "message"
                ]
                self.assertEqual(
                    [message["role"] for message in messages],
                    ["user", "assistant", "user"],
                )
                self.assertIn("visible answer", str(messages))
                self.assertIn("tool work complete", str(messages))
                self.assertIn("data:image/png;base64,aGVsbG8=", str(messages))
                self.assertNotIn("private chain of thought", portable)
                self.assertNotIn("private-signature", portable)
                self.assertNotIn("sk-secret-value", portable)
                self.assertNotIn("raw tool result", portable)
                self.assertEqual(converted.session.status, "interrupted")
            finally:
                converted.cleanup()
            self.assertEqual(source.read_bytes(), before)

    def test_materializes_independent_resumable_claude_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "work"
            cwd.mkdir()
            source = root / "source.jsonl"
            portable = root / "portable.jsonl"
            rows = base_rows(
                thread_id="01900000-0000-7000-8000-000000000123"
            )
            rows[0]["payload"]["cwd"] = str(cwd)  # type: ignore[index]
            rows.extend(
                [
                    response(
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "portable request token=secret-value",
                                },
                                {
                                    "type": "input_image",
                                    "image_url": (
                                        "data:image/png;base64,aGVsbG8="
                                    ),
                                },
                            ],
                        }
                    ),
                    response(
                        {
                            "type": "reasoning",
                            "encrypted_content": "ciphertext",
                        }
                    ),
                    response(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "portable answer",
                                }
                            ],
                        }
                    ),
                ]
            )
            write_rollout(source, rows)
            sanitize_rollout(source, portable)
            before = portable.read_bytes()
            target_home = root / "target-claude"

            result = materialize_claude_session(
                portable,
                target_home=target_home,
                cwd=cwd,
                title="Round trip",
                model="claude-test",
                target_node="relay",
                claude_version="2.1.220 (Claude Code)",
            )

            self.assertNotEqual(
                result["threadId"],
                "01900000-0000-7000-8000-000000000123",
            )
            self.assertIn(
                f"--resume {result['threadId']}",
                result["resumeCommand"],
            )
            sessions = list_claude_sessions(target_home)
            self.assertEqual([session.id for session in sessions], [result["threadId"]])
            self.assertEqual(sessions[0].title, "Round trip")
            transcript = sessions[0].path.read_text(encoding="utf-8")
            self.assertIn("portable request", transcript)
            self.assertIn("portable answer", transcript)
            self.assertIn('"type":"image"', transcript)
            self.assertNotIn("ciphertext", transcript)
            self.assertNotIn("secret-value", transcript)
            self.assertEqual(portable.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
