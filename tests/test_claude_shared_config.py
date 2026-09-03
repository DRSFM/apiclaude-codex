from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
from claude_shared_config import merge_shared_mcp_servers
from secure_store import SecureStore


class ClaudeSharedConfigTests(unittest.TestCase):
    def test_merge_updates_managed_servers_and_preserves_other_json(self) -> None:
        source = {
            "numStartups": 12,
            "mcpServers": {
                "SDW_Search": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["search.js"],
                }
            },
        }
        target = {
            "theme": "dark",
            "mcpServers": {"local": {"command": "local-mcp"}},
        }

        first = merge_shared_mcp_servers(target, source)
        updated_source = json.loads(json.dumps(source))
        updated_source["mcpServers"]["SDW_Search"]["args"] = ["search-v2.js"]
        second = merge_shared_mcp_servers(
            first.payload,
            updated_source,
            first.source_hashes,
        )

        self.assertEqual(second.payload["theme"], "dark")
        self.assertIn("local", second.payload["mcpServers"])
        self.assertEqual(
            second.payload["mcpServers"]["SDW_Search"]["args"],
            ["search-v2.js"],
        )

    def test_merge_preserves_local_conflict_and_removes_unchanged_copy(self) -> None:
        source = {"mcpServers": {"search": {"command": "shared-v1"}}}
        first = merge_shared_mcp_servers({}, source)
        locally_edited = json.loads(json.dumps(first.payload))
        locally_edited["mcpServers"]["search"]["command"] = "node-local"

        conflict = merge_shared_mcp_servers(
            locally_edited,
            {"mcpServers": {"search": {"command": "shared-v2"}}},
            first.source_hashes,
        )
        removed = merge_shared_mcp_servers(
            first.payload,
            {"mcpServers": {}},
            first.source_hashes,
        )

        self.assertEqual(conflict.conflicts, ("search",))
        self.assertEqual(
            conflict.payload["mcpServers"]["search"]["command"],
            "node-local",
        )
        self.assertNotIn("search", removed.payload.get("mcpServers", {}))

    def test_enable_syncs_account_user_mcp_to_isolated_and_desktop_configs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_root = root / ".apiclaude"
            account_config = root / ".claude.json"
            account_config.write_text(
                json.dumps(
                    {
                        "accountState": "keep",
                        "mcpServers": {
                            "SDW_Search": {
                                "type": "stdio",
                                "command": "node",
                                "args": ["search.js"],
                                "env": {"PRIVATE_TEST_TOKEN": "must-not-print"},
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            isolated_path = nodes_root / "nodes" / "relay" / ".claude.json"
            isolated_path.parent.mkdir(parents=True)
            isolated_path.write_text(
                json.dumps({"theme": "dark"}),
                encoding="utf-8",
            )
            desktop_root = root / ".apiclaude-desktop" / "nodes"
            desktop_path = (
                desktop_root
                / "relay"
                / "claude-code-config"
                / ".claude.json"
            )
            desktop_path.parent.mkdir(parents=True)
            desktop_path.write_text(
                json.dumps({"mcpServers": {"apiclaude-web": {"command": "web"}}}),
                encoding="utf-8",
            )
            config = {
                "nodes": {
                    "relay": {
                        "type": "codex_bridge",
                        "isolation": "isolated",
                        "home": "nodes/relay",
                    }
                },
                "current": "relay",
            }
            output = io.StringIO()

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CLAUDE_NODES_ROOT", nodes_root),
                patch.object(apiagent, "CLAUDE_DESKTOP_DATA_ROOT", desktop_root),
                patch.object(apiagent, "load_claude_config", return_value=config),
                redirect_stdout(output),
                redirect_stderr(output),
            ):
                code = apiagent.claude_shared_main(["enable", "--account"])

            self.assertEqual(code, 0)
            for path in (isolated_path, desktop_path):
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertIn("SDW_Search", payload["mcpServers"])
            self.assertEqual(
                json.loads(isolated_path.read_text(encoding="utf-8"))["theme"],
                "dark",
            )
            self.assertIn(
                "apiclaude-web",
                json.loads(desktop_path.read_text(encoding="utf-8"))["mcpServers"],
            )
            state = json.loads(
                (nodes_root / "shared-mcp.json").read_text(encoding="utf-8")
            )
            self.assertTrue(state["accountMcpEnabled"])
            self.assertEqual(list(state["managedServers"]), ["SDW_Search"])
            self.assertNotIn("must-not-print", output.getvalue())
            self.assertTrue(list((nodes_root / "shared-mcp-backups").glob("*/*")))

    def test_future_isolated_node_inherits_shared_mcp_when_added(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nodes_root = root / ".apiclaude"
            (nodes_root).mkdir(parents=True)
            (root / ".claude.json").write_text(
                json.dumps(
                    {"mcpServers": {"SDW_Search": {"command": "node"}}}
                ),
                encoding="utf-8",
            )
            (nodes_root / "shared-mcp.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "accountMcpEnabled": True,
                        "managedServers": {},
                    }
                ),
                encoding="utf-8",
            )
            config = {"nodes": {}, "current": None}

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CLAUDE_NODES_ROOT", nodes_root),
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", root / "nodes.json"),
                patch.object(apiagent, "SECRET_STORE", SecureStore(root / "secrets")),
                patch.object(apiagent, "getpass", return_value="sk-test"),
                patch(
                    "builtins.input",
                    side_effect=["https://relay.test", "", ""],
                ),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.add_claude_node(config, "relay")

            self.assertEqual(code, 0)
            node_config = nodes_root / "nodes" / "relay" / ".claude.json"
            payload = json.loads(node_config.read_text(encoding="utf-8"))
            self.assertIn("SDW_Search", payload["mcpServers"])

    def test_shared_enable_requires_explicit_account_authorization(self) -> None:
        error = io.StringIO()

        with redirect_stderr(error):
            code = apiagent.claude_shared_main(["enable"])

        self.assertEqual(code, 1)
        self.assertIn("requires --account", error.getvalue())


if __name__ == "__main__":
    unittest.main()
