from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
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


if __name__ == "__main__":
    unittest.main()
