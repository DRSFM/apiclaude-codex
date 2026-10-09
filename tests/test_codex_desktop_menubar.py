from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import codex_desktop_menubar as menubar


@unittest.skipUnless(sys.platform == "darwin", "macOS menu bar")
class MenuBarTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "relay"
        self.data.mkdir()
        (self.data / "SingletonLock").symlink_to("test-host-123")
        self.executable = self.root / "ChatGPT.app/Contents/MacOS/ChatGPT"
        self.ps = Mock(returncode=0, stdout="Fri Oct  9 20:00:00 2026\n")

    def ready(self, *_args, **_kwargs):
        root = self.root / ".menubar"
        record = json.loads(next(root.glob("*.instance.json")).read_text())
        (root / "status.json").write_text(json.dumps({
            "updatedAt": time.time(), "instances": [{"registrationId": record["registrationId"]}],
        }))
        return Mock()

    def test_helper_environment_excludes_all_credentials_and_parent_runtime(self):
        with patch.dict(os.environ, {"HOME": "/home/test", "USER": "test", "PATH": "/unsafe",
                    "APICODEX_API_KEY": "synthetic-secret", "OPENAI_API_KEY": "synthetic-secret",
                    "CODEX_HOME": "/parent", "CODEX_CLI_PATH": "/parent/cli",
                    "AWS_SECRET_ACCESS_KEY": "synthetic-secret", "NODE_OPTIONS": "--inspect"}, clear=True):
            env = menubar.helper_environment()
        self.assertEqual(env, {"HOME": "/home/test", "USER": "test", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C"})

    def test_registration_contains_only_identity_and_confirms_native_menu(self):
        with patch.object(menubar.subprocess, "run", return_value=self.ps), patch.object(menubar.subprocess, "Popen", side_effect=self.ready) as popen, patch.dict(os.environ, {"APICODEX_API_KEY": "synthetic-secret"}):
            self.assertTrue(menubar.register_instance(self.root, self.data, "\ufeffrelay\n中文\x00", self.executable))
        root = self.root / ".menubar"
        record = json.loads(next(root.glob("*.instance.json")).read_text())
        self.assertEqual(record["name"], "relay 中文")
        self.assertEqual(record["pid"], 123)
        self.assertEqual(record["dataPath"], str(self.data))
        self.assertEqual(set(record), {"schema", "name", "dataPath", "pid", "executable", "lock", "processStart", "registrationId"})
        self.assertNotIn("synthetic-secret", repr(record) + repr(popen.call_args))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_reregistration_renames_one_entry_without_duplicating_it(self):
        with patch.object(menubar.subprocess, "run", return_value=self.ps), patch.object(menubar.subprocess, "Popen", side_effect=self.ready):
            for name in ("old", "new"):
                self.assertTrue(menubar.register_instance(self.root, self.data, name, self.executable))
        records = list((self.root / ".menubar").glob("*.instance.json"))
        self.assertEqual(len(records), 1)
        self.assertEqual(json.loads(records[0].read_text())["name"], "new")

    def test_redirected_or_outside_profile_is_rejected_before_process_lookup(self):
        linked = self.root / "link"
        linked.symlink_to(self.data, target_is_directory=True)
        with patch.object(menubar.subprocess, "run") as run:
            self.assertFalse(menubar.register_instance(self.root, linked, "relay", self.executable))
            self.assertFalse(menubar.register_instance(self.data, self.root, "relay", self.executable))
        run.assert_not_called()

    def test_dead_pid_does_not_register_or_start_helper(self):
        with patch.object(menubar.subprocess, "run", return_value=Mock(returncode=1, stdout="")), patch.object(menubar.subprocess, "Popen") as popen:
            self.assertFalse(menubar.register_instance(self.root, self.data, "relay", self.executable))
        popen.assert_not_called()
        self.assertFalse(list((self.root / ".menubar").glob("*.instance.json")))

    def test_stale_native_status_does_not_report_success(self):
        def stale(*args, **kwargs):
            result = self.ready(*args, **kwargs)
            path = self.root / ".menubar/status.json"
            status = json.loads(path.read_text())
            status["updatedAt"] = time.time() - 60
            path.write_text(json.dumps(status))
            return result
        with patch.object(menubar.subprocess, "run", return_value=self.ps), patch.object(menubar.subprocess, "Popen", side_effect=stale), patch.object(menubar.time, "monotonic", side_effect=[0, 0, 6]), patch.object(menubar.time, "sleep"):
            self.assertFalse(menubar.register_instance(self.root, self.data, "relay", self.executable))

    def test_native_helper_start_failure_is_reported(self):
        with patch.object(menubar.subprocess, "run", return_value=self.ps), patch.object(menubar.subprocess, "Popen", side_effect=OSError("failed")):
            self.assertFalse(menubar.register_instance(self.root, self.data, "relay", self.executable))

    def test_existing_manager_lock_prevents_second_native_menu(self):
        import fcntl
        root = self.root / ".menubar"
        root.mkdir()
        with (root / "manager.lock").open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(menubar.subprocess, "run") as run:
                self.assertEqual(menubar.run_menubar(root), 0)
            run.assert_not_called()

    def test_redirected_manager_lock_is_rejected(self):
        root = self.root / ".menubar"
        root.mkdir()
        target = self.root / "untouched"
        target.write_text("preserve")
        (root / "manager.lock").symlink_to(target)
        with self.assertRaises(OSError), patch.object(menubar.subprocess, "run") as run:
            menubar.run_menubar(root)
        run.assert_not_called()
        self.assertEqual(target.read_text(), "preserve")
