from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import codex_desktop_windows as desktop_windows


class DesktopWindowTests(unittest.TestCase):
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
