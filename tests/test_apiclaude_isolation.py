from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
from secure_store import SecureStore, SecureStoreError


def _relay_config(**node_extra: object) -> dict:
    node = {
        "base_url": "https://example.test",
        "credential_id": "claude:relay",
    }
    node.update(node_extra)
    return {"nodes": {"relay": node}, "current": "relay"}


class ApiClaudeIsolationTests(unittest.TestCase):
    def test_shared_legacy_node_keeps_default_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            store.set("claude:relay", "sk-test-secret")
            config = _relay_config()

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "save_claude_config"),
                patch.object(apiagent, "run_command", return_value=0) as run,
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.run_claude_node(config, "relay", [])

            self.assertEqual(code, 0)
            self.assertNotIn("CLAUDE_CONFIG_DIR", run.call_args.kwargs["env"])
            self.assertIn(
                "CLAUDE_CONFIG_DIR",
                run.call_args.kwargs["env_remove"],
            )

    def test_isolated_node_sets_scoped_config_dir_and_persists_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "sk-test-secret")
            config = _relay_config(isolation="isolated")

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CLAUDE_NODES_ROOT", root / ".apiclaude"),
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", root / "config.json"),
                patch.object(
                    apiagent,
                    "now_iso",
                    return_value="2026-07-26T12:00:00+08:00",
                ),
                patch.object(apiagent, "run_command", return_value=0) as run,
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.run_claude_node(config, "relay", [])

            self.assertEqual(code, 0)
            expected_home = root / ".apiclaude" / "nodes" / "relay"
            self.assertEqual(
                run.call_args.kwargs["env"]["CLAUDE_CONFIG_DIR"], str(expected_home)
            )
            self.assertTrue(expected_home.is_dir())
            self.assertEqual(config["nodes"]["relay"]["home"], "nodes/relay")
            saved = json.loads((root / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["nodes"]["relay"]["home"], "nodes/relay")
            self.assertEqual(
                saved["nodes"]["relay"]["lastUsedAt"],
                "2026-07-26T12:00:00+08:00",
            )

    def test_list_json_exposes_only_non_sensitive_node_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _relay_config(
                isolation="isolated",
                home="nodes/relay",
                lastUsedAt="2026-07-26T12:00:00+08:00",
                token="must-not-leak",
            )
            output = io.StringIO()

            with (
                patch.object(apiagent, "CLAUDE_NODES_ROOT", root / ".apiclaude"),
                patch.object(
                    apiagent,
                    "CLAUDE_VSCODE_DATA_ROOT",
                    root / ".apiclaude-vscode",
                ),
                patch.object(apiagent, "load_claude_config", return_value=config),
                patch.object(apiagent, "SECRET_STORE") as store,
                redirect_stdout(output),
            ):
                code = apiagent.claude_main(["list", "--json"])

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(payload["current"], "relay")
            metadata = payload["nodes"][0]
            self.assertEqual(metadata["name"], "relay")
            self.assertEqual(metadata["isolation"], "isolated")
            self.assertEqual(
                metadata["lastUsedAt"],
                "2026-07-26T12:00:00+08:00",
            )
            self.assertNotIn("credential_id", metadata)
            self.assertNotIn("token", metadata)
            self.assertNotIn("must-not-leak", output.getvalue())
            store.get.assert_not_called()

    def test_list_json_rejects_unsafe_isolated_home(self) -> None:
        config = _relay_config(isolation="isolated", home="../.claude")

        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "SECRET_STORE") as store,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = apiagent.claude_main(["list", "--json"])

        self.assertEqual(code, 1)
        store.get.assert_not_called()

    def test_unsafe_isolated_home_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "sk-test-secret")
            for unsafe_home in ("..", "../escape", str(root / "elsewhere")):
                config = _relay_config(isolation="isolated", home=unsafe_home)

                with (
                    patch.object(apiagent, "SECRET_STORE", store),
                    patch.object(apiagent, "CLAUDE_NODES_ROOT", root / ".apiclaude"),
                    patch.object(apiagent, "save_claude_config"),
                    patch.object(apiagent, "run_command", return_value=0) as run,
                    redirect_stdout(io.StringIO()),
                ):
                    code = apiagent.run_claude_node(config, "relay", [])

                self.assertEqual(code, 1, unsafe_home)
                run.assert_not_called()

    def test_mode_switch_round_trip_preserves_isolated_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _relay_config()

            with (
                patch.object(apiagent, "CLAUDE_NODES_ROOT", root / ".apiclaude"),
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", root / "config.json"),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    apiagent.set_claude_node_mode(config, "relay", "isolated"), 0
                )
                node = config["nodes"]["relay"]
                self.assertEqual(node["isolation"], "isolated")
                home = apiagent.claude_node_home("relay", node)
                self.assertTrue(home.is_dir())
                marker = home / "settings.json"
                marker.write_text("{}", encoding="utf-8")

                self.assertEqual(
                    apiagent.set_claude_node_mode(config, "relay", "shared"), 0
                )
                self.assertEqual(node["isolation"], "shared")
                self.assertTrue(marker.exists())

                self.assertEqual(
                    apiagent.set_claude_node_mode(config, "relay", "isolated"), 0
                )
                self.assertEqual(node["isolation"], "isolated")
                self.assertEqual(apiagent.claude_node_home("relay", node), home)
                self.assertTrue(marker.exists())

    def test_mode_rejects_unknown_value_and_missing_node(self) -> None:
        config = _relay_config()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(apiagent.set_claude_node_mode(config, "relay", "both"), 1)
            self.assertEqual(apiagent.set_claude_node_mode(config, "ghost", "shared"), 1)
        self.assertEqual(apiagent.claude_node_isolation(config["nodes"]["relay"]), "shared")

    def test_default_home_collision_appends_digest(self) -> None:
        config = {"nodes": {"配置一": {}, "配置二": {}}, "current": None}
        first = apiagent.default_claude_node_home(config, "配置一")
        config["nodes"]["配置一"]["home"] = first
        second = apiagent.default_claude_node_home(config, "配置二")
        self.assertEqual(first, "nodes/profile")
        self.assertNotEqual(first, second)
        self.assertTrue(second.startswith("nodes/profile-"))

    def test_remove_archives_isolated_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "sk-test-secret")
            nodes_root = root / ".apiclaude"
            archive_root = nodes_root / "archived-nodes"
            home = nodes_root / "nodes" / "relay"
            home.mkdir(parents=True)
            (home / "settings.json").write_text("{}", encoding="utf-8")
            config = _relay_config(isolation="isolated", home="nodes/relay")

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CLAUDE_NODES_ROOT", nodes_root),
                patch.object(apiagent, "CLAUDE_ARCHIVE_ROOT", archive_root),
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", root / "config.json"),
                patch("builtins.input", return_value="YES"),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.remove_claude_node(config, "relay")

            self.assertEqual(code, 0)
            self.assertNotIn("relay", config["nodes"])
            self.assertFalse(home.exists())
            archived = list(archive_root.iterdir())
            self.assertEqual(len(archived), 1)
            self.assertTrue((archived[0] / "settings.json").exists())
            with self.assertRaises((KeyError, SecureStoreError)):
                store.get("claude:relay")

    def test_remove_archive_failure_preserves_node_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "sk-test-secret")
            nodes_root = root / ".apiclaude"
            archive_root = nodes_root / "archived-nodes"
            home = nodes_root / "nodes" / "relay"
            home.mkdir(parents=True)
            config = _relay_config(isolation="isolated", home="nodes/relay")

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CLAUDE_NODES_ROOT", nodes_root),
                patch.object(apiagent, "CLAUDE_ARCHIVE_ROOT", archive_root),
                patch.object(apiagent, "save_claude_config") as save_config,
                patch.object(apiagent.shutil, "move", side_effect=OSError("locked")),
                patch("builtins.input", return_value="YES"),
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                code = apiagent.remove_claude_node(config, "relay")

            self.assertEqual(code, 1)
            self.assertIn("relay", config["nodes"])
            self.assertTrue(home.exists())
            self.assertEqual(store.get("claude:relay"), "sk-test-secret")
            save_config.assert_not_called()

    def test_remove_rejects_unsafe_isolated_home_without_mutation(self) -> None:
        config = _relay_config(isolation="isolated", home="../.claude")

        with (
            patch.object(apiagent, "SECRET_STORE") as store,
            patch.object(apiagent, "save_claude_config") as save_config,
            patch.object(apiagent.shutil, "move") as move,
            patch("builtins.input", return_value="YES"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = apiagent.remove_claude_node(config, "relay")

        self.assertEqual(code, 1)
        self.assertIn("relay", config["nodes"])
        save_config.assert_not_called()
        store.clear.assert_not_called()
        move.assert_not_called()

    def test_isolated_vscode_launch_uses_node_scoped_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "sk-test-secret")
            config = _relay_config(isolation="isolated", home="nodes/relay")

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CLAUDE_NODES_ROOT", root / ".apiclaude"),
                patch.object(
                    apiagent,
                    "CLAUDE_VSCODE_DATA_ROOT",
                    root / ".apiclaude-vscode",
                ),
                patch.object(apiagent, "save_claude_config") as save_config,
                patch.object(
                    apiagent,
                    "now_iso",
                    return_value="2026-07-26T12:00:00+08:00",
                ),
                patch.object(apiagent, "run_command", return_value=0) as run,
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.launch_claude_vscode(config, "relay")

            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[0], "code")
            command_args = run.call_args.args[1]
            self.assertIn("--new-window", command_args)
            self.assertIn("--user-data-dir", command_args)
            self.assertNotIn("sk-test-secret", command_args)
            child_env = run.call_args.kwargs["env"]
            self.assertEqual(child_env["ANTHROPIC_AUTH_TOKEN"], "sk-test-secret")
            self.assertEqual(
                child_env["CLAUDE_CONFIG_DIR"],
                str(root / ".apiclaude" / "nodes" / "relay"),
            )
            self.assertEqual(config["current"], "relay")
            self.assertEqual(
                config["nodes"]["relay"]["lastUsedAt"],
                "2026-07-26T12:00:00+08:00",
            )
            save_config.assert_called_once_with(config)

    def test_shared_vscode_launch_omits_claude_config_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "sk-test-secret")
            config = _relay_config()

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
            self.assertNotIn("CLAUDE_CONFIG_DIR", run.call_args.kwargs["env"])
            self.assertIn(
                "CLAUDE_CONFIG_DIR",
                run.call_args.kwargs["env_remove"],
            )

    def test_vscode_and_update_management_commands_are_routed(self) -> None:
        config = _relay_config()
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "launch_claude_vscode", return_value=0) as vscode,
            redirect_stdout(io.StringIO()),
        ):
            code = apiagent.claude_main(["vscode", "relay"])

        self.assertEqual(code, 0)
        vscode.assert_called_once_with(config, "relay")

        for command in ("--up", "update"):
            with self.subTest(command=command):
                with (
                    patch.object(apiagent, "load_claude_config", return_value=config),
                    patch.object(apiagent, "run_command", return_value=0) as run,
                    redirect_stdout(io.StringIO()),
                ):
                    code = apiagent.claude_main([command])

                self.assertEqual(code, 0)
                run.assert_called_once()
                self.assertEqual(run.call_args.args, ("claude", ["update"]))
                self.assertIn(
                    "ANTHROPIC_AUTH_TOKEN",
                    run.call_args.kwargs["env_remove"],
                )


if __name__ == "__main__":
    unittest.main()
