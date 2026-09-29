from __future__ import annotations

import subprocess
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

import apiagent
import codex_desktop_windows as desktop_windows


class DesktopWindowTests(unittest.TestCase):
    def test_packaged_launch_keeps_filtered_environment_and_never_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "app" / "ChatGPT.exe"
            executable.parent.mkdir()
            executable.touch()
            (root / "AppxManifest.xml").touch()
            for result in (0, 1):
                with (
                    self.subTest(result=result),
                    patch.object(desktop_windows.os, "name", "nt"),
                    patch.dict(os.environ, {"OPENAI_API_KEY": "parent-secret", "CODEX_HOME": "parent"}),
                    patch.object(apiagent, "start_packaged_codex_desktop", return_value=result) as start,
                    patch.object(apiagent.subprocess, "Popen") as direct,
                ):
                    self.assertEqual(apiagent.start_detached_process(
                        str(executable), ["--user-data-dir=isolated 中文"],
                        env={"CODEX_HOME": "target", "APICODEX_API_KEY": "synthetic-api-key"},
                        env_remove=("OPENAI_API_KEY", "CODEX_HOME"),
                    ), result)
                direct.assert_not_called()
                environment = start.call_args.args[2]
                self.assertEqual(environment["CODEX_HOME"], "target")
                self.assertEqual(environment["APICODEX_API_KEY"], "synthetic-api-key")
                self.assertNotIn("OPENAI_API_KEY", environment)

    def test_unpacked_override_retains_direct_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "ChatGPT.exe"
            executable.touch()
            with (
                patch.object(apiagent, "start_packaged_codex_desktop") as packaged,
                patch.object(apiagent.subprocess, "Popen") as direct,
            ):
                self.assertEqual(apiagent.start_detached_process(str(executable), []), 0)
            packaged.assert_not_called()
            direct.assert_called_once()

    def test_pipe_carries_environment_without_command_line_secrets(self) -> None:
        server, endpoint = MagicMock(), MagicMock()
        server.poll.return_value = True
        server.recv_bytes.side_effect = [b'{"ready":true}', b'{"started":true,"pid":123}']
        endpoint.fileno.return_value = 88
        env = {"CODEX_HOME": "isolated 中文", "APICODEX_API_KEY": "synthetic-secret"}
        with (
            patch("multiprocessing.connection.Pipe", return_value=(server, endpoint)),
            patch.object(desktop_windows.shutil, "which", return_value="pwsh.exe"),
            patch.object(Path, "is_file", return_value=True),
            patch.object(desktop_windows.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run,
            patch.object(desktop_windows, "ensure_desktop_tray", return_value=True),
        ):
            self.assertEqual(desktop_windows.start_packaged_codex_desktop(Path("ChatGPT.exe"), ["--test"], env), 0)
        self.assertNotIn("synthetic-secret", str(run.call_args.args))
        self.assertNotIn("synthetic-secret", run.call_args.kwargs["env"]["APICODEX_PACKAGE_HELPER_ARGS"])
        payload = json.loads(server.send_bytes.call_args.args[0])
        self.assertEqual(payload["env"], env)
        self.assertEqual(payload["command"], ["ChatGPT.exe", "--test"])
        server.close.assert_called_once()
        self.assertTrue(endpoint.close.called)

    def test_activation_and_handshake_failures_close_pipe_without_disclosing_payload(self) -> None:
        for exit_code, ready, reply in ((1, True, b'{}'), (0, False, b'{}'),
                                        (0, True, b'invalid'), (0, True, b'{"started":false}')):
            server, endpoint = MagicMock(), MagicMock()
            server.poll.return_value = ready
            server.recv_bytes.side_effect = [b'{"ready":true}', reply]
            endpoint.fileno.return_value = 88
            output = io.StringIO()
            with (
                self.subTest(exit_code=exit_code, ready=ready, reply=reply),
                redirect_stderr(output),
                patch("multiprocessing.connection.Pipe", return_value=(server, endpoint)),
                patch.object(desktop_windows.shutil, "which", return_value="pwsh.exe"),
                patch.object(Path, "is_file", return_value=True),
                patch.object(desktop_windows.subprocess, "run", return_value=subprocess.CompletedProcess([], exit_code)),
            ):
                self.assertEqual(desktop_windows.start_packaged_codex_desktop(
                    Path("ChatGPT.exe"), [], {"APICODEX_API_KEY": "synthetic-secret"}), 1)
            self.assertNotIn("synthetic-secret", output.getvalue())
            server.close.assert_called_once()
            self.assertTrue(endpoint.close.called)

    @unittest.skipUnless(os.name == "nt", "Windows process identity")
    def test_identity_mismatch_terminates_only_the_new_child(self) -> None:
        child = MagicMock()
        with (
            patch.object(desktop_windows, "_package_name", side_effect=["expected", "wrong"]),
            patch.object(desktop_windows.subprocess, "Popen", return_value=child),
        ):
            with self.assertRaises(OSError):
                desktop_windows._spawn_packaged_child({"command": ["ChatGPT.exe"], "env": {}, "cwd": "."})
        child.terminate.assert_called_once()
        child.wait.assert_called_once()

    @unittest.skipUnless(os.name == "nt" and os.environ.get("APICODEX_TEST_NATIVE_PACKAGE") == "1",
                         "opt-in installed Windows package probe")
    def test_native_package_environment_arguments_and_working_directory(self) -> None:
        executable = apiagent.find_codex_desktop_executable()
        self.assertIsNotNone(executable)
        with tempfile.TemporaryDirectory(prefix="apicodex package 中文 ") as directory:
            root = Path(directory)
            script, result = root / "probe.py", root / "result.json"
            script.write_text(
                "import json,os,sys\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[2])\n"
                "from codex_desktop_windows import _package_name\n"
                "Path(sys.argv[1]).write_text(json.dumps({'package':_package_name(),"
                "'home':os.environ.get('CODEX_HOME'),'key':os.environ.get('APICODEX_API_KEY'),"
                "'excluded':os.environ.get('APICODEX_PROBE_EXCLUDED'),'cwd':os.getcwd(),"
                "'args':sys.argv[3:]},ensure_ascii=True),encoding='utf8')\n", encoding="utf-8")
            env = os.environ.copy()
            env.update({"CODEX_HOME": str(root / "isolated"), "APICODEX_API_KEY": "synthetic-only"})
            env.pop("APICODEX_PROBE_EXCLUDED", None)
            arguments = ["spaces 中文", 'literal "quotes"', "trailing\\", "$(no-execution)"]
            with patch.dict(os.environ, {"APICODEX_PROBE_EXCLUDED": "parent-only"}):
                code = desktop_windows._start_in_package(executable, [
                    sys.executable, str(script), str(result), str(Path(desktop_windows.__file__).parent),
                    *arguments,
                ], env)
            self.assertEqual(code, 0)
            deadline = time.monotonic() + 10
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            data = json.loads(result.read_text(encoding="utf-8"))
            self.assertTrue(data["package"].startswith("OpenAI.Codex_"))
            self.assertEqual(data["home"], str(root / "isolated"))
            self.assertEqual(data["key"], "synthetic-only")
            self.assertIsNone(data["excluded"])
            self.assertEqual(data["cwd"], os.getcwd())
            self.assertEqual(data["args"], arguments)

    def test_label_uses_environment_not_command_line_for_profile_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "desktop profile"
            profile.mkdir()
            executable = root / "ChatGPT.exe"
            executable.write_bytes(b"test")
            completed = subprocess.CompletedProcess([], 0)
            with (
                patch.object(desktop_windows.os, "name", "nt"),
                patch.object(desktop_windows.shutil, "which", return_value="pwsh.exe"),
                patch.object(desktop_windows.subprocess, "run", return_value=completed) as run,
            ):
                result = desktop_windows.label_codex_desktop_window(
                    profile,
                    "  My\nProfile  ",
                    executable,
                    timeout_seconds=2,
                )

            self.assertTrue(result)
            command = run.call_args.args[0]
            self.assertNotIn(str(profile.resolve()), " ".join(command))
            self.assertNotIn("My Profile", " ".join(command))
            environment = run.call_args.kwargs["env"]
            self.assertEqual(
                environment["APICODEX_DESKTOP_PROFILE_PATH"],
                str(profile.resolve()),
            )
            self.assertEqual(
                environment["APICODEX_DESKTOP_WINDOW_TITLE"],
                "ChatGPT (My Profile)",
            )
            self.assertNotIn("APICODEX_API_KEY", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)

    def test_label_fails_closed_for_missing_inputs_or_helper_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "profile"
            profile.mkdir()
            executable = root / "ChatGPT.exe"
            executable.write_bytes(b"test")
            with patch.object(desktop_windows.os, "name", "nt"):
                self.assertFalse(
                    desktop_windows.label_codex_desktop_window(
                        profile,
                        "",
                        executable,
                    )
                )
                with (
                    patch.object(desktop_windows.shutil, "which", return_value="pwsh.exe"),
                    patch.object(
                        desktop_windows.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 3),
                    ),
                ):
                    self.assertFalse(
                        desktop_windows.label_codex_desktop_window(
                            profile,
                            "relay",
                            executable,
                            timeout_seconds=1,
                        )
                    )


if __name__ == "__main__":
    unittest.main()
