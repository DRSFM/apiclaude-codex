from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
import codex_share_cli
from codex_app_server import AppServerCapability, AppServerError
from codex_conversation_pool import ConversationPool
from codex_share_cli import (
    ShareContext,
    copy_share_thread,
    list_share_targets,
    list_share_threads,
    main,
)
from tests.test_codex_conversation_pool import (
    FakeSecurity,
    base_rows,
    response,
    write_rollout,
)


class FakeAppServer:
    threads_by_home: dict[str, dict[str, dict[str, object]]] = {}
    next_id = 1

    def __init__(self, codex_home: Path, **_: object) -> None:
        self.codex_home = codex_home.resolve()
        self.threads = self.threads_by_home.setdefault(str(self.codex_home), {})

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def list_threads(self, *, limit: int = 100):
        return list(self.threads.values())[:limit]

    def read_thread(self, thread_id: str, *, include_turns: bool = False):
        del include_turns
        return dict(self.threads[thread_id])

    def fork_path(
        self,
        *,
        source_thread_id: str,
        rollout_path: Path,
        model_provider: str,
        cwd: Path,
        model: str | None = None,
    ):
        del source_thread_id, model
        thread_id = f"01900000-0000-7000-8000-{self.next_id:012d}"
        type(self).next_id += 1
        target = (
            self.codex_home
            / "sessions"
            / "2026"
            / "01"
            / "01"
            / f"rollout-{thread_id}.jsonl"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rollout_path, target)
        lines = target.read_text(encoding="utf-8").splitlines()
        metadata = json.loads(lines[0])
        metadata["payload"]["id"] = thread_id
        metadata["payload"]["session_id"] = thread_id
        lines[0] = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        thread = {
            "id": thread_id,
            "path": str(target),
            "cwd": str(cwd.resolve()),
            "modelProvider": model_provider,
            "name": None,
            "preview": "Portable clone",
            "status": {"type": "idle"},
        }
        self.threads[thread_id] = thread
        return dict(thread)

    def set_thread_name(self, thread_id: str, name: str) -> None:
        self.threads[thread_id]["name"] = name

    def delete_thread(self, thread_id: str) -> None:
        thread = self.threads.pop(thread_id)
        Path(str(thread["path"])).unlink(missing_ok=True)


def run_cli(arguments: list[str], context: ShareContext):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = main(arguments, context)
    payload = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else None
    return code, payload, stderr.getvalue()


class ShareCliTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeAppServer.threads_by_home = {}
        FakeAppServer.next_id = 1

    def make_context(self, root: Path) -> tuple[ShareContext, Path, Path, str]:
        account_home = root / "account"
        api_root = root / "api"
        profile_home = api_root / "profiles" / "relay"
        account_home.mkdir()
        profile_home.mkdir(parents=True)
        (profile_home / "config.toml").write_text(
            'model = "gpt-test"\nmodel_provider = "apicodex"\n',
            encoding="utf-8",
        )
        thread_id = "01900000-0000-7000-8000-000000000099"
        rollout = (
            profile_home
            / "sessions"
            / "2026"
            / "01"
            / "01"
            / f"rollout-{thread_id}.jsonl"
        )
        rows = base_rows(thread_id=thread_id)
        rows[1]["payload"]["turn_id"] = "turn-one"  # type: ignore[index]
        rows.extend(
            [
                response(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "portable request"}],
                    }
                ),
                response(
                    {
                        "type": "message",
                        "role": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "output_text", "text": "portable answer"}],
                    }
                ),
                {
                    "timestamp": "2026-01-01T00:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-one",
                    },
                },
            ]
        )
        write_rollout(rollout, rows)
        FakeAppServer.threads_by_home[str(profile_home.resolve())] = {
            thread_id: {
                "id": thread_id,
                "path": str(rollout),
                "cwd": str(root.resolve()),
                "modelProvider": "apicodex",
                "name": "Source title",
                "preview": "portable request",
                "status": {"type": "idle"},
            }
        }
        context = ShareContext(
            account_home=account_home,
            api_root=api_root,
            local_state_root=root / "state",
            load_api_profiles=lambda: [
                {
                    "id": "relay",
                    "name": "Relay",
                    "home": "profiles/relay",
                    "model": "gpt-test",
                }
            ],
            pool_security=FakeSecurity(),
            app_server_factory=FakeAppServer,
        )
        return context, profile_home, rollout, thread_id

    def test_init_publish_list_log_clone_and_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, profile_home, _, thread_id = self.make_context(root)
            pool_root = root / "pool"

            code, payload, _ = run_cli(
                ["init", "--pool", str(pool_root), "--json"],
                context,
            )
            self.assertEqual(code, 0)
            self.assertTrue(payload["created"])

            code, published, stderr = run_cli(
                [
                    "publish",
                    "demo",
                    "--api-profile",
                    "relay",
                    "--thread",
                    thread_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(published["commit"]["lineage_name"], "demo")
            self.assertEqual(published["snapshot"]["warnings"], [])

            code, listed, _ = run_cli(["list", "--json"], context)
            self.assertEqual(code, 0)
            self.assertEqual(listed["conversations"][0]["name"], "demo")
            code, logged, _ = run_cli(["log", "demo", "--json"], context)
            self.assertEqual(code, 0)
            self.assertEqual(len(logged["commits"]), 1)

            capability = AppServerCapability(True, "codex-cli test", "supported")
            with patch.object(
                codex_share_cli,
                "detect_fork_path_capability",
                return_value=capability,
            ):
                code, cloned, stderr = run_cli(
                    [
                        "clone",
                        "demo",
                        "--api-profile",
                        "relay",
                        "--cwd",
                        str(root),
                        "--json",
                    ],
                    context,
                )
            self.assertEqual(code, 0, stderr)
            clone_id = cloned["clone"]["threadId"]
            self.assertNotEqual(clone_id, thread_id)
            self.assertEqual(cloned["clone"]["title"], "Source title [shared]")
            self.assertEqual(cloned["clone"]["model"], "gpt-test")

            clone_rollout = Path(
                FakeAppServer.threads_by_home[str(profile_home.resolve())][clone_id][
                    "path"
                ]
            )
            clone_rows = [
                json.loads(line)
                for line in clone_rollout.read_text(encoding="utf-8").splitlines()
            ]
            clone_contexts = [
                row["payload"]
                for row in clone_rows
                if row["type"] == "turn_context"
            ]
            self.assertTrue(clone_contexts)
            self.assertEqual(
                {context_row["model"] for context_row in clone_contexts},
                {"gpt-test"},
            )
            self.assertEqual(
                {context_row["cwd"] for context_row in clone_contexts},
                {str(root.resolve())},
            )

            code, status, stderr = run_cli(
                [
                    "status",
                    "--api-profile",
                    "relay",
                    "--thread",
                    clone_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(status["items"][0]["status"], "clean")

            self.assertTrue(
                Path(status["items"][0]["snapshotHash"]).__str__()
            )

            with clone_rollout.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        response(
                            {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "clone update"}
                                ],
                            }
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            code, status, stderr = run_cli(
                [
                    "status",
                    "--api-profile",
                    "relay",
                    "--thread",
                    clone_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(status["items"][0]["status"], "ahead")
            code, pushed, stderr = run_cli(
                [
                    "push",
                    "--api-profile",
                    "relay",
                    "--thread",
                    clone_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(pushed["commit"]["ref_name"], "main")

            source_rollout = Path(
                FakeAppServer.threads_by_home[str(profile_home.resolve())][thread_id][
                    "path"
                ]
            )
            with source_rollout.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(
                    json.dumps(
                        response(
                            {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {"type": "input_text", "text": "source fork update"}
                                ],
                            }
                        ),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    + "\n"
                )
            code, status, stderr = run_cli(
                [
                    "status",
                    "--api-profile",
                    "relay",
                    "--thread",
                    thread_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(status["items"][0]["status"], "diverged")
            code, failed_push, _ = run_cli(
                [
                    "push",
                    "--api-profile",
                    "relay",
                    "--thread",
                    thread_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 1)
            self.assertFalse(failed_push["ok"])
            code, branched, stderr = run_cli(
                [
                    "push",
                    "--api-profile",
                    "relay",
                    "--thread",
                    thread_id,
                    "--new-branch",
                    "source-work",
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertEqual(branched["commit"]["ref_name"], "source-work")

            pool = ConversationPool(pool_root, security=FakeSecurity())
            self.assertEqual(len(pool.list_lineages()), 1)
            self.assertEqual(len(pool.log("demo", ref_name="main")), 2)
            self.assertEqual(len(pool.log("demo", ref_name="source-work")), 2)
            self.assertTrue(
                (context.local_state_root / "mappings.sqlite3").is_file()
            )
            self.assertTrue(profile_home.is_dir())

    def test_service_lists_all_targets_and_copies_between_distinct_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, _, _, thread_id = self.make_context(root)
            pool_root = root / "pool"
            code, _, stderr = run_cli(
                ["init", "--pool", str(pool_root), "--json"],
                context,
            )
            self.assertEqual(code, 0, stderr)

            targets = list_share_targets(context)
            self.assertEqual(
                [target["id"] for target in targets],
                ["account", "api:relay"],
            )
            threads = list_share_threads(context, "api:relay")
            self.assertEqual(len(threads), 1)
            self.assertEqual(threads[0]["id"], thread_id)
            self.assertNotIn("path", threads[0])

            capability = AppServerCapability(True, "codex-cli test", "supported")
            with patch.object(
                codex_share_cli,
                "detect_fork_path_capability",
                return_value=capability,
            ):
                result = copy_share_thread(
                    context,
                    source_target_id="api:relay",
                    target_target_id="account",
                    thread_id=thread_id,
                    lineage_name="service-copy",
                    cwd=root,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["source"]["targetId"], "api:relay")
            self.assertEqual(result["target"]["targetId"], "account")
            self.assertNotEqual(result["source"]["threadId"], result["target"]["threadId"])
            self.assertNotIn("rollout", str(result).lower())
            self.assertNotIn("mapping", str(result).lower())

            with self.assertRaisesRegex(
                codex_share_cli.ConversationPoolError,
                "must be different",
            ):
                copy_share_thread(
                    context,
                    source_target_id="api:relay",
                    target_target_id="api:relay",
                    thread_id=thread_id,
                    lineage_name="invalid-copy",
                    cwd=root,
                )

    def test_service_round_trips_between_codex_and_claude_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, _, _, thread_id = self.make_context(root)
            context.claude_account_home = root / "claude-account"
            context.claude_nodes_root = root / "apiclaude"
            context.load_claude_nodes = lambda: {
                "relay-claude": {
                    "isolation": "isolated",
                    "home": "nodes/relay-claude",
                    "model": "claude-test",
                }
            }
            pool_root = root / "pool"
            code, _, stderr = run_cli(
                ["init", "--pool", str(pool_root), "--json"],
                context,
            )
            self.assertEqual(code, 0, stderr)

            targets = list_share_targets(context)
            self.assertEqual(
                [target["id"] for target in targets],
                ["account", "api:relay", "claude:relay-claude"],
            )
            claude_target = targets[-1]
            self.assertEqual(claude_target["kind"], "claude")
            self.assertEqual(claude_target["isolation"], "isolated")
            self.assertNotIn("credential", str(claude_target).lower())

            to_claude = copy_share_thread(
                context,
                source_target_id="api:relay",
                target_target_id="claude:relay-claude",
                thread_id=thread_id,
                lineage_name="codex-to-claude",
                cwd=root,
            )
            claude_id = to_claude["target"]["threadId"]
            self.assertNotEqual(claude_id, thread_id)
            self.assertEqual(
                to_claude["target"]["sessionId"],
                claude_id,
            )
            self.assertIn("--resume", to_claude["target"]["resumeCommand"])
            self.assertNotIn("path", str(to_claude).lower())
            self.assertNotIn("mapping", str(to_claude).lower())

            claude_threads = list_share_threads(
                context,
                "claude:relay-claude",
            )
            self.assertEqual(
                [thread["id"] for thread in claude_threads],
                [claude_id],
            )
            self.assertEqual(claude_threads[0]["title"], "[shared] Source title")
            self.assertEqual(claude_threads[0]["model"], "claude-test")

            capability = AppServerCapability(
                True,
                "codex-cli test",
                "supported",
            )
            with patch.object(
                codex_share_cli,
                "detect_fork_path_capability",
                return_value=capability,
            ):
                back_to_codex = copy_share_thread(
                    context,
                    source_target_id="claude:relay-claude",
                    target_target_id="account",
                    thread_id=claude_id,
                    lineage_name="claude-to-codex",
                    cwd=root,
                )

            codex_id = back_to_codex["target"]["threadId"]
            self.assertNotEqual(codex_id, claude_id)
            self.assertEqual(
                back_to_codex["source"]["threadId"],
                claude_id,
            )
            account_threads = FakeAppServer.threads_by_home[
                str(context.account_home.resolve())
            ]
            target_rollout = Path(str(account_threads[codex_id]["path"]))
            target_text = target_rollout.read_text(encoding="utf-8")
            self.assertIn("portable request", target_text)
            self.assertIn("portable answer", target_text)
            self.assertNotIn("encrypted_content", target_text)

    def test_publish_dry_run_does_not_create_lineage_or_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, _, _, thread_id = self.make_context(root)
            pool_root = root / "pool"
            run_cli(["init", "--pool", str(pool_root), "--json"], context)
            code, payload, stderr = run_cli(
                [
                    "publish",
                    "dry",
                    "--api-profile",
                    "relay",
                    "--thread",
                    thread_id,
                    "--dry-run",
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertTrue(payload["dryRun"])
            pool = ConversationPool(pool_root, security=FakeSecurity())
            self.assertEqual(pool.list_lineages(), [])
            self.assertEqual(
                codex_share_cli.LocalMappingStore(
                    context.local_state_root
                ).find(
                    pool_id=pool.pool_id(),
                    target_home=context.api_root / "profiles" / "relay",
                ),
                [],
            )

    @unittest.skipUnless(os.name == "nt", "Windows extended paths are Windows-only")
    def test_windows_extended_rollout_path_stays_inside_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, profile_home, rollout, thread_id = self.make_context(root)
            thread = FakeAppServer.threads_by_home[str(profile_home.resolve())][
                thread_id
            ]
            thread["path"] = "\\\\?\\" + str(rollout.resolve())
            pool_root = root / "pool"
            run_cli(["init", "--pool", str(pool_root), "--json"], context)
            code, payload, stderr = run_cli(
                [
                    "publish",
                    "extended-path",
                    "--api-profile",
                    "relay",
                    "--thread",
                    thread_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            self.assertTrue(payload["ok"])

    def test_clone_failure_rolls_back_thread_and_does_not_register_mapping(self) -> None:
        class FailingNameAppServer(FakeAppServer):
            def set_thread_name(self, thread_id: str, name: str) -> None:
                del thread_id, name
                raise AppServerError("simulated name failure")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, profile_home, _, thread_id = self.make_context(root)
            pool_root = root / "pool"
            run_cli(["init", "--pool", str(pool_root), "--json"], context)
            code, _, stderr = run_cli(
                [
                    "publish",
                    "rollback-demo",
                    "--api-profile",
                    "relay",
                    "--thread",
                    thread_id,
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 0, stderr)
            context.app_server_factory = FailingNameAppServer
            capability = AppServerCapability(True, "codex-cli test", "supported")
            with patch.object(
                codex_share_cli,
                "detect_fork_path_capability",
                return_value=capability,
            ):
                code, payload, _ = run_cli(
                    [
                        "clone",
                        "rollback-demo",
                        "--api-profile",
                        "relay",
                        "--cwd",
                        str(root),
                        "--json",
                    ],
                    context,
                )
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            threads = FakeAppServer.threads_by_home[str(profile_home.resolve())]
            self.assertEqual(set(threads), {thread_id})
            pool = ConversationPool(pool_root, security=FakeSecurity())
            mappings = codex_share_cli.LocalMappingStore(
                context.local_state_root
            ).find(
                pool_id=pool.pool_id(),
                target_home=profile_home,
            )
            self.assertEqual([mapping.thread_id for mapping in mappings], [thread_id])

    def test_apiagent_routes_share_without_loading_normal_profile_launcher(self) -> None:
        with patch.object(apiagent, "codex_share_main", return_value=7) as routed:
            code = apiagent.codex_main(["share", "list", "--json"])
        self.assertEqual(code, 7)
        routed.assert_called_once_with(["list", "--json"])

    def test_json_error_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context, _, _, _ = self.make_context(root)
            code, payload, stderr = run_cli(
                [
                    "list",
                    "--pool",
                    str(root / "missing-pool"),
                    "--json",
                ],
                context,
            )
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(stderr, "")


if __name__ == "__main__":
    unittest.main()
