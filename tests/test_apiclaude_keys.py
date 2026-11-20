from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from getpass import GetPassWarning
import io
from pathlib import Path
import tempfile
from unittest import skipUnless
from unittest.mock import patch
import warnings

import apiagent as api
from secure_store import SecureStore, SecureStoreError
from tests.support import KeychainIsolationMixin


class ApiClaudeKeysTests(KeychainIsolationMixin):
    def setUp(self) -> None:
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.store = SecureStore(root / "secrets")
        self.store.set("claude:anyrouter", "synthetic-claude-first")
        nodes_root = root / ".apiclaude"
        self.home = nodes_root / "nodes" / "anyrouter"
        self.home.mkdir(parents=True)
        self.chat = self.home / "conversation.jsonl"
        self.chat.write_bytes(b"existing conversation")
        for name, value in (
            ("CLAUDE_CONFIG_PATH", root / "claude.json"),
            ("CLAUDE_NODES_ROOT", nodes_root),
            ("CLAUDE_VSCODE_DATA_ROOT", root / "vscode"),
            ("CLAUDE_DESKTOP_DATA_ROOT", root / "desktop"),
            ("CLAUDE_ARCHIVE_ROOT", root / "archive"),
            ("SECRET_STORE", self.store),
            ("HOME", root),
        ):
            mock = patch.object(api, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        runner = patch.object(api, "run_command", side_effect=AssertionError("unexpected client launch"))
        runner.start()
        self.addCleanup(runner.stop)
        api.save_claude_config({
            "nodes": {"anyrouter": {
                "base_url": "https://relay.test",
                "isolation": "isolated",
                "home": "nodes/anyrouter",
                "proxy_enabled": False,
                "proxy_url": api.DEFAULT_CLAUDE_PROXY_URL,
                "cli_force_default_model": True,
            }},
            "current": "anyrouter",
        })

    def node(self) -> dict:
        return api.load_claude_config()["nodes"]["anyrouter"]

    def add_key(self) -> int:
        with patch.object(api, "getpass", return_value="\ufeffsynthetic-claude-second"), redirect_stdout(io.StringIO()):
            return api.claude_main(["keys", "add", "anyrouter", "account-2"])

    def use_key(self, label: str) -> int:
        with redirect_stdout(io.StringIO()):
            return api.claude_main(["keys", "use", "anyrouter", label])

    def test_add_keeps_original_active_and_list_only_shows_labels(self) -> None:
        self.assertEqual(self.add_key(), 0)
        self.assertEqual(api.get_claude_secret("anyrouter", self.node()), "synthetic-claude-first")
        output = io.StringIO()
        with patch.object(self.store, "get", side_effect=AssertionError("unexpected secret read")), redirect_stdout(output):
            self.assertEqual(api.claude_main(["keys", "list", "anyrouter"]), 0)
        self.assertIn("account-1 (current)", output.getvalue())
        self.assertIn("account-2", output.getvalue())
        self.assertNotIn("synthetic-claude", api.CLAUDE_CONFIG_PATH.read_text(encoding="utf-8"))
        with patch.object(api, "getpass") as prompt, redirect_stderr(io.StringIO()):
            self.assertEqual(api.claude_main(["keys", "add", "anyrouter", "ACCOUNT-2"]), 1)
        prompt.assert_not_called()

    def test_interactive_and_noninteractive_launch_share_history(self) -> None:
        self.assertEqual(self.add_key(), 0)
        config = api.load_claude_config()
        config["nodes"]["other"] = {"base_url": "https://other.test"}
        api.save_claude_config(config)
        launches = (
            ([], True, ["1", "2"], "synthetic-claude-second"),
            (["--api-profile", "anyrouter", "--resume"], False, [], "synthetic-claude-second"),
            (["--vscode", "--api-profile", "anyrouter"], True, ["account-1"], "synthetic-claude-first"),
        )
        for args, interactive, replies, expected in launches:
            with (
                self.subTest(args=args),
                patch.object(api.sys.stdin, "isatty", return_value=interactive),
                patch("builtins.input", side_effect=replies),
                patch.object(api, "run_command", return_value=0) as run,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(api.claude_main(args), 0)
                self.assertEqual(run.call_args.kwargs["env"]["ANTHROPIC_AUTH_TOKEN"], expected)
                self.assertEqual(run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], str(self.home))
                self.assertNotIn(expected, " ".join(run.call_args.args[1]))
        self.assertEqual(self.chat.read_bytes(), b"existing conversation")

    @skipUnless(api.os.name == "nt", "Claude Desktop launches on Windows")
    def test_desktop_reads_selection_for_the_same_instance(self) -> None:
        self.assertEqual(self.add_key(), 0)
        captured = []

        def run_worker(config, name, *, port):
            node = config["nodes"][name]
            captured.append((api.claude_desktop_profile_home(name, node), api.get_claude_secret(name, node)))
            return 0

        with (
            patch.object(api.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=["2", "1"]),
            patch.object(api, "find_claude_desktop_executable", return_value=self.home / "Claude.exe"),
            patch.object(api, "ensure_private_desktop_directory"),
            patch.object(api, "run_claude_desktop_worker", side_effect=run_worker),
            redirect_stdout(io.StringIO()),
        ):
            args = ["--desktop-foreground", "--api-profile", "anyrouter"]
            self.assertEqual(api.claude_main(args), 0)
            self.assertEqual(api.claude_main(args), 0)
        profile = api.CLAUDE_DESKTOP_DATA_ROOT / "anyrouter"
        self.assertEqual(captured, [(profile, "synthetic-claude-second"), (profile, "synthetic-claude-first")])

    def test_cancel_and_missing_key_do_not_launch_or_change_selection(self) -> None:
        self.assertEqual(self.add_key(), 0)
        self.assertEqual(self.use_key("account-2"), 0)
        self.store.clear(self.node()["credential_id"])
        original = api.CLAUDE_CONFIG_PATH.read_bytes()
        for interactive in (True, False):
            with (
                patch.object(api.sys.stdin, "isatty", return_value=interactive),
                patch("builtins.input", side_effect=["q"] if interactive else []),
                patch.object(api, "run_command") as run,
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(api.claude_main(["--api-profile", "anyrouter"]), 1)
                run.assert_not_called()
            self.assertEqual(api.CLAUDE_CONFIG_PATH.read_bytes(), original)
        self.assertEqual(self.store.get("claude:anyrouter"), "synthetic-claude-first")

    def test_failed_registry_write_leaves_original_and_allows_retry(self) -> None:
        original = api.CLAUDE_CONFIG_PATH.read_bytes()
        with patch.object(api, "write_json_atomic", side_effect=OSError("sensitive-error-sentinel")), redirect_stderr(io.StringIO()) as error:
            self.assertEqual(self.add_key(), 1)
        self.assertNotIn("sensitive-error-sentinel", error.getvalue())
        self.assertEqual(api.CLAUDE_CONFIG_PATH.read_bytes(), original)
        self.assertEqual(self.store.get("claude:anyrouter"), "synthetic-claude-first")
        self.assertEqual(self.add_key(), 0)

    def test_add_stops_before_echoed_input_if_hidden_input_is_unavailable(self) -> None:
        original = api.CLAUDE_CONFIG_PATH.read_bytes()

        def unavailable(_prompt: str) -> str:
            warnings.warn("fallback", GetPassWarning)
            self.fail("input would be echoed")

        with patch.object(api, "getpass", side_effect=unavailable), redirect_stderr(io.StringIO()):
            self.assertEqual(api.claude_main(["keys", "add", "anyrouter", "account-2"]), 1)
        self.assertEqual(api.CLAUDE_CONFIG_PATH.read_bytes(), original)

    def test_node_update_replaces_only_the_active_key_and_remove_clears_both(self) -> None:
        self.assertEqual(self.add_key(), 0)
        self.assertEqual(self.use_key("account-2"), 0)
        previous = self.node()["credential_id"]
        with (
            patch.object(api, "getpass", return_value="synthetic-claude-replacement"),
            patch("builtins.input", side_effect=["y", "https://relay.test", "", ""]),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(api.claude_main(["--api-add", "--api-profile", "anyrouter"]), 0)
        second = self.node()["credential_id"]
        self.assertNotEqual(second, previous)
        self.assertEqual(self.node()["apiKeys"][1], {"name": "account-2", "credentialId": second})
        self.assertTrue(self.node()["cli_force_default_model"])
        self.assertEqual(self.store.get(second), "synthetic-claude-replacement")
        self.assertEqual(self.store.get("claude:anyrouter"), "synthetic-claude-first")
        with self.assertRaises(KeyError):
            self.store.get(previous)

        with patch("builtins.input", return_value="YES"), redirect_stdout(io.StringIO()):
            self.assertEqual(api.claude_main(["--api-remove", "--api-profile", "anyrouter"]), 0)
        for ref in ("claude:anyrouter", second):
            with self.assertRaises((KeyError, SecureStoreError)):
                self.store.get(ref)
        self.assertEqual(list(api.CLAUDE_ARCHIVE_ROOT.glob("*/conversation.jsonl"))[0].read_bytes(), b"existing conversation")

    def test_update_and_remove_leave_keys_and_history_on_registry_failure(self) -> None:
        self.assertEqual(self.add_key(), 0)
        self.assertEqual(self.use_key("account-2"), 0)
        original = api.CLAUDE_CONFIG_PATH.read_bytes()
        with (
            patch.object(api, "write_json_atomic", side_effect=OSError("unavailable")),
            patch.object(api, "getpass", return_value="synthetic-claude-replacement"),
            redirect_stdout(io.StringIO()),
        ):
            with patch("builtins.input", side_effect=["y", "https://relay.test", "", ""]):
                with self.assertRaises(OSError):
                    api.claude_main(["--api-add", "--api-profile", "anyrouter"])
            with patch("builtins.input", return_value="YES"):
                with self.assertRaises(OSError):
                    api.claude_main(["--api-remove", "--api-profile", "anyrouter"])
        self.assertEqual(api.CLAUDE_CONFIG_PATH.read_bytes(), original)
        self.assertTrue(self.chat.is_file())
        self.assertEqual(self.store.get("claude:anyrouter"), "synthetic-claude-first")
        self.assertEqual(api.get_claude_secret("anyrouter", self.node()), "synthetic-claude-second")

    def test_launch_metadata_does_not_overwrite_newer_key_choice(self) -> None:
        self.assertEqual(self.add_key(), 0)
        original_save = api.save_claude_config

        def save_after_switch(config):
            self.assertEqual(self.use_key("account-1"), 0)
            original_save(config)

        with (
            patch.object(api.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="2"),
            patch.object(api, "save_claude_config", side_effect=save_after_switch),
            patch.object(api, "run_command", return_value=0) as run,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(api.claude_main(["--api-profile", "anyrouter"]), 0)
        self.assertEqual(run.call_args.kwargs["env"]["ANTHROPIC_AUTH_TOKEN"], "synthetic-claude-second")
        self.assertEqual(api.get_claude_secret("anyrouter", self.node()), "synthetic-claude-first")

    @skipUnless(api.os.name == "nt", "Claude Desktop launches on Windows")
    def test_desktop_background_spawns_with_selected_reference(self) -> None:
        self.assertEqual(self.add_key(), 0)
        second_ref = self.node()["apiKeys"][1]["credentialId"]
        with (
            patch.object(api.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", return_value="2"),
            patch.object(api, "find_claude_desktop_executable", return_value=self.home / "Claude.exe"),
            patch.object(api, "ensure_private_desktop_directory"),
            patch.object(api, "_spawn_claude_desktop_worker", return_value=0) as spawn,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(api.claude_main(["--desktop", "--api-profile", "anyrouter"]), 0)
        spawn.assert_called_once_with(
            api.CLAUDE_DESKTOP_DATA_ROOT / "anyrouter", "anyrouter", port=None, key_reference=second_ref,
        )

    @skipUnless(api.os.name == "nt", "Claude Desktop launches on Windows")
    def test_desktop_worker_uses_pinned_key_after_another_selection(self) -> None:
        self.assertEqual(self.add_key(), 0)
        self.assertEqual(self.use_key("account-2"), 0)
        second_ref = self.node()["credential_id"]
        self.assertEqual(self.use_key("account-1"), 0)
        with (
            patch.object(api, "find_claude_desktop_executable", return_value=self.home / "Claude.exe"),
            patch.object(api, "ensure_private_desktop_directory"),
            patch.object(api, "anthropic_passthrough_bridge", side_effect=api.ClaudeDesktopError("test stop")) as gateway,
            redirect_stderr(io.StringIO()),
        ):
            code = api.claude_main(["--desktop-worker", "--api-profile", "anyrouter", "--key-reference", second_ref])
        self.assertEqual(code, 1)
        self.assertEqual(gateway.call_args.kwargs["upstream_api_key"], "synthetic-claude-second")
        self.assertEqual(api.get_claude_secret("anyrouter", self.node()), "synthetic-claude-first")
