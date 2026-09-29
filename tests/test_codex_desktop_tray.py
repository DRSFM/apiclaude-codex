from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

import codex_desktop_tray as tray
import codex_desktop_windows as desktop


class FakeShell:
    def __init__(self):
        self.icons = {tray.SHARED_GUID: None}
        self.fail = set()
        self.calls = []

    def ensure(self, host, guid, title):
        self.calls.append(("ensure", guid, host.hwnd))
        if guid in self.fail:
            return False
        self.icons[guid] = host
        return True

    def delete(self, host, guid):
        self.calls.append(("delete", guid, host.hwnd))
        self.icons.pop(guid, None)
        return True

    def registered(self, guid):
        return guid in self.icons


def host(pid, started=100, hwnd=None):
    return tray.Host(hwnd or pid * 100, pid, started, "official/ChatGPT.exe", f"ChatGPT ({pid})")


def test_independent_official_callbacks_and_partial_failure_keep_working_menu():
    shell = FakeShell()
    bridge = tray.TrayBridge(shell)
    a, b = host(1), host(2)
    shell.fail.add(b.guid)
    assert not bridge.synchronize([a, b])
    assert set(shell.icons) == {tray.SHARED_GUID}
    shell.fail.clear()
    assert bridge.synchronize([a, b])
    assert shell.icons == {a.guid: a, b.guid: b}
    data = tray.NativeTray.data(b, b.guid)
    assert data.hwnd == b.hwnd and data.id == 3
    assert bytes(data.guid.data) == b.guid.bytes_le


def test_lifecycle_recreated_window_pid_reuse_and_single_instance_restore():
    shell = FakeShell()
    bridge = tray.TrayBridge(shell)
    a, b, c = host(1), host(2), host(3)
    assert bridge.synchronize([a, b, c])
    replacement = host(2, hwnd=201)
    assert bridge.synchronize([a, replacement])
    assert shell.icons == {a.guid: a, b.guid: replacement}
    reused = host(2, started=200)
    assert reused.guid != b.guid
    assert bridge.synchronize([a, reused])
    assert b.guid not in shell.icons
    shell.fail.add(tray.SHARED_GUID)
    assert not bridge.synchronize([a])
    assert a.guid in shell.icons
    shell.fail.clear()
    assert bridge.synchronize([a])
    assert shell.icons == {tray.SHARED_GUID: a}
    assert not bridge.active


def test_helper_restart_recovers_single_icon_and_explorer_restart_recovers_all():
    shell = FakeShell()
    a, b = host(1), host(2)
    shell.icons = {a.guid: a}
    bridge = tray.TrayBridge(shell)
    assert bridge.synchronize([a])
    assert shell.icons == {tray.SHARED_GUID: a}
    assert bridge.synchronize([a, b])
    shell.icons.clear()  # Explorer lost its icon registrations.
    assert bridge.synchronize([a, b])
    assert set(shell.icons) == {a.guid, b.guid}
    assert bridge.synchronize([])
    assert not shell.icons


def test_failed_new_instance_does_not_remove_existing_independent_icons():
    shell = FakeShell()
    bridge = tray.TrayBridge(shell)
    a, b, c, d = map(host, range(1, 5))
    assert bridge.synchronize([a, b])
    shell.fail.add(d.guid)
    assert not bridge.synchronize([a, b, c, d])
    assert shell.icons == {a.guid: a, b.guid: b}


def test_host_validation_checks_window_pid_process_birth_and_executable():
    native = object.__new__(tray.NativeTray)
    expected = host(1)
    with patch.object(native, "window_identity", return_value=("OwlElectron_NotifyIconHostWindow", 1)), \
         patch.object(native, "process_identity", return_value=(expected.executable, 100)) as identity:
        assert native.valid(expected)
        identity.return_value = expected.executable, 101
        assert not native.valid(expected)
        identity.return_value = None  # Wrong package / executable / inaccessible.
        assert not native.valid(expected)
    with patch.object(native, "window_identity", return_value=("OtherWindow", 1)), \
         patch.object(native, "process_identity") as identity:
        assert not native.valid(expected)
        identity.assert_not_called()


@pytest.mark.skipif(os.name != "nt", reason="Windows helper process")
def test_helper_receives_no_credentials_and_start_is_idempotent():
    child = MagicMock()
    with patch.object(tray, "running", side_effect=[False, True]), \
         patch.object(Path, "is_file", return_value=True), \
         patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "APICODEX_API_KEY": "secret",
                                 "CODEX_HOME": "private", "PYTHONPATH": "untrusted"}), \
         patch.object(tray.subprocess, "Popen", return_value=child) as spawn:
        assert tray.start()
        environment = spawn.call_args.kwargs["env"]
        assert set(k.upper() for k in environment) <= tray.SAFE_ENV
        assert "secret" not in str(spawn.call_args)
        assert "CODEX_HOME" not in environment and "PYTHONPATH" not in environment
        assert spawn.call_args.args[0][-1] == "--run"
    with patch.object(tray, "running", return_value=True), patch.object(tray.subprocess, "Popen") as spawn:
        assert tray.start()
        spawn.assert_not_called()


def test_only_successful_packaged_launch_enables_tray_and_tray_failure_is_nonfatal():
    for code, enabled in [(0, True), (0, False), (1, False)]:
        with patch.object(desktop, "_start_in_package", return_value=code), \
             patch.object(desktop, "ensure_desktop_tray", return_value=enabled) as enable:
            assert desktop.start_packaged_codex_desktop(Path("ChatGPT.exe"), [], {}) == code
            assert enable.call_count == (1 if code == 0 else 0)
