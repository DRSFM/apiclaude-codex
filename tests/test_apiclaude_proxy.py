from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
from secure_store import SecureStore


class ApiClaudeProxyTests(unittest.TestCase):
    def test_load_migrates_all_existing_nodes_to_enabled_default_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "claude.json"
            config_path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "relay": {"base_url": "https://relay.test"},
                            "bridge": {
                                "type": "codex_bridge",
                                "codex_profile": "relay",
                            },
                        },
                        "current": "relay",
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", config_path),
                patch.object(apiagent, "SECRET_STORE", SecureStore(root / "secrets")),
            ):
                config = apiagent.load_claude_config()

            for node in config["nodes"].values():
                self.assertTrue(node["proxy_enabled"])
                self.assertEqual(node["proxy_url"], apiagent.DEFAULT_CLAUDE_PROXY_URL)
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["nodes"]["relay"]["proxy_enabled"])
            self.assertTrue(saved["nodes"]["bridge"]["proxy_enabled"])

    def test_add_node_defaults_proxy_on_and_allows_opt_out(self) -> None:
        for answer, expected in (("", True), ("n", False)):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = {"nodes": {}, "current": None}
                with (
                    patch.object(apiagent, "SECRET_STORE", SecureStore(root / "secrets")),
                    patch.object(apiagent, "CLAUDE_CONFIG_PATH", root / "claude.json"),
                    patch.object(apiagent, "getpass", return_value="sk-test"),
                    patch(
                        "builtins.input",
                        side_effect=["https://relay.test", "", answer],
                    ),
                    redirect_stdout(io.StringIO()),
                ):
                    code = apiagent.add_claude_node(config, "relay")

                self.assertEqual(code, 0)
                node = config["nodes"]["relay"]
                self.assertIs(node["proxy_enabled"], expected)
                self.assertEqual(node["proxy_url"], apiagent.DEFAULT_CLAUDE_PROXY_URL)

    def test_regular_node_launch_sets_or_removes_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp) / "secrets")
            store.set("claude:relay", "sk-test")
            for enabled in (True, False):
                with self.subTest(enabled=enabled):
                    config = {
                        "nodes": {
                            "relay": {
                                "base_url": "https://relay.test",
                                "credential_id": "claude:relay",
                                "proxy_enabled": enabled,
                                "proxy_url": "http://127.0.0.1:7897",
                            }
                        },
                        "current": "relay",
                    }
                    with (
                        patch.object(apiagent, "SECRET_STORE", store),
                        patch.object(apiagent, "save_claude_config"),
                        patch.object(apiagent, "run_command", return_value=0) as run,
                        redirect_stdout(io.StringIO()),
                    ):
                        code = apiagent.run_claude_node(config, "relay", [])

                    self.assertEqual(code, 0)
                    env = run.call_args.kwargs["env"]
                    if enabled:
                        self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7897")
                        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")
                    else:
                        self.assertNotIn("HTTP_PROXY", env)
                        self.assertNotIn("HTTPS_PROXY", env)
                    removed = run.call_args.kwargs["env_remove"]
                    self.assertIn("HTTP_PROXY", removed)
                    self.assertIn("HTTPS_PROXY", removed)

    def test_vscode_launch_uses_the_same_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "sk-test")
            config = {
                "nodes": {
                    "relay": {
                        "base_url": "https://relay.test",
                        "credential_id": "claude:relay",
                        "proxy_enabled": True,
                        "proxy_url": "http://127.0.0.1:7897",
                    }
                },
                "current": "relay",
            }
            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(
                    apiagent,
                    "CLAUDE_VSCODE_DATA_ROOT",
                    root / ".apiclaude-vscode",
                ),
                patch.object(apiagent, "save_claude_config"),
                patch.object(apiagent, "run_command", return_value=0) as run,
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.launch_claude_vscode(config, "relay")

            self.assertEqual(code, 0)
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:7897")
            self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7897")

    def test_proxy_flag_selects_node_then_disables_or_enables_it(self) -> None:
        config = {
            "nodes": {
                "relay": {
                    "base_url": "https://relay.test",
                    "proxy_enabled": True,
                    "proxy_url": "http://127.0.0.1:7897",
                },
                "backup": {
                    "base_url": "https://backup.test",
                    "proxy_enabled": False,
                    "proxy_url": "http://127.0.0.1:7897",
                },
            },
            "current": "backup",
        }
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "save_claude_config") as save,
            patch("builtins.input", side_effect=["1", "n"]),
            redirect_stdout(io.StringIO()),
        ):
            code = apiagent.claude_main(["--proxy"])

        self.assertEqual(code, 0)
        self.assertFalse(config["nodes"]["relay"]["proxy_enabled"])
        save.assert_called_once_with(config)

        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "save_claude_config") as save,
            patch("builtins.input", return_value="y"),
            redirect_stdout(io.StringIO()),
        ):
            code = apiagent.claude_main(
                ["--proxy", "--api-profile", "backup"]
            )

        self.assertEqual(code, 0)
        self.assertTrue(config["nodes"]["backup"]["proxy_enabled"])
        save.assert_called_once_with(config)

    def test_proxy_state_is_in_non_sensitive_list_metadata(self) -> None:
        metadata = apiagent.claude_node_metadata(
            "relay",
            {
                "base_url": "https://relay.test",
                "proxy_enabled": True,
                "proxy_url": "http://127.0.0.1:7897",
            },
        )

        self.assertTrue(metadata["proxyEnabled"])
        self.assertEqual(metadata["proxyUrl"], "http://127.0.0.1:7897")

    def test_bridge_effective_proxy_respects_enabled_switch(self) -> None:
        node = {
            "proxy_enabled": False,
            "proxy_url": "http://127.0.0.1:7897",
        }
        self.assertEqual(apiagent.claude_node_effective_proxy(node), "direct")
        node["proxy_enabled"] = True
        self.assertEqual(
            apiagent.claude_node_effective_proxy(node),
            "http://127.0.0.1:7897",
        )


if __name__ == "__main__":
    unittest.main()
