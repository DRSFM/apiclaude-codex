from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

import apiagent as api


class ClaudeCliResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = {
            "PATH": str(self.root / "empty-path"),
            "APPDATA": str(self.root / "roaming"),
        }
        for context in (
            patch.object(api, "HOME", self.root),
            patch.dict(os.environ, self.environment, clear=True),
        ):
            context.start()
            self.addCleanup(context.stop)

    def file(self, path: Path, text: str = "synthetic executable") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def launch(self, found=None, args=None, env=None):
        commands = found or {}
        output = io.StringIO()
        with (
            patch.object(api.shutil, "which", side_effect=lambda name, path=None: commands.get(name)),
            patch.object(api.subprocess, "run", return_value=Mock(returncode=0)) as run,
            redirect_stderr(output),
        ):
            code = api.run_command("claude", args or ["--version"], env=env)
        return code, run, output.getvalue()

    def test_native_install_outside_path(self) -> None:
        name = "claude.exe" if os.name == "nt" else "claude"
        target = self.file(self.root / ".local/bin" / name)
        code, run, _ = self.launch()
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(target), "--version"])

    def test_existing_path_entry_keeps_priority(self) -> None:
        preferred = self.file(self.root / "preferred/claude.exe")
        self.file(self.root / ".local/bin/claude.exe")
        code, run, _ = self.launch({"claude": str(preferred)})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(preferred), "--version"])

    @unittest.skipUnless(os.name == "nt", "Windows npm prefix")
    def test_npm_native_install_without_command_shim(self) -> None:
        target = self.file(self.root / "roaming/npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
        code, run, _ = self.launch()
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(target), "--version"])

    def test_custom_npm_prefix_without_shim(self) -> None:
        prefix = self.root / "自定义 npm"
        target = self.file(prefix / "node_modules/@anthropic-ai/claude-code/cli.js")
        node = self.file(prefix / ("node.exe" if os.name == "nt" else "node"))
        code, run, _ = self.launch({"node": str(node)}, env={"NPM_CONFIG_PREFIX": str(prefix)})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(node), str(target), "--version"])

    @unittest.skipUnless(os.name == "nt", "Windows npm prefix")
    def test_path_npm_prefix_without_shim(self) -> None:
        prefix = self.root / "另一个 npm"
        target = self.file(prefix / "node_modules/@anthropic-ai/claude-code/bin/claude.exe")
        code, run, _ = self.launch(env={"PATH": str(prefix)})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(target), "--version"])

    @unittest.skipUnless(os.name == "nt", "Windows native launcher")
    def test_stale_npm_shim_falls_back_to_native_install(self) -> None:
        shim = self.file(self.root / "stale/claude.cmd", '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*')
        target = self.file(self.root / ".local/bin/claude.exe")
        code, run, _ = self.launch({"claude": str(shim)})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(target), "--version"])

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher")
    def test_old_javascript_shim_can_find_new_native_npm_target(self) -> None:
        prefix = self.root / "custom-prefix"
        shim = self.file(prefix / "claude.cmd", '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\cli.js" %*')
        target = self.file(prefix / "node_modules/@anthropic-ai/claude-code/bin/claude.exe")
        code, run, _ = self.launch({"claude": str(shim)})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(target), "--version"])

    @unittest.skipUnless(os.name == "nt", "Windows JavaScript npm launcher")
    def test_npm_javascript_fallback_preserves_arguments_and_environment(self) -> None:
        target = self.file(self.root / "roaming/npm/node_modules/@anthropic-ai/claude-code/cli.js")
        node = self.file(self.root / "node.exe")
        arguments = ["-p", '中文 & | %PATH% !value! "quotes" trailing\\']
        code, run, _ = self.launch({"node": str(node)}, arguments, {"ANTHROPIC_AUTH_TOKEN": "synthetic-secret"})
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(node), str(target), *arguments])
        self.assertEqual(run.call_args.kwargs["env"]["ANTHROPIC_AUTH_TOKEN"], "synthetic-secret")
        self.assertNotIn("synthetic-secret", run.call_args.args[0])

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher")
    def test_javascript_without_node_reports_missing_cli(self) -> None:
        self.file(self.root / "roaming/npm/node_modules/@anthropic-ai/claude-code/cli.js")
        code, run, error = self.launch()
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("command not found: claude", error)

    @unittest.skipUnless(os.name == "nt", "Windows custom wrapper")
    def test_unknown_path_wrapper_remains_authoritative(self) -> None:
        shim = self.file(self.root / "custom/claude.cmd", "@echo CUSTOM %*\n")
        self.file(self.root / ".local/bin/claude.exe")
        code, run, _ = self.launch({"claude": str(shim)})
        self.assertEqual(code, 0)
        self.assertIn(str(shim), run.call_args.args[0])

    def test_missing_install_does_not_launch_or_install_anything(self) -> None:
        code, run, error = self.launch()
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("command not found: claude", error)

    @unittest.skipUnless(os.name == "nt", "Windows native launcher")
    def test_update_uses_the_same_fallback(self) -> None:
        target = self.file(self.root / ".local/bin/claude.exe")
        code, run, _ = self.launch(args=["update"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], [str(target), "update"])
