from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_conversation_pool as pool_module
from codex_conversation_pool import (
    ConversationPool,
    LocalMappingStore,
    PORTABLE_MODEL_ID,
    PORTABLE_THREAD_ID,
    PoolConflictError,
    PoolIntegrityError,
    SnapshotChangedError,
    SnapshotCompatibilityError,
    audit_jsonl_no_encrypted_content,
    audit_target_runtime_context,
    materialize_snapshot_for_target,
    sanitize_rollout,
    semantic_snapshot_hash,
)


class FakeSecurity:
    def prepare(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)

    def verify(self, root: Path) -> dict[str, object]:
        if not root.is_dir():
            raise AssertionError("missing fake secure root")
        return {"efs": True, "aclProtected": True}

    def verify_encrypted_file(self, path: Path) -> None:
        if not path.is_file():
            raise AssertionError(f"missing fake encrypted file: {path}")


def response(payload: dict[str, object], timestamp: str = "2026-01-01T00:00:01Z") -> dict[str, object]:
    return {"timestamp": timestamp, "type": "response_item", "payload": payload}


def base_rows(*, thread_id: str = "01900000-0000-7000-8000-000000000001") -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": thread_id,
                "id": thread_id,
                "timestamp": "2026-01-01T00:00:00Z",
                "cwd": r"D:\work\demo",
                "originator": "codex_cli_rs",
                "cli_version": "0.144.6",
                "source": "appServer",
                "thread_source": "appServer",
                "model_provider": "old-provider",
                "base_instructions": {"text": "old instructions"},
                "dynamic_tools": [{"name": "old-plugin"}],
                "git": {
                    "commit_hash": "abc123",
                    "branch": "main",
                    "repository_url": "https://user:password@example.test/repo.git?token=bad",
                },
            },
        },
        {
            "timestamp": "2026-01-01T00:00:00Z",
            "type": "turn_context",
            "payload": {
                "approval_policy": "never",
                "sandbox_policy": {"type": "danger"},
                "skills": ["old"],
            },
        },
    ]


def write_rollout(path: Path, rows: list[dict[str, object]], *, final_newline: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        for row in rows
    )
    if final_newline:
        text += "\n"
    path.write_text(text, encoding="utf-8")


