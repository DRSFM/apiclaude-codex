from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_app_server import CodexAppServer, detect_fork_path_capability, resolve_share_codex_command
from codex_conversation_pool import (
    PoolIntegrityError, audit_paginated_replay, materialize_snapshot_for_target,
    paginated_replay_items, sanitize_rollout,
    semantic_snapshot_hash,
)
from tests.test_codex_conversation_pool import base_rows, response, write_rollout


def modern_rows(root: Path):
    rows = base_rows()
    tid = rows[0]["payload"]["id"]
    turn = "01900000-0000-7000-8000-000000000123"
    rows[0]["payload"]["history_mode"] = "paginated"
    rows[1]["payload"]["turn_id"] = turn
    rows.insert(1, {"timestamp": "2026-01-01T00:00:01Z", "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn, "model_context_window": 258400}})
    items = [
        {"type": "UserMessage", "id": "user-1", "content": [{"type": "text", "text": "完整问题", "text_elements": []}]},
        {"type": "AgentMessage", "id": "agent-1", "content": [{"type": "Text", "text": "完整回答"}], "phase": "final_answer"},
        {"type": "CommandExecution", "id": "cmd-1", "process_id": None, "command": ["echo", "ok"],
         "cwd": root.as_uri(), "parsed_cmd": [], "source": "agent", "status": "completed", "stdout": "ok",
         "stderr": "", "aggregated_output": "ok", "exit_code": 0, "duration": {"secs": 0, "nanos": 1}, "formatted_output": "ok"},
        {"type": "ImageView", "id": "image-1", "path": (root / "sample.png").as_uri()},
    ]
    for item in items:
        rows.append({"timestamp": "2026-01-01T00:00:02Z", "type": "event_msg", "payload": {
            "type": "item_completed", "thread_id": tid, "turn_id": turn, "item": item,
            "started_at_ms": 1767225602000, "completed_at_ms": 1767225602001}})
    rows.extend([
        response({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "完整问题"}]}),
        response({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "完整回答"}]}),
        {"timestamp": "2026-01-01T00:00:03Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn}},
    ])
    return rows


class PaginatedImportTests(unittest.TestCase):
    def test_runtime_resolution_avoids_stale_path_custom_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = root / "old.exe"
            current = root / "OpenAI" / "Codex" / "bin" / "build-hash" / "codex.exe"
            current.parent.mkdir(parents=True)
            old.touch()
            current.touch()
            def version(args, **kwargs):
                return subprocess.CompletedProcess(args, 0, "codex-cli " + ("0.153.3" if args[0] == str(current) else "0.147.0"))
            with patch.dict("os.environ", {"LOCALAPPDATA": str(root)}), patch("codex_app_server.shutil.which", return_value=str(old)), patch("codex_app_server.subprocess.run", side_effect=version):
                self.assertEqual(resolve_share_codex_command(), str(current))

    def test_materialization_is_exclusive_and_preserves_event_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, portable, target = (root / name for name in ("source.jsonl", "portable.jsonl", "target.jsonl"))
            write_rollout(source, modern_rows(root))
            before = source.read_bytes()
            sanitize_rollout(source, portable)
            materialize_snapshot_for_target(portable, target, model_provider="openai", model="target-model", cwd=root,
                                            imported_thread_id="01900000-0000-7000-8000-000000000999")
            rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["ordinal"] for r in rows], list(range(len(rows))))
            self.assertEqual(rows[0]["payload"]["history_mode"], "paginated")
            self.assertNotIn("history_base", rows[0]["payload"])
            self.assertEqual([e["item"] for e in paginated_replay_items(portable)], [e["item"] for e in paginated_replay_items(target)])
            with self.assertRaises(PoolIntegrityError):
                audit_paginated_replay(portable, target, {"turns": []})
            with self.assertRaises(Exception):
                materialize_snapshot_for_target(portable, target, model_provider="openai", model="target-model", cwd=root)
            self.assertEqual(source.read_bytes(), before)
            recleaned = root / "recleaned.jsonl"
            sanitize_rollout(target, recleaned)
            self.assertEqual(semantic_snapshot_hash(portable), semantic_snapshot_hash(recleaned))

    def test_native_resume_survives_restart_and_preserves_all_display_items(self):
        exe = resolve_share_codex_command()
        with tempfile.TemporaryDirectory(prefix="apicodex-native-test-", ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            home = root / "home"
            home.mkdir()
            capability = detect_fork_path_capability(codex_command=exe, codex_home=home)
            if not capability.paginated_history:
                self.skipTest("installed Codex does not support paginated history")
            source, portable = root / "source.jsonl", root / "portable.jsonl"
            write_rollout(source, modern_rows(root))
            sanitize_rollout(source, portable)
            tid = "01900000-0000-7000-8000-000000000999"
            target = home / "sessions" / f"rollout-2026-01-01T00-00-00-{tid}.jsonl"
            materialize_snapshot_for_target(portable, target, model_provider="openai", model="gpt-test", cwd=root, imported_thread_id=tid)
            with CodexAppServer(home, codex_command=exe) as client:
                client.resume_import(thread_id=tid, rollout_path=target, model_provider="openai", model="gpt-test", cwd=root)
                client.set_thread_name(tid, "Native import test")
                thread = client.read_thread(tid, include_turns=True)
                self.assertEqual(audit_paginated_replay(portable, target, thread), 4)
            with CodexAppServer(home, codex_command=exe) as client:
                thread = client.read_thread(tid, include_turns=True)
                self.assertEqual(audit_paginated_replay(portable, target, thread), 4)
                items = thread["turns"][0]["items"]
                self.assertEqual({i["type"] for i in items}, {"userMessage", "agentMessage", "commandExecution", "imageView"})
                self.assertEqual(next(i["text"] for i in items if i["type"] == "agentMessage"), "完整回答")


if __name__ == "__main__":
    unittest.main()
