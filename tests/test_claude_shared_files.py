from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
import claude_shared_files as shared_files


def _write_skill(directory: Path, body: str = "skill") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(body, encoding="utf-8")
    return directory


def _read_skill(directory: Path) -> str:
    return (directory / "SKILL.md").read_text(encoding="utf-8")


def _links_supported(root: Path) -> bool:
    target = root / "link-probe-target"
    target.mkdir()
    link = root / "link-probe"
    try:
        shared_files.create_directory_link(link, target)
    except OSError:
        return False
    shared_files.remove_directory_link(link)
    target.rmdir()
    return True


class SharedInstructionsPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def _target(self, name: str) -> tuple[str, Path]:
        home = self.root / "nodes" / name
        home.mkdir(parents=True, exist_ok=True)
        return (f"node:{name}", home)

    def test_writes_missing_copy_and_adopts_identical_copy(self) -> None:
        empty = self._target("empty")
        same = self._target("same")
        (same[1] / "CLAUDE.md").write_bytes(b"rules v1\r\n")

        plan = shared_files.plan_shared_instructions(b"rules v1\r\n", [empty, same], {})
        shared_files.apply_shared_instructions(plan, b"rules v1\r\n")

        self.assertEqual([label for label, _path in plan.writes], ["node:empty"])
        self.assertEqual(plan.conflicts, ())
        self.assertEqual(len(plan.managed), 2)
        self.assertEqual((empty[1] / "CLAUDE.md").read_bytes(), b"rules v1\r\n")

    def test_updates_unedited_copy_and_keeps_locally_edited_copy(self) -> None:
        clean = self._target("clean")
        edited = self._target("edited")
        first = shared_files.plan_shared_instructions(b"v1", [clean, edited], {})
        shared_files.apply_shared_instructions(first, b"v1")
        (edited[1] / "CLAUDE.md").write_bytes(b"node-local edit")

        second = shared_files.plan_shared_instructions(
            b"v2", [clean, edited], first.managed
        )
        shared_files.apply_shared_instructions(second, b"v2")

        self.assertEqual([label for label, _path in second.writes], ["node:clean"])
        self.assertEqual(second.conflicts, ("node:edited",))
        self.assertEqual((clean[1] / "CLAUDE.md").read_bytes(), b"v2")
        self.assertEqual((edited[1] / "CLAUDE.md").read_bytes(), b"node-local edit")

    def test_disable_removes_only_unedited_managed_copies(self) -> None:
        clean = self._target("clean")
        edited = self._target("edited")
        foreign = self._target("foreign")
        (foreign[1] / "CLAUDE.md").write_bytes(b"never managed")
        first = shared_files.plan_shared_instructions(b"v1", [clean, edited], {})
        shared_files.apply_shared_instructions(first, b"v1")
        (edited[1] / "CLAUDE.md").write_bytes(b"node-local edit")

        off = shared_files.plan_shared_instructions(
            None, [clean, edited, foreign], first.managed
        )
        shared_files.apply_shared_instructions(off, None)

        self.assertEqual([label for label, _path in off.removes], ["node:clean"])
        self.assertEqual(off.conflicts, ("node:edited",))
        self.assertEqual(off.managed, {})
        self.assertFalse((clean[1] / "CLAUDE.md").exists())
        self.assertEqual((edited[1] / "CLAUDE.md").read_bytes(), b"node-local edit")
        self.assertEqual((foreign[1] / "CLAUDE.md").read_bytes(), b"never managed")

    def test_entries_for_unknown_nodes_pass_through(self) -> None:
        known = self._target("known")
        ghost_copy = self.root / "nodes" / "ghost" / "CLAUDE.md"
        ghost_copy.parent.mkdir(parents=True)
        ghost_copy.write_bytes(b"v0")
        ghost_key = shared_files.path_key(ghost_copy)
        managed = {ghost_key: shared_files._sha256(b"v0")}

        on = shared_files.plan_shared_instructions(b"v1", [known], managed)
        off = shared_files.plan_shared_instructions(None, [known], on.managed)
        shared_files.apply_shared_instructions(off, None)

        self.assertEqual(on.managed[ghost_key], managed[ghost_key])
        self.assertEqual(off.managed, managed)
        self.assertEqual(ghost_copy.read_bytes(), b"v0")


class SharedSkillsPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        if not _links_supported(self.root):
            self.skipTest("directory links are not available here")
        self.store = self.root / "store"
        self.account = self.root / "account-skills"
        self.account.mkdir()
        self.node = self.root / "nodes" / "relay"
        self.node.mkdir(parents=True)
        self.target = ("node:relay", self.node)

    def _plan(self, skills, managed=None) -> shared_files.SkillsPlan:
        return shared_files.plan_shared_skills(skills, [self.target], managed or {})

    def test_discover_skips_non_skills_and_resolves_links(self) -> None:
        real = _write_skill(self.store / "linked")
        shared_files.create_directory_link(self.account / "linked", real)
        _write_skill(self.account / "plain")
        _write_skill(self.account / ".system")
        (self.account / "notes").mkdir()
        (self.account / "README.md").write_text("not a skill", encoding="utf-8")

        found = shared_files.discover_shared_skills(self.account)

        self.assertEqual(list(found), ["linked", "plain"])
        self.assertEqual(
            shared_files.path_key(found["linked"]), shared_files.path_key(real)
        )

    def test_discover_requires_the_skills_directory(self) -> None:
        with self.assertRaises(FileNotFoundError):
            shared_files.discover_shared_skills(self.root / "missing")

    def test_links_are_created_once_and_then_left_alone(self) -> None:
        skills = {"alpha": _write_skill(self.store / "alpha", "alpha body")}

        first = self._plan(skills)
        shared_files.apply_shared_skills(first)
        again = self._plan(skills, first.managed)

        self.assertEqual([link.name for _label, link, _target in first.links], ["alpha"])
        self.assertEqual(_read_skill(self.node / "skills" / "alpha"), "alpha body")
        self.assertIsNotNone(
            shared_files.directory_link_target(self.node / "skills" / "alpha")
        )
        self.assertEqual((again.links, again.relinks, again.unlinks), ((), (), ()))
        self.assertEqual(again.conflicts, {})
        self.assertEqual(again.managed, first.managed)

    def test_adopts_matching_link_made_by_someone_else(self) -> None:
        real = _write_skill(self.store / "alpha")
        shared_files.create_directory_link(self.node / "skills" / "alpha", real)

        plan = self._plan({"alpha": real})

        self.assertEqual((plan.links, plan.relinks, plan.unlinks), ((), (), ()))
        self.assertEqual(plan.conflicts, {})
        self.assertEqual(len(plan.managed), 1)

    def test_node_local_directory_and_foreign_link_are_kept(self) -> None:
        shared_alpha = _write_skill(self.store / "alpha", "account")
        shared_beta = _write_skill(self.store / "beta", "account")
        _write_skill(self.node / "skills" / "alpha", "node-local")
        elsewhere = _write_skill(self.root / "elsewhere" / "beta", "foreign")
        shared_files.create_directory_link(self.node / "skills" / "beta", elsewhere)

        plan = self._plan({"alpha": shared_alpha, "beta": shared_beta})
        shared_files.apply_shared_skills(plan)

        self.assertEqual(plan.conflicts, {"node:relay": ("alpha", "beta")})
        self.assertEqual((plan.links, plan.relinks, plan.unlinks), ((), (), ()))
        self.assertEqual(plan.managed, {})
        self.assertEqual(_read_skill(self.node / "skills" / "alpha"), "node-local")
        self.assertEqual(_read_skill(self.node / "skills" / "beta"), "foreign")

    def test_removed_skill_drops_only_the_managed_link(self) -> None:
        alpha = _write_skill(self.store / "alpha")
        beta = _write_skill(self.store / "beta", "beta body")
        first = self._plan({"alpha": alpha, "beta": beta})
        shared_files.apply_shared_skills(first)

        second = self._plan({"alpha": alpha}, first.managed)
        shared_files.apply_shared_skills(second)

        self.assertEqual([link.name for _label, link in second.unlinks], ["beta"])
        self.assertFalse(os.path.lexists(self.node / "skills" / "beta"))
        self.assertEqual(_read_skill(beta), "beta body")
        self.assertTrue((self.node / "skills" / "alpha" / "SKILL.md").is_file())
        self.assertEqual(len(second.managed), 1)

    def test_link_replaced_by_a_real_directory_is_not_removed(self) -> None:
        alpha = _write_skill(self.store / "alpha")
        first = self._plan({"alpha": alpha})
        shared_files.apply_shared_skills(first)
        link = self.node / "skills" / "alpha"
        shared_files.remove_directory_link(link)
        _write_skill(link, "now node-local")

        off = self._plan(None, first.managed)
        shared_files.apply_shared_skills(off)

        self.assertEqual(off.unlinks, ())
        self.assertEqual(off.managed, {})
        self.assertEqual(_read_skill(link), "now node-local")

    def test_moved_skill_repoints_the_managed_link(self) -> None:
        old = _write_skill(self.store / "alpha-old", "old")
        new = _write_skill(self.store / "alpha-new", "new")
        first = self._plan({"alpha": old})
        shared_files.apply_shared_skills(first)

        second = self._plan({"alpha": new}, first.managed)
        shared_files.apply_shared_skills(second)

        self.assertEqual(
            [link.name for _label, link, _target in second.relinks], ["alpha"]
        )
        self.assertEqual(_read_skill(self.node / "skills" / "alpha"), "new")
        self.assertEqual(_read_skill(old), "old")

    def test_remove_directory_link_refuses_a_real_directory(self) -> None:
        real = _write_skill(self.store / "alpha")

        with self.assertRaises(OSError):
            shared_files.remove_directory_link(real)

        self.assertTrue((real / "SKILL.md").is_file())

    def test_links_work_with_non_ascii_paths(self) -> None:
        real = _write_skill(self.root / "技能库" / "演示", "中文路径")
        node = self.root / "节点" / "中转"
        node.mkdir(parents=True)

        plan = shared_files.plan_shared_skills(
            {"演示": real}, [("node:中转", node)], {}
        )
        shared_files.apply_shared_skills(plan)
        again = shared_files.plan_shared_skills(
            {"演示": real}, [("node:中转", node)], plan.managed
        )

        self.assertEqual(_read_skill(node / "skills" / "演示"), "中文路径")
        self.assertEqual((again.links, again.relinks, again.unlinks), ((), (), ()))
        self.assertEqual(again.conflicts, {})

    def test_apply_accepts_a_link_another_launch_just_created(self) -> None:
        real = _write_skill(self.store / "alpha", "alpha body")
        plan = self._plan({"alpha": real})
        shared_files.create_directory_link(self.node / "skills" / "alpha", real)

        shared_files.apply_shared_skills(plan)

        self.assertEqual(_read_skill(self.node / "skills" / "alpha"), "alpha body")

    def test_apply_fails_when_something_else_took_the_link_path(self) -> None:
        real = _write_skill(self.store / "alpha")
        plan = self._plan({"alpha": real})
        _write_skill(self.node / "skills" / "alpha", "appeared meanwhile")

        with self.assertRaises(OSError):
            shared_files.apply_shared_skills(plan)

        self.assertEqual(
            _read_skill(self.node / "skills" / "alpha"), "appeared meanwhile"
        )

    def test_entries_for_unknown_nodes_pass_through(self) -> None:
        alpha = _write_skill(self.store / "alpha")
        ghost_link = self.root / "nodes" / "ghost" / "skills" / "alpha"
        shared_files.create_directory_link(ghost_link, alpha)
        managed = {shared_files.path_key(ghost_link): str(alpha)}

        off = self._plan(None, managed)
        shared_files.apply_shared_skills(off)

        self.assertEqual(off.unlinks, ())
        self.assertEqual(off.managed, managed)
        self.assertTrue(os.path.lexists(ghost_link))


class ApiClaudeSharedFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        if not _links_supported(self.root):
            self.skipTest("directory links are not available here")
        self.nodes_root = self.root / ".apiclaude"
        self.account = self.root / ".claude"
        self.account.mkdir()
        (self.account / "CLAUDE.md").write_bytes(b"# rules v1\r\n")
        _write_skill(self.account / "skills" / "alpha", "alpha v1")
        self.relay = self.nodes_root / "nodes" / "relay"
        self.bridge = self.nodes_root / "nodes" / "bridge"
        self.config = {
            "nodes": {
                "relay": {"isolation": "isolated", "home": "nodes/relay"},
                "bridge": {
                    "type": "codex_bridge",
                    "isolation": "isolated",
                    "home": "nodes/bridge",
                },
                "shared": {"type": "anthropic"},
            },
            "current": "relay",
        }
        for name, value in {
            "HOME": self.root,
            "CLAUDE_NODES_ROOT": self.nodes_root,
            "CLAUDE_DESKTOP_DATA_ROOT": self.root / "desktop",
        }.items():
            context = patch.object(apiagent, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(apiagent, "load_claude_config", return_value=self.config)
        context.start()
        self.addCleanup(context.stop)

    def _shared(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = apiagent.claude_shared_main(list(args))
        return code, output.getvalue()

    def _state(self) -> dict:
        path = self.nodes_root / "shared-files.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _refresh(self, name: str = "relay") -> tuple[bool, str]:
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            ok = apiagent.refresh_claude_shared(self.config, name)
        return ok, output.getvalue()

    def test_enable_shares_instructions_and_skills_with_isolated_nodes(self) -> None:
        code, output = self._shared("enable", "--account", "--instructions", "--skills")

        self.assertEqual(code, 0, output)
        for home in (self.relay, self.bridge):
            self.assertEqual((home / "CLAUDE.md").read_bytes(), b"# rules v1\r\n")
            self.assertEqual(_read_skill(home / "skills" / "alpha"), "alpha v1")
            self.assertIsNotNone(
                shared_files.directory_link_target(home / "skills" / "alpha")
            )
        state = self._state()
        self.assertTrue(state["accountInstructionsEnabled"])
        self.assertTrue(state["accountSkillsEnabled"])
        self.assertEqual(len(state["managedInstructions"]), 2)
        self.assertEqual(len(state["managedSkills"]), 2)
        self.assertFalse((self.nodes_root / "shared-mcp.json").exists())
        self.assertFalse((self.nodes_root / "nodes" / "shared").exists())

    def test_plain_enable_still_shares_only_mcp(self) -> None:
        (self.root / ".claude.json").write_text(
            json.dumps({"mcpServers": {"search": {"command": "node"}}}),
            encoding="utf-8",
        )

        code, output = self._shared("enable", "--account")

        self.assertEqual(code, 0, output)
        mcp_state = json.loads(
            (self.nodes_root / "shared-mcp.json").read_text(encoding="utf-8")
        )
        self.assertTrue(mcp_state["accountMcpEnabled"])
        self.assertFalse((self.nodes_root / "shared-files.json").exists())
        self.assertFalse((self.relay / "CLAUDE.md").exists())
        self.assertFalse((self.relay / "skills").exists())

    def test_enable_all_covers_every_component(self) -> None:
        (self.root / ".claude.json").write_text(
            json.dumps({"mcpServers": {"search": {"command": "node"}}}),
            encoding="utf-8",
        )

        code, output = self._shared("enable", "--account", "--all")

        self.assertEqual(code, 0, output)
        relay_config = json.loads(
            (self.relay / ".claude.json").read_text(encoding="utf-8")
        )
        self.assertIn("search", relay_config["mcpServers"])
        self.assertEqual((self.relay / "CLAUDE.md").read_bytes(), b"# rules v1\r\n")
        self.assertEqual(_read_skill(self.relay / "skills" / "alpha"), "alpha v1")

    def test_enable_still_requires_account_authorization(self) -> None:
        code, output = self._shared("enable", "--instructions", "--skills")

        self.assertEqual(code, 1)
        self.assertIn("requires --account", output)
        self.assertFalse((self.relay / "CLAUDE.md").exists())
        self.assertFalse((self.nodes_root / "shared-files.json").exists())

    def test_dry_run_reports_without_changing_anything(self) -> None:
        code, output = self._shared(
            "enable", "--account", "--instructions", "--skills", "--dry-run"
        )

        self.assertEqual(code, 0, output)
        self.assertIn("Would update instruction copies: node:relay, node:bridge", output)
        self.assertIn("node:relay: alpha", output)
        self.assertFalse(self.relay.exists())
        self.assertFalse((self.nodes_root / "shared-files.json").exists())
        self.assertFalse((self.nodes_root / "shared-files-backups").exists())

    def test_launch_refresh_follows_account_changes(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")
        (self.account / "CLAUDE.md").write_bytes(b"# rules v2\r\n")
        _write_skill(self.account / "skills" / "beta", "beta v1")
        (self.bridge / "CLAUDE.md").write_bytes(b"edited inside the node")

        ok, output = self._refresh()

        self.assertTrue(ok, output)
        self.assertEqual((self.relay / "CLAUDE.md").read_bytes(), b"# rules v2\r\n")
        self.assertEqual(
            (self.bridge / "CLAUDE.md").read_bytes(), b"edited inside the node"
        )
        self.assertEqual(_read_skill(self.relay / "skills" / "beta"), "beta v1")
        backups = list((self.nodes_root / "shared-files-backups").glob("*/*.CLAUDE.md"))
        self.assertEqual([path.read_bytes() for path in backups], [b"# rules v1\r\n"])

    def test_refresh_without_opt_in_leaves_nodes_alone(self) -> None:
        self.relay.mkdir(parents=True)

        ok, output = self._refresh()

        self.assertTrue(ok, output)
        self.assertEqual(list(self.relay.iterdir()), [])
        self.assertFalse((self.nodes_root / "shared-files.json").exists())

    def test_unchanged_refresh_does_not_rewrite_state(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")
        state_path = self.nodes_root / "shared-files.json"
        before = state_path.read_bytes()

        with patch.object(
            apiagent,
            "write_json_atomic",
            side_effect=AssertionError("state was rewritten"),
        ):
            ok, output = self._refresh()

        self.assertTrue(ok, output)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertFalse((self.nodes_root / "shared-files-backups").exists())

    def test_missing_account_sources_warn_and_keep_node_content(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")
        (self.account / "CLAUDE.md").unlink()
        (self.account / "skills").rename(self.root / "skills-moved-away")

        ok, output = self._refresh()

        self.assertTrue(ok, output)
        self.assertIn("account instructions were not found", output)
        self.assertIn("account skills were not found", output)
        self.assertEqual((self.relay / "CLAUDE.md").read_bytes(), b"# rules v1\r\n")
        self.assertTrue(os.path.lexists(self.relay / "skills" / "alpha"))
        self.assertEqual(len(self._state()["managedSkills"]), 2)

    def test_disable_removes_managed_copies_and_links_only(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")
        _write_skill(self.relay / "skills" / "local-only", "node skill")
        (self.bridge / "CLAUDE.md").write_bytes(b"edited inside the node")

        code, output = self._shared("disable", "--instructions", "--skills")

        self.assertEqual(code, 0, output)
        self.assertFalse((self.relay / "CLAUDE.md").exists())
        self.assertEqual(
            (self.bridge / "CLAUDE.md").read_bytes(), b"edited inside the node"
        )
        self.assertIn("Conflict in 'node:bridge' (kept target-local): CLAUDE.md", output)
        self.assertFalse(os.path.lexists(self.relay / "skills" / "alpha"))
        self.assertEqual(_read_skill(self.relay / "skills" / "local-only"), "node skill")
        self.assertEqual(_read_skill(self.account / "skills" / "alpha"), "alpha v1")
        state = self._state()
        self.assertFalse(state["accountInstructionsEnabled"])
        self.assertFalse(state["accountSkillsEnabled"])
        self.assertEqual(state["managedInstructions"], {})
        self.assertEqual(state["managedSkills"], {})

    def test_plain_disable_leaves_instructions_and_skills_shared(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")

        code, output = self._shared("disable")

        self.assertEqual(code, 0, output)
        self.assertEqual((self.relay / "CLAUDE.md").read_bytes(), b"# rules v1\r\n")
        self.assertTrue(self._state()["accountInstructionsEnabled"])
        self.assertTrue(self._state()["accountSkillsEnabled"])

    def test_partial_config_cannot_touch_other_nodes(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")
        only_bridge = {
            "nodes": {"bridge": self.config["nodes"]["bridge"]},
            "current": "bridge",
        }

        ok, _report = apiagent.sync_claude_shared_files(
            only_bridge, instructions=False, skills=False
        )

        self.assertTrue(ok)
        self.assertFalse((self.bridge / "CLAUDE.md").exists())
        self.assertEqual((self.relay / "CLAUDE.md").read_bytes(), b"# rules v1\r\n")
        self.assertEqual(_read_skill(self.relay / "skills" / "alpha"), "alpha v1")
        state = self._state()
        self.assertEqual(len(state["managedInstructions"]), 1)
        self.assertEqual(len(state["managedSkills"]), 1)

    def test_status_reports_each_component(self) -> None:
        self._shared("enable", "--account", "--skills")

        code, output = self._shared("status")

        self.assertEqual(code, 0, output)
        self.assertIn("Account MCP sharing: disabled", output)
        self.assertIn("Account instructions sharing: disabled", output)
        self.assertIn("Account skills sharing: enabled", output)
        self.assertIn("Managed skill links: 2", output)

    def test_sync_needs_something_enabled(self) -> None:
        code, output = self._shared("sync")

        self.assertEqual(code, 1)
        self.assertIn("account sharing is disabled", output)

    def test_sync_and_status_reject_component_flags(self) -> None:
        self._shared("enable", "--account", "--instructions")

        for action in ("sync", "status"):
            with self.subTest(action=action):
                code, output = self._shared(action, "--skills")

                self.assertEqual(code, 1)
                self.assertIn("does not take", output)
        self.assertFalse((self.relay / "skills").exists())

    def test_sync_refreshes_enabled_components_on_request(self) -> None:
        self._shared("enable", "--account", "--instructions")
        (self.account / "CLAUDE.md").write_bytes(b"# rules v2\r\n")

        code, output = self._shared("sync")

        self.assertEqual(code, 0, output)
        self.assertIn("Updated instruction copies: node:relay, node:bridge", output)
        self.assertEqual((self.bridge / "CLAUDE.md").read_bytes(), b"# rules v2\r\n")
        self.assertFalse((self.relay / "skills").exists())

    def test_launch_refresh_points_out_what_the_launched_node_keeps(self) -> None:
        _write_skill(self.bridge / "skills" / "alpha", "node-local alpha")
        self._shared("enable", "--account", "--instructions", "--skills")
        (self.bridge / "CLAUDE.md").write_bytes(b"edited inside the node")
        (self.account / "CLAUDE.md").write_bytes(b"# rules v2\r\n")

        bridge_ok, bridge_output = self._refresh("bridge")
        relay_ok, relay_output = self._refresh("relay")

        self.assertTrue(bridge_ok and relay_ok)
        self.assertIn("node 'bridge' keeps its own edited CLAUDE.md", bridge_output)
        self.assertIn("these skills instead of the account version: alpha", bridge_output)
        self.assertEqual(relay_output, "")
        self.assertEqual(_read_skill(self.bridge / "skills" / "alpha"), "node-local alpha")

    def test_launch_continues_when_the_files_refresh_fails(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")
        _write_skill(self.account / "skills" / "beta", "beta v1")

        with patch.object(
            apiagent,
            "apply_claude_shared_skills",
            side_effect=OSError("link is locked"),
        ):
            ok, output = self._refresh()

        self.assertTrue(ok)
        self.assertIn("shared Claude files sync failed: link is locked", output)
        self.assertIn("launching with the node's existing instructions", output)
        self.assertEqual(len(self._state()["managedSkills"]), 2)

        ok, output = self._refresh()

        self.assertTrue(ok)
        self.assertEqual(output, "")
        self.assertEqual(_read_skill(self.relay / "skills" / "beta"), "beta v1")

    def test_launch_refresh_stops_when_the_mcp_refresh_fails(self) -> None:
        with (
            patch.object(apiagent, "sync_claude_shared_mcp", return_value=(False, {})),
            patch.object(apiagent, "sync_claude_shared_files") as files_sync,
        ):
            ok, _output = self._refresh()

        self.assertFalse(ok)
        files_sync.assert_not_called()

    def test_removing_a_node_detaches_it_from_account_skills(self) -> None:
        self._shared("enable", "--account", "--instructions", "--skills")
        archive_root = self.nodes_root / "archived-nodes"

        with (
            patch.object(apiagent, "SECRET_STORE"),
            patch.object(apiagent, "CLAUDE_ARCHIVE_ROOT", archive_root),
            patch.object(apiagent, "save_claude_config"),
            patch("builtins.input", return_value="YES"),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = apiagent.remove_claude_node(self.config, "relay")

        self.assertEqual(code, 0)
        archived = list(archive_root.iterdir())
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0] / "CLAUDE.md").read_bytes(), b"# rules v1\r\n")
        self.assertFalse(os.path.lexists(archived[0] / "skills" / "alpha"))
        self.assertEqual(_read_skill(self.account / "skills" / "alpha"), "alpha v1")
        self.assertEqual(_read_skill(self.bridge / "skills" / "alpha"), "alpha v1")
        state = self._state()
        self.assertEqual(len(state["managedInstructions"]), 1)
        self.assertEqual(len(state["managedSkills"]), 1)

    def test_launch_stops_when_the_refresh_fails(self) -> None:
        with (
            patch.object(apiagent, "refresh_claude_shared", return_value=False) as refresh,
            patch.object(apiagent, "run_command") as run,
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
        ):
            code = apiagent.run_claude_node(self.config, "relay", [])

        self.assertEqual(code, 1)
        refresh.assert_called_once_with(self.config, "relay")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