class SnapshotTests(unittest.TestCase):
    def test_tool_search_call_and_output_are_preserved_as_a_pair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            destination = root / "portable.jsonl"
            rows = base_rows()
            rows.extend(
                [
                    response(
                        {
                            "type": "tool_search_call",
                            "id": "tool-search-call",
                            "call_id": "call-tool-search",
                            "status": "completed",
                            "arguments": {"query": "available tools"},
                        }
                    ),
                    response(
                        {
                            "type": "tool_search_output",
                            "id": "tool-search-output",
                            "call_id": "call-tool-search",
                            "status": "completed",
                            "tools": [{"type": "namespace", "name": "example"}],
                        }
                    ),
                ]
            )
            write_rollout(source, rows)

            sanitize_rollout(source, destination)

            payloads = [
                row["payload"]
                for row in (
                    json.loads(line)
                    for line in destination.read_text(encoding="utf-8").splitlines()
                )
                if row["type"] == "response_item"
            ]
            self.assertEqual(
                [payload["type"] for payload in payloads],
                ["tool_search_call", "tool_search_output"],
            )
            self.assertTrue(all("id" not in payload for payload in payloads))

    def test_portable_snapshot_keeps_visible_history_tools_images_and_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            destination = root / "portable.jsonl"
            rows = base_rows()
            rows.extend(
                [
                    response(
                        {
                            "type": "message",
                            "role": "developer",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "<app-context>old injected context</app-context>",
                                }
                            ],
                        }
                    ),
                    response(
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_text",
                                    "text": "<environment_context>old environment</environment_context>",
                                }
                            ],
                        }
                    ),
                    response(
                        {
                            "type": "message",
                            "role": "user",
                            "id": "msg_old",
                            "internal_chat_message_metadata_passthrough": {
                                "turn_id": "turn-visible",
                                "opaque_provider_data": "drop me",
                            },
                            "content": [
                                {"type": "input_text", "text": "please inspect this image"},
                                {
                                    "type": "input_image",
                                    "image_url": "data:image/png;base64,iVBORw0KGgo=",
                                },
                            ],
                        }
                    ),
                    response(
                        {
                            "type": "reasoning",
                            "id": "reason_old",
                            "encrypted_content": "ciphertext",
                            "summary": [{"type": "summary_text", "text": "hidden"}],
                        }
                    ),
                    response(
                        {
                            "type": "function_call",
                            "id": "fc_old",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": '{"path":"README.md","api_key":"sk-supersecretvalue"}',
                        }
                    ),
                    response(
                        {
                            "type": "function_call_output",
                            "call_id": "call_1",
                            "output": '{"ok":true,"access_token":"secret-token-value"}',
                        }
                    ),
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "type": "compacted",
                        "payload": {
                            "message": "Conversation summary",
                            "replacement_history": [
                                {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {"type": "input_text", "text": "earlier request"}
                                    ],
                                },
                                {
                                    "type": "reasoning",
                                    "encrypted_content": "nested cipher",
                                    "summary": [],
                                },
                                {
                                    "type": "compaction",
                                    "encrypted_content": "nested compaction cipher",
                                },
                                {
                                    "type": "custom_tool_call",
                                    "call_id": "custom_1",
                                    "name": "lookup",
                                    "input": "{}",
                                },
                                {
                                    "type": "custom_tool_call_output",
                                    "call_id": "custom_1",
                                    "output": "done",
                                },
                            ],
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:03Z",
                        "type": "event_msg",
                        "payload": {"type": "token_count", "usage": {"total": 123}},
                    },
                ]
            )
            write_rollout(source, rows)

            result = sanitize_rollout(source, destination)

            self.assertEqual(result.source_model_provider, "old-provider")
            self.assertGreater(result.dropped_rows, 3)
            output = [
                json.loads(line)
                for line in destination.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [row["type"] for row in output],
                [
                    "session_meta",
                    "response_item",
                    "response_item",
                    "response_item",
                    "compacted",
                ],
            )
            metadata = output[0]["payload"]
            self.assertNotIn("base_instructions", metadata)
            self.assertNotIn("dynamic_tools", metadata)
            self.assertEqual(
                metadata["git"]["repository_url"],
                "https://example.test/repo.git",
            )
            user_message = output[1]["payload"]
            self.assertNotIn("id", user_message)
            self.assertEqual(
                user_message["internal_chat_message_metadata_passthrough"],
                {"turn_id": "turn-visible"},
            )
            self.assertEqual(
                user_message["content"][1]["image_url"],
                "data:image/png;base64,iVBORw0KGgo=",
            )
            self.assertIn("[REDACTED]", output[2]["payload"]["arguments"])
            self.assertIn("[REDACTED]", output[3]["payload"]["output"])
            replacement = output[4]["payload"]["replacement_history"]
            self.assertEqual(
                [item["type"] for item in replacement],
                ["message", "custom_tool_call", "custom_tool_call_output"],
            )
            audit_jsonl_no_encrypted_content(destination)
            self.assertNotIn(
                "encrypted_content",
                destination.read_text(encoding="utf-8"),
            )

    def test_replay_events_are_kept_but_runtime_and_reasoning_events_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            destination = root / "portable.jsonl"
            turn_id = "01900000-0000-7000-8000-000000000010"
            rows = base_rows()
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
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "visible request",
                            "images": ["data:image/png;base64,AA=="],
                            "access_token": "secret",
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "visible answer",
                            "phase": "final_answer",
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:04Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_reasoning",
                            "text": "hidden",
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:05Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "thread_settings_applied",
                            "thread_settings": {
                                "approval_policy": "never",
                                "developer_instructions": "old",
                            },
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:06Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "turn_complete",
                            "turn_id": turn_id,
                        },
                    },
                ]
            )
            write_rollout(source, rows)

            sanitize_rollout(source, destination)

            events = [
                json.loads(line)["payload"]
                for line in destination.read_text(encoding="utf-8").splitlines()
                if json.loads(line)["type"] == "event_msg"
            ]
            self.assertEqual(
                [event["type"] for event in events],
                ["task_started", "user_message", "agent_message", "turn_complete"],
            )
            self.assertEqual(events[1]["access_token"], "[REDACTED]")
            self.assertNotIn("hidden", destination.read_text(encoding="utf-8"))

    def test_target_materialization_replaces_portable_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            portable = root / "portable.jsonl"
            target = root / "target.jsonl"
            rows = base_rows()
            rows[1]["payload"]["turn_id"] = "turn-one"  # type: ignore[index]
            rows[1]["payload"]["model"] = "old-profile-model"  # type: ignore[index]
            rows.append(
                {
                    "timestamp": "2026-01-01T00:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-one",
                    },
                }
            )
            write_rollout(source, rows)

            sanitize_rollout(source, portable)
            portable_before = portable.read_bytes()
            portable_rows = [
                json.loads(line)
                for line in portable.read_text(encoding="utf-8").splitlines()
            ]
            portable_context = next(
                row["payload"]
                for row in portable_rows
                if row["type"] == "turn_context"
            )
            self.assertNotIn("model", portable_context)

            materialize_snapshot_for_target(
                portable,
                target,
                model_provider="apicodex",
                model="gpt-target",
                cwd=root,
            )
            target_rows = [
                json.loads(line)
                for line in target.read_text(encoding="utf-8").splitlines()
            ]
            target_metadata = next(
                row["payload"]
                for row in target_rows
                if row["type"] == "session_meta"
            )
            target_context = next(
                row["payload"]
                for row in target_rows
                if row["type"] == "turn_context"
            )
            self.assertEqual(target_metadata["model_provider"], "apicodex")
            self.assertEqual(target_metadata["cwd"], str(root.resolve()))
            self.assertEqual(target_context["model"], "gpt-target")
            self.assertEqual(target_context["cwd"], str(root.resolve()))
            self.assertEqual(portable.read_bytes(), portable_before)
            audit_target_runtime_context(
                target,
                expected_model_provider="apicodex",
                expected_model="gpt-target",
            )

            target_context["model"] = PORTABLE_MODEL_ID
            write_rollout(root / "bad-target.jsonl", target_rows)
            with self.assertRaisesRegex(PoolIntegrityError, "portable model leaked"):
                audit_target_runtime_context(
                    root / "bad-target.jsonl",
                    expected_model_provider="apicodex",
                    expected_model="gpt-target",
                )

    def test_unknown_response_type_is_rejected_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            destination = root / "portable.jsonl"
            write_rollout(
                source,
                base_rows()
                + [
                    response(
                        {
                            "type": "future_opaque_item",
                            "content": "must not be silently lost",
                        }
                    )
                ],
            )
            with self.assertRaises(SnapshotCompatibilityError):
                sanitize_rollout(source, destination)
            self.assertFalse(destination.exists())

    def test_identical_session_metadata_is_deduplicated_but_conflicts_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            duplicate = base_rows()[0]
            write_rollout(source, base_rows() + [duplicate])
            result = sanitize_rollout(source, root / "portable.jsonl")
            self.assertTrue(
                any("deduplicated repeated session_meta" in item for item in result.warnings)
            )

            conflict = json.loads(json.dumps(duplicate))
            conflict["payload"]["id"] = (
                "01900000-0000-7000-8000-000000000099"
            )
            conflict["payload"]["session_id"] = (
                "01900000-0000-7000-8000-000000000099"
            )
            conflict_source = root / "conflict.jsonl"
            write_rollout(conflict_source, base_rows() + [conflict])
            with self.assertRaisesRegex(
                SnapshotCompatibilityError,
                "conflicting duplicate session_meta",
            ):
                sanitize_rollout(
                    conflict_source,
                    root / "conflicting-portable.jsonl",
                )

    def test_fork_target_metadata_supersedes_portable_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_id = "01900000-0000-7000-8000-000000000077"
            target_rows = base_rows(thread_id=target_id)
            portable_metadata = base_rows(thread_id=PORTABLE_THREAD_ID)[0]
            source = root / "forked.jsonl"
            write_rollout(
                source,
                [target_rows[0], portable_metadata, target_rows[1]],
            )

            result = sanitize_rollout(
                source,
                root / "forked-portable.jsonl",
                expected_thread_id=target_id,
            )

            self.assertEqual(result.source_thread_id, target_id)
            self.assertTrue(
                any(
                    "portable fork session_meta" in warning
                    for warning in result.warnings
                )
            )

    def test_orphan_tool_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            write_rollout(
                source,
                base_rows()
                + [
                    response(
                        {
                            "type": "function_call_output",
                            "call_id": "missing",
                            "output": "result",
                        }
                    )
                ],
            )
            with self.assertRaisesRegex(
                SnapshotCompatibilityError,
                "orphan function_call_output",
            ):
                sanitize_rollout(source, root / "portable.jsonl")

    def test_partial_last_line_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            write_rollout(source, base_rows(), final_newline=False)
            with self.assertRaises(SnapshotChangedError):
                sanitize_rollout(source, root / "portable.jsonl")

    def test_source_identity_change_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            write_rollout(source, base_rows())
            original = pool_module._file_identity(source)
            changed = pool_module.FileIdentity(
                original.device,
                original.inode,
                original.size + 1,
                original.modified_ns + 1,
            )
            with patch.object(
                pool_module,
                "_file_identity",
                side_effect=[original, changed],
            ):
                with self.assertRaises(SnapshotChangedError):
                    sanitize_rollout(source, root / "portable.jsonl")

    def test_semantic_hash_ignores_fork_generated_bookkeeping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = root / "left.jsonl"
            right = root / "right.jsonl"
            write_rollout(
                left,
                [
                    {
                        "timestamp": "2026-01-01T00:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": PORTABLE_THREAD_ID,
                            "cwd": r"D:\work\demo",
                            "git": {"branch": "main", "commit_hash": "abc123"},
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "hello",
                            "audio": [],
                            "local_audio": [],
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-1",
                            "started_at": "2026-01-01T00:00:01Z",
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "web_search_end",
                            "call_id": "search-1",
                            "query": "portable context",
                            "results": [{"title": "Fork-only replay detail"}],
                        },
                    },
                    {
                        "timestamp": "2026-01-01T00:00:04Z",
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "turn-1",
                            "cwd": "C:\\",
                            "model": PORTABLE_MODEL_ID,
                        },
                    },
                ],
            )
            write_rollout(
                right,
                [
                    {
                        "timestamp": "2026-02-02T00:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": PORTABLE_THREAD_ID,
                            "cwd": r"D:\work\demo",
                        },
                    },
                    {
                        "timestamp": "2026-02-02T00:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "hello",
                        },
                    },
                    {
                        "timestamp": "2026-02-02T00:00:02Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "task_complete",
                            "turn_id": "turn-1",
                        },
                    },
                    {
                        "timestamp": "2026-02-02T00:00:03Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "web_search_end",
                            "call_id": "search-1",
                            "query": "portable context",
                        },
                    },
                    {
                        "timestamp": "2026-02-02T00:00:04Z",
                        "type": "turn_context",
                        "payload": {
                            "turn_id": "turn-1",
                            "cwd": r"D:\work\demo",
                            "model": "gpt-target",
                        },
                    },
                ],
            )
            self.assertNotEqual(left.read_bytes(), right.read_bytes())
            self.assertEqual(
                semantic_snapshot_hash(left),
                semantic_snapshot_hash(right),
            )

    def test_non_terminal_last_turn_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.jsonl"
            rows = base_rows()
            rows[1]["payload"]["turn_id"] = "turn-active"  # type: ignore[index]
            rows.append(
                response(
                    {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "still working"}],
                    }
                )
            )
            write_rollout(source, rows)
            with self.assertRaisesRegex(
                SnapshotChangedError,
                "has no terminal event",
            ):
                sanitize_rollout(source, root / "portable.jsonl")


