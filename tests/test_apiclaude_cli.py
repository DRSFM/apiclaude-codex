from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
from secure_store import SecureStore


def _two_node_config() -> dict:
    return {
        "nodes": {
            "relay": {
                "base_url": "https://relay.test",
                "credential_id": "claude:relay",
            },
            "backup": {
                "base_url": "https://backup.test",
                "credential_id": "claude:backup",
            },
        },
        "current": "backup",
    }


def _no_input(prompt: str = "") -> str:
    raise AssertionError(f"unexpected interactive prompt: {prompt!r}")


class ApiClaudeCodexStyleCliTests(unittest.TestCase):
    def test_api_list_json_flag_matches_legacy_contract(self) -> None:
        config = _two_node_config()
        output = io.StringIO()

        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch("builtins.input", _no_input),
            redirect_stdout(output),
        ):
            code = apiagent.claude_main(["--api-list", "--json"])

        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(len(payload["nodes"]), 2)

    def test_json_without_api_list_is_rejected(self) -> None:
        with (
            patch.object(apiagent, "load_claude_config") as load,
            redirect_stderr(io.StringIO()),
        ):
            code = apiagent.claude_main(["--json"])

        self.assertEqual(code, 1)
        load.assert_not_called()

    def test_api_profile_starts_named_node_without_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            store.set("claude:relay", "sk-test-relay")
            config = _two_node_config()

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_claude_config", return_value=config),
                patch.object(apiagent, "save_claude_config"),
                patch.object(apiagent, "run_command", return_value=0) as run,
                patch("builtins.input", _no_input),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.claude_main(["--api-profile", "relay", "resume"])

            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[1], ["resume"])
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://relay.test")
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "sk-test-relay")
            self.assertEqual(config["current"], "relay")

    def test_api_profile_requires_name(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(apiagent.claude_main(["--api-profile"]), 1)

    def test_api_profile_unknown_node_fails(self) -> None:
        config = _two_node_config()
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "run_command", return_value=0) as run,
            patch("builtins.input", _no_input),
            redirect_stderr(io.StringIO()),
        ):
            code = apiagent.claude_main(["--api-profile", "ghost"])

        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_setup_alias_routes_to_add_with_requested_name(self) -> None:
        config = _two_node_config()
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "add_claude_node", return_value=0) as add,
        ):
            self.assertEqual(apiagent.claude_main(["--setup"]), 0)
            self.assertEqual(
                apiagent.claude_main(["--api-add", "--api-profile", "relay"]), 0
            )

        self.assertEqual(add.call_args_list[0].args, (config, None))
        self.assertEqual(add.call_args_list[1].args, (config, "relay"))

    def test_api_remove_passes_requested_node(self) -> None:
        config = _two_node_config()
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "remove_claude_node", return_value=0) as remove,
        ):
            code = apiagent.claude_main(["--api-remove", "--api-profile", "relay"])

        self.assertEqual(code, 0)
        self.assertEqual(remove.call_args.args, (config, "relay"))

    def test_vscode_flag_uses_api_profile_and_rejects_positional(self) -> None:
        config = _two_node_config()
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "launch_claude_vscode", return_value=0) as launch,
            patch("builtins.input", _no_input),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(
                apiagent.claude_main(["--vscode", "--api-profile", "relay"]), 0
            )
            self.assertEqual(apiagent.claude_main(["--vscode", "relay"]), 1)

        launch.assert_called_once_with(config, "relay")

    def test_interactive_remove_accepts_number_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            store.set("claude:relay", "sk-test-relay")
            config = _two_node_config()

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_claude_config", return_value=config),
                patch.object(apiagent, "save_claude_config"),
                patch("builtins.input", side_effect=["1", "YES"]),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.claude_main(["--api-remove"])

            self.assertEqual(code, 0)
            self.assertNotIn("relay", config["nodes"])
            self.assertIn("backup", config["nodes"])

    def test_api_help_flag_shows_help(self) -> None:
        output = io.StringIO()
        with patch.object(apiagent, "load_claude_config") as load, redirect_stdout(output):
            code = apiagent.claude_main(["--api-help"])

        self.assertEqual(code, 0)
        self.assertIn("--api-profile", output.getvalue())
        load.assert_not_called()

    def test_bare_args_still_pass_through_to_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            store.set("claude:relay", "sk-test-relay")
            config = {
                "nodes": {
                    "relay": {
                        "base_url": "https://relay.test",
                        "credential_id": "claude:relay",
                    }
                },
                "current": "relay",
            }

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_claude_config", return_value=config),
                patch.object(apiagent, "save_claude_config"),
                patch.object(apiagent, "run_command", return_value=0) as run,
                patch("builtins.input", _no_input),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.claude_main(["--permission-mode", "bypassPermissions"])

            self.assertEqual(code, 0)
            self.assertEqual(
                run.call_args.args[1], ["--permission-mode", "bypassPermissions"]
            )


if __name__ == "__main__":
    unittest.main()
