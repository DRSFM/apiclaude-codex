from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from codex_app_server import CodexAppServer, detect_fork_path_capability
from codex_conversation_pool import (
    PORTABLE_THREAD_ID,
    audit_jsonl_no_encrypted_content,
    audit_target_runtime_context,
    materialize_snapshot_for_target,
    sanitize_rollout,
)
from tests.test_codex_conversation_pool import (
    base_rows,
    response,
    write_rollout,
)


@unittest.skipUnless(shutil.which("codex"), "installed Codex CLI is required")
class AppServerIntegrationTests(unittest.TestCase):
    def test_fork_portable_path_into_isolated_codex_home(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="apicodex-app-server-test-",
            ignore_cleanup_errors=True,
        ) as directory:
            root = Path(directory)
            target_home = root / "target-codex-home"
            target_home.mkdir()
            source = root / "source.jsonl"
            portable = root / "portable.jsonl"
            target_snapshot = root / "target.jsonl"
            source_thread_id = "01900000-0000-7000-8000-000000000123"
            turn_id = "01900000-0000-7000-8000-000000000456"
            rows = base_rows(thread_id=source_thread_id)
            rows[1]["payload"]["turn_id"] = turn_id  # type: ignore[index]
            rows.extend(
                [
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_started",
                            "turn_id": turn_id,
                            "model_context_window": 258400,
                        },
                    },
                    response(
                        {
                            "type": "message",
                            "role": "user",
                            "internal_chat_message_metadata_passthrough": {
                                "turn_id": turn_id
                            },
                            "content": [
                                {"type": "input_text", "text": "portable hello"}
                            ],
                        }
                    ),
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "portable hello",
                            "images": [],
                            "local_images": [],
                        },
                    },
                    response(
                        {
                            "type": "message",
                            "role": "assistant",
                            "phase": "final_answer",
                            "internal_chat_message_metadata_passthrough": {
                                "turn_id": turn_id
                            },
                            "content": [
                                {"type": "output_text", "text": "portable answer"}
                            ],
                        }
                    ),
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "portable answer",
                            "phase": "final_answer",
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": turn_id,
                        },
                    },
                ]
            )
            write_rollout(source, rows)
            source_before = source.read_bytes()
            snapshot = sanitize_rollout(
                source,
                portable,
                expected_thread_id=source_thread_id,
            )
            materialize_snapshot_for_target(
                portable,
                target_snapshot,
                model_provider="openai",
                model="gpt-test",
                cwd=root,
            )
            capability = detect_fork_path_capability(
                codex_home=target_home,
                timeout=30,
            )
            if not capability.available:
                self.skipTest(capability.detail)

            client = CodexAppServer(target_home, timeout=30)
            clone_id: str | None = None
            try:
                client.start()
                thread = client.fork_path(
                    source_thread_id=PORTABLE_THREAD_ID,
                    rollout_path=target_snapshot,
                    model_provider="openai",
                    cwd=root,
                    model="gpt-test",
                )
                clone_id = str(thread["id"])
                self.assertNotEqual(clone_id, source_thread_id)
                self.assertNotEqual(clone_id, PORTABLE_THREAD_ID)
                client.set_thread_name(clone_id, "Portable integration [shared]")
                verified = client.read_thread(clone_id, include_turns=True)
                self.assertEqual(verified["id"], clone_id)
                self.assertEqual(verified["modelProvider"], "openai")
                self.assertEqual(
                    Path(verified["cwd"]).resolve(),
                    root.resolve(),
                )
                self.assertEqual(
                    verified["name"],
                    "Portable integration [shared]",
                )
                self.assertEqual(len(verified["turns"]), 1)
                visible_items = verified["turns"][0]["items"]
                self.assertTrue(
                    any(item.get("type") == "userMessage" for item in visible_items)
                )
                self.assertTrue(
                    any(item.get("type") == "agentMessage" for item in visible_items)
                )
                rollout = Path(verified["path"]).resolve()
                self.assertTrue(
                    rollout.is_relative_to((target_home / "sessions").resolve())
                )
                audit_jsonl_no_encrypted_content(rollout)
                audit_target_runtime_context(
                    rollout,
                    expected_model_provider="openai",
                    expected_model="gpt-test",
                )
                rollout_rows = [
                    json.loads(line)
                    for line in rollout.read_text(encoding="utf-8").splitlines()
                ]
                turn_contexts = [
                    row["payload"]
                    for row in rollout_rows
                    if row["type"] == "turn_context"
                ]
                self.assertTrue(turn_contexts)
                self.assertEqual(
                    {context["model"] for context in turn_contexts},
                    {"gpt-test"},
                )
                listed_ids = {
                    str(item.get("id"))
                    for item in client.list_threads(limit=100)
                }
                self.assertIn(clone_id, listed_ids)
                self.assertEqual(source.read_bytes(), source_before)
                self.assertGreater(snapshot.kept_rows, 2)
            finally:
                if clone_id:
                    try:
                        client.delete_thread(clone_id)
                    except Exception:
                        pass
                client.close()


if __name__ == "__main__":
    unittest.main()