class StorageTests(unittest.TestCase):
    def make_snapshot(
        self,
        root: Path,
        *,
        thread_id: str,
        message: str,
        suffix: str,
    ):
        source = root / f"source-{suffix}.jsonl"
        write_rollout(
            source,
            base_rows(thread_id=thread_id)
            + [
                response(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": message}],
                    }
                )
            ],
        )
        return sanitize_rollout(source, root / f"snapshot-{suffix}.jsonl")

    def test_publish_resolve_dedupe_fast_forward_and_branch_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            security = FakeSecurity()
            pool_root = root / "pool"
            initialized = ConversationPool.initialize(pool_root, security=security)
            pool = ConversationPool(pool_root, security=security)
            first = self.make_snapshot(
                root,
                thread_id="01900000-0000-7000-8000-000000000001",
                message="first",
                suffix="one",
            )
            initial = pool.publish(
                "demo",
                first,
                title="Demo",
                source_target="api:one",
            )
            self.assertEqual(initial.ref_name, "main")
            self.assertEqual(initial.source_thread_id, PORTABLE_THREAD_ID)
            self.assertEqual(initial.source_target, "local-portable-snapshot")
            self.assertEqual(
                pool.resolve("demo").snapshot_hash,
                first.sha256,
            )
            self.assertEqual(initialized["poolId"], pool.pool_id())
            equivalent_rows = base_rows(
                thread_id="01900000-0000-7000-8000-000000000001"
            )
            equivalent_meta = equivalent_rows[0]["payload"]
            assert isinstance(equivalent_meta, dict)
            equivalent_meta.pop("git", None)
            equivalent_rows.append(
                response(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first"}],
                    },
                    timestamp="2026-02-02T00:00:01Z",
                )
            )
            equivalent_source = root / "source-equivalent.jsonl"
            write_rollout(equivalent_source, equivalent_rows)
            equivalent = sanitize_rollout(
                equivalent_source,
                root / "snapshot-equivalent.jsonl",
            )
            unchanged = pool.push(
                lineage_id=initial.lineage_id,
                ref_name="main",
                base_commit=initial.id,
                snapshot=equivalent,
                source_target="api:one",
                title="Demo",
            )
            self.assertEqual(unchanged.id, initial.id)
            self.assertEqual(len(pool.log("demo")), 1)
            with self.assertRaises(PoolConflictError):
                pool.publish(
                    "DEMO",
                    first,
                    title="Duplicate",
                    source_target="api:one",
                )

            second = self.make_snapshot(
                root,
                thread_id=first.source_thread_id,
                message="second",
                suffix="two",
            )
            pushed = pool.push(
                lineage_id=initial.lineage_id,
                ref_name="main",
                base_commit=initial.id,
                snapshot=second,
                source_target="api:one",
                title="Demo",
            )
            self.assertEqual(pool.resolve("demo").id, pushed.id)
            with self.assertRaises(PoolConflictError):
                pool.push(
                    lineage_id=initial.lineage_id,
                    ref_name="main",
                    base_commit=initial.id,
                    snapshot=first,
                    source_target="api:two",
                    title="Demo",
                )
            branch = pool.push(
                lineage_id=initial.lineage_id,
                ref_name="main",
                base_commit=initial.id,
                snapshot=first,
                source_target="api:two",
                title="Demo",
                new_branch="alternate",
            )
            self.assertEqual(
                pool.resolve("demo", ref_name="alternate").id,
                branch.id,
            )
            self.assertEqual(len(pool.log("demo")), 2)
            self.assertEqual(pool.doctor()["objectsVerified"], 2)

    def test_dry_run_does_not_write_object_or_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            security = FakeSecurity()
            pool_root = root / "pool"
            ConversationPool.initialize(pool_root, security=security)
            pool = ConversationPool(pool_root, security=security)
            snapshot = self.make_snapshot(
                root,
                thread_id="01900000-0000-7000-8000-000000000010",
                message="dry",
                suffix="dry",
            )
            preview = pool.publish(
                "dry-demo",
                snapshot,
                title="Dry",
                source_target="account",
                dry_run=True,
            )
            self.assertFalse(pool.object_path(snapshot.sha256).exists())
            self.assertEqual(preview.snapshot_hash, snapshot.sha256)
            self.assertEqual(pool.list_lineages(), [])

    def test_hash_tamper_and_path_traversal_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            security = FakeSecurity()
            pool_root = root / "pool"
            ConversationPool.initialize(pool_root, security=security)
            pool = ConversationPool(pool_root, security=security)
            snapshot = self.make_snapshot(
                root,
                thread_id="01900000-0000-7000-8000-000000000011",
                message="tamper",
                suffix="tamper",
            )
            pool.publish(
                "tamper-demo",
                snapshot,
                title="Tamper",
                source_target="account",
            )
            object_path = pool.object_path(snapshot.sha256)
            object_path.write_bytes(object_path.read_bytes() + b" ")
            with self.assertRaises(PoolIntegrityError):
                pool.verify_object(snapshot.sha256)
            with self.assertRaises(PoolIntegrityError):
                pool.object_path("../escape")

    def test_local_mapping_is_private_and_scoped_by_target_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LocalMappingStore(root / "state")
            record = store.register(
                pool_id="pool-id",
                target_id="api:one",
                target_home=root / "profile-one",
                thread_id="thread-one",
                lineage_id="lineage",
                lineage_name="demo",
                ref_name="main",
                base_commit="commit",
                rollout_path=root / "rollout.jsonl",
            )
            found = store.find(
                pool_id="pool-id",
                target_home=root / "profile-one",
                thread_id="thread-one",
            )
            self.assertEqual(found, [record])
            self.assertEqual(
                store.find(
                    pool_id="pool-id",
                    target_home=root / "profile-two",
                ),
                [],
            )

    def test_default_pool_path_follows_current_user_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            store = LocalMappingStore(root / "state")
            with patch.object(pool_module.Path, "home", return_value=home):
                self.assertEqual(
                    store.get_pool_path(),
                    (home / "CodexConversationPool").resolve(),
                )

    def test_security_failure_never_creates_plaintext_pool_metadata(self) -> None:
        class FailingSecurity(FakeSecurity):
            def prepare(self, root: Path) -> None:
                root.mkdir(parents=True, exist_ok=True)
                raise pool_module.PoolSecurityError("EFS unavailable")

        class FailingVerificationSecurity(FakeSecurity):
            def verify_encrypted_file(self, path: Path) -> None:
                raise pool_module.PoolSecurityError(
                    f"EFS verification failed: {path.name}"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool_root = root / "prepare-failure"
            with self.assertRaisesRegex(
                pool_module.PoolSecurityError,
                "EFS unavailable",
            ):
                ConversationPool.initialize(
                    pool_root,
                    security=FailingSecurity(),
                )
            self.assertFalse((pool_root / "pool.json").exists())
            self.assertFalse((pool_root / "pool.sqlite3").exists())
            self.assertFalse((pool_root / "objects").exists())

            pool_root = root / "verification-failure"
            with self.assertRaisesRegex(
                pool_module.PoolSecurityError,
                "EFS verification failed",
            ):
                ConversationPool.initialize(
                    pool_root,
                    security=FailingVerificationSecurity(),
                )
            self.assertFalse((pool_root / "pool.json").exists())
            self.assertFalse((pool_root / "pool.sqlite3").exists())
            self.assertFalse((pool_root / "objects").exists())


if __name__ == "__main__":
    unittest.main()
