from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import apiagent as api


class CliReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, value in {
            "HOME": self.root,
            "CLAUDE_CONFIG_PATH": self.root / "claude.json",
            "CODEX_HOME": self.root / "codex-api",
            "CODEX_ARCHIVE_ROOT": self.root / "archive",
        }.items():
            context = patch.object(api, name, value)
            context.start()
            self.addCleanup(context.stop)
        context = patch.object(api, "SECRET_STORE", Mock())
        context.start()
        self.addCleanup(context.stop)
        self.config = {
            "nodes": {
                "relay": {
                    "base_url": "https://example.test",
                    "credential_id": "claude:relay",
                    "isolation": "shared",
                    "proxy_enabled": False,
                    "proxy_url": "http://127.0.0.1:7890",
                }
            },
            "current": None,
        }

    def test_failed_claude_write_preserves_previous_config(self) -> None:
        api.save_claude_config(self.config)
        before = api.CLAUDE_CONFIG_PATH.read_bytes()
        updated = copy.deepcopy(self.config)
        updated["current"] = "relay"
        with patch.object(api.os, "replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                api.save_claude_config(updated)
        self.assertEqual(api.CLAUDE_CONFIG_PATH.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_launch_preserves_node_added_after_config_was_loaded(self) -> None:
        api.save_claude_config(self.config)
        launching = api.load_claude_config()
        updating = api.load_claude_config()
        updating["nodes"]["second"] = copy.deepcopy(updating["nodes"]["relay"])
        api.save_claude_config(updating)
        with (
            patch.object(api, "sync_claude_shared_mcp", return_value=(True, {})),
            patch.object(api, "get_claude_secret", return_value="synthetic-placeholder"),
            patch.object(api, "run_command", return_value=0),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(api.run_claude_node(launching, "relay", []), 0)
        saved = json.loads(api.CLAUDE_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertIn("second", saved["nodes"])
        self.assertTrue(saved["nodes"]["relay"]["lastUsedAt"])

    def test_concurrent_launch_metadata_preserves_both_nodes(self) -> None:
        self.config["nodes"]["second"] = copy.deepcopy(self.config["nodes"]["relay"])
        api.save_claude_config(self.config)
        first, second = api.load_claude_config(), api.load_claude_config()
        first["current"] = "relay"
        first["nodes"]["relay"]["lastUsedAt"] = "2026-10-03T01:00:00Z"
        second["current"] = "second"
        second["nodes"]["second"]["lastUsedAt"] = "2026-10-03T01:00:01Z"
        api.save_claude_config(first)
        api.save_claude_config(second)
        saved = api.load_claude_config()
        self.assertEqual(saved["current"], "second")
        self.assertEqual(saved["nodes"]["relay"]["lastUsedAt"], first["nodes"]["relay"]["lastUsedAt"])
        self.assertEqual(saved["nodes"]["second"]["lastUsedAt"], second["nodes"]["second"]["lastUsedAt"])

    def test_conflicting_node_edit_is_rejected_without_overwriting(self) -> None:
        api.save_claude_config(self.config)
        first, second = api.load_claude_config(), api.load_claude_config()
        first["nodes"]["relay"]["base_url"] = "https://first.example.test"
        second["nodes"]["relay"]["base_url"] = "https://second.example.test"
        api.save_claude_config(first)
        with self.assertRaisesRegex(ValueError, "changed"):
            api.save_claude_config(second)
        self.assertEqual(api.load_claude_config()["nodes"]["relay"]["base_url"], "https://first.example.test")

    def test_corrupt_claude_config_is_not_loaded_as_an_empty_registry(self) -> None:
        api.CLAUDE_CONFIG_PATH.write_text('{"nodes":', encoding="utf-8")
        with self.assertRaises(ValueError):
            api.load_claude_config()
        self.assertEqual(api.CLAUDE_CONFIG_PATH.read_text(encoding="utf-8"), '{"nodes":')

    def test_launch_does_not_recreate_a_concurrently_removed_node(self) -> None:
        api.save_claude_config(self.config)
        launching = api.load_claude_config()
        removing = api.load_claude_config()
        del removing["nodes"]["relay"]
        api.save_claude_config(removing)
        launching["nodes"]["relay"]["lastUsedAt"] = "2026-10-03T01:00:00Z"
        with self.assertRaisesRegex(ValueError, "changed"):
            api.save_claude_config(launching)
        self.assertEqual(api.load_claude_config()["nodes"], {})

    def test_codex_archive_failure_retains_registration_and_credential(self) -> None:
        home = api.CODEX_HOME / "profiles" / "relay"
        home.mkdir(parents=True)
        (home / "history.txt").write_text("synthetic history", encoding="utf-8")
        profile = {"id": "relay", "name": "relay", "home": "profiles/relay", "credentialId": "codex:relay"}
        with (
            patch.object(api, "load_codex_profiles", return_value=[profile]),
            patch.object(api, "save_codex_profiles") as save,
            patch.object(api.shutil, "move", side_effect=PermissionError("locked")),
            patch("builtins.input", return_value="YES"),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(api.remove_codex_profile("relay"), 1)
        save.assert_not_called()
        api.SECRET_STORE.clear.assert_not_called()
        self.assertEqual((home / "history.txt").read_text(), "synthetic history")

    def test_codex_registry_failure_restores_archived_directory(self) -> None:
        home = api.CODEX_HOME / "profiles" / "relay"
        home.mkdir(parents=True)
        (home / "history.txt").write_text("synthetic history", encoding="utf-8")
        profile = {"id": "relay", "name": "relay", "home": "profiles/relay", "credentialId": "codex:relay"}
        with (
            patch.object(api, "load_codex_profiles", return_value=[profile]),
            patch.object(api, "save_codex_profiles", side_effect=OSError("disk failure")),
            patch("builtins.input", return_value="YES"),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(api.remove_codex_profile("relay"), 1)
        api.SECRET_STORE.clear.assert_not_called()
        self.assertEqual((home / "history.txt").read_text(), "synthetic history")

    def test_codex_rollback_does_not_merge_into_a_recreated_home(self) -> None:
        home = api.CODEX_HOME / "profiles" / "relay"
        home.mkdir(parents=True)
        (home / "history.txt").write_text("original history", encoding="utf-8")
        profile = {"id": "relay", "name": "relay", "home": "profiles/relay", "credentialId": "codex:relay"}

        def failed_save(profiles):
            home.mkdir()
            (home / "new.txt").write_text("another process", encoding="utf-8")
            raise OSError("disk failure")

        with (
            patch.object(api, "load_codex_profiles", return_value=[profile]),
            patch.object(api, "save_codex_profiles", side_effect=failed_save),
            patch("builtins.input", return_value="YES"),
            redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(api.remove_codex_profile("relay"), 1)
        self.assertEqual(list(home.iterdir()), [home / "new.txt"])
        retained = list(api.CODEX_ARCHIVE_ROOT.iterdir())
        self.assertEqual(len(retained), 1)
        self.assertEqual((retained[0] / "history.txt").read_text(), "original history")
        api.SECRET_STORE.clear.assert_not_called()

    def test_compatibility_wrapper_reports_corrupt_config_without_traceback(self) -> None:
        path = self.root / ".apiclaude_config.json"
        path.write_text('{"nodes":', encoding="utf-8")
        environment = os.environ.copy()
        environment.update(HOME=str(self.root), USERPROFILE=str(self.root))
        result = subprocess.run(
            [sys.executable, str(Path(api.__file__).with_name("apiclaude.py")), "--api-list"],
            env=environment, capture_output=True, text=True, encoding="utf-8",
            timeout=15,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(path.read_text(encoding="utf-8"), '{"nodes":')

    @unittest.skipUnless(os.name == "nt", "Windows batch launcher")
    def test_unknown_batch_shim_cannot_expand_or_execute_prompt_text(self) -> None:
        shim = self.root / "custom.cmd"
        shim.write_text("@echo off\necho STUB:%1\n", encoding="ascii")
        real_run = subprocess.run
        for argument in ("ONE&echo.REVIEW_INJECTED", "%REVIEW_SYNTHETIC_VAR%", 'literal "quotes"', "line\nbreak"):
            with self.subTest(argument=argument):
                outputs = []

                def capture(*args, **kwargs):
                    result = real_run(*args, capture_output=True, **kwargs)
                    outputs.append(result.stdout)
                    return result

                with patch.object(api.subprocess, "run", side_effect=capture), redirect_stderr(io.StringIO()):
                    code = api.run_command(str(shim), [argument], env={"REVIEW_SYNTHETIC_VAR": "REVIEW_EXPANDED"})
                self.assertEqual(code, 1)
                self.assertEqual(outputs, [])

    @unittest.skipUnless(os.name == "nt", "Windows npm shim")
    def test_claude_npm_native_target_receives_literal_arguments(self) -> None:
        shim = self.root / "claude.cmd"
        relative = "node_modules/@anthropic-ai/claude-code/bin/claude.exe"
        executable = self.root / relative
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"synthetic executable")
        shim.write_text(f'@echo off\n"%dp0%\\{relative.replace("/", chr(92))}" %*\n', encoding="utf-8")
        arguments = ["-p", '中文 & | %PATH% !value! "quotes" trailing\\']
        with (
            patch.object(api.shutil, "which", return_value=str(shim)),
            patch.object(api.subprocess, "run", return_value=Mock(returncode=0)) as run,
        ):
            self.assertEqual(api.run_command("claude", arguments), 0)
        self.assertEqual(run.call_args.args[0], [str(executable), *arguments])

    @unittest.skipUnless(os.name == "nt", "Windows npm shim")
    def test_claude_npm_javascript_target_keeps_literal_arguments(self) -> None:
        shim = self.root / "claude.cmd"
        target = self.root / "node_modules/@anthropic-ai/claude-code/cli.js"
        target.parent.mkdir(parents=True)
        target.write_text("// synthetic npm entry", encoding="utf-8")
        runtime = self.root / "node.exe"
        runtime.write_bytes(b"synthetic executable")
        shim.write_text('"%dp0%\\node.exe" "%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*', encoding="utf-8")
        arguments = ["-p", "%PATH% & literal"]
        with (
            patch.object(api.shutil, "which", return_value=str(shim)),
            patch.object(api.subprocess, "run", return_value=Mock(returncode=0)) as run,
        ):
            self.assertEqual(api.run_command("claude", arguments), 0)
        self.assertEqual(run.call_args.args[0], [str(runtime), str(target), *arguments])
