"""Keep independent, official Windows Desktop tray menus without a skin runtime.

The icons belong to the verified Desktop HWNDs. Their existing callback handles
Recent/Open/Exit; this helper never implements Exit or terminates a Desktop.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

SHARED_GUID = uuid.UUID("e5768d8b-6936-4f45-b1ad-4c5fb414cb35")
NAMESPACE = uuid.UUID("aa90a833-672b-4b1e-a13e-ae086bccf6cb")
MUTEX = "Local\\ApiCodex.DesktopTray.v1"
STOP_EVENT = "Local\\ApiCodex.DesktopTray.Stop.v1"
HOST_CLASSES = {"OwlElectron_NotifyIconHostWindow", "Electron_NotifyIconHostWindow"}
SAFE_ENV = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE",
            "LOCALAPPDATA", "APPDATA", "SYSTEMDRIVE"}


@dataclass(frozen=True)
class Host:
    hwnd: int
    pid: int
    started: int
    executable: str
    title: str = "ChatGPT"

    @property
    def guid(self) -> uuid.UUID:
        # Include creation time so PID reuse cannot redirect a previous icon.
        return uuid.uuid5(NAMESPACE, f"{self.pid}:{self.started}")


class TrayBridge:
    def __init__(self, native):
        self.native = native
        self.active: dict[uuid.UUID, Host] = {}

    def synchronize(self, hosts: list[Host]) -> bool:
        desired = {host.guid: host for host in hosts}
        if len(desired) < 2:
            return self.restore(list(desired.values()))
        ensured = []
        for guid, host in desired.items():
            if not self.native.ensure(host, guid, host.title):
                # Keep the working shared icon until ALL replacements exist.
                for added in ensured:
                    if added.guid not in self.active:
                        self.native.delete(added, added.guid)
                return False
            ensured.append(host)
        for guid, old in self.active.items():
            if guid not in desired:
                self.native.delete(old, guid)
        self.active = desired
        for host in hosts:
            self.native.delete(host, SHARED_GUID)
        return True

    def restore(self, hosts: list[Host]) -> bool:
        current = {h.guid: h for h in hosts if self.native.registered(h.guid)}
        if not self.active and not current:
            return True
        if hosts and not self.native.ensure(hosts[0], SHARED_GUID, "ChatGPT"):
            return False
        for guid, host in (self.active | current).items():
            self.native.delete(host, guid)
        self.active.clear()
        return True


class Guid(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def from_uuid(cls, value: uuid.UUID):
        result = cls()
        result.data[:] = value.bytes_le
        return result


class NotifyIconData(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("hwnd", w.HWND), ("id", w.UINT),
                ("flags", w.UINT), ("message", w.UINT), ("icon", w.HICON),
                ("tip", w.WCHAR * 128), ("state", w.DWORD), ("mask", w.DWORD),
                ("info", w.WCHAR * 256), ("version", w.UINT),
                ("info_title", w.WCHAR * 64), ("info_flags", w.DWORD),
                ("guid", Guid), ("balloon", w.HICON)]


class IconIdentifier(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("hwnd", w.HWND), ("id", w.UINT), ("guid", Guid)]


def _function(library, name, result, *arguments):
    function = getattr(library, name)
    function.restype, function.argtypes = result, list(arguments)
    return function


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    _function(kernel, "CloseHandle", w.BOOL, w.HANDLE)
    _function(kernel, "CreateMutexW", w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)
    _function(kernel, "OpenMutexW", w.HANDLE, w.DWORD, w.BOOL, w.LPCWSTR)
    _function(kernel, "ReleaseMutex", w.BOOL, w.HANDLE)
    _function(kernel, "CreateEventW", w.HANDLE, w.LPVOID, w.BOOL, w.BOOL, w.LPCWSTR)
    _function(kernel, "OpenEventW", w.HANDLE, w.DWORD, w.BOOL, w.LPCWSTR)
    _function(kernel, "SetEvent", w.BOOL, w.HANDLE)
    _function(kernel, "WaitForSingleObject", w.DWORD, w.HANDLE, w.DWORD)
    return kernel


class NativeTray:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel = _kernel()
        self.callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        for name, result, args in [
            ("EnumWindows", w.BOOL, [self.callback_type, w.LPARAM]),
            ("GetClassNameW", ctypes.c_int, [w.HWND, w.LPWSTR, ctypes.c_int]),
            ("GetWindowTextW", ctypes.c_int, [w.HWND, w.LPWSTR, ctypes.c_int]),
            ("GetWindowThreadProcessId", w.DWORD, [w.HWND, ctypes.POINTER(w.DWORD)]),
            ("IsWindowVisible", w.BOOL, [w.HWND]),
            ("LoadImageW", w.HANDLE, [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]),
            ("DestroyIcon", w.BOOL, [w.HICON]),
        ]:
            _function(self.user, name, result, *args)
        _function(self.shell, "Shell_NotifyIconW", w.BOOL, w.DWORD, ctypes.POINTER(NotifyIconData))
        _function(self.shell, "Shell_NotifyIconGetRect", w.LONG, ctypes.POINTER(IconIdentifier), ctypes.POINTER(w.RECT))
        _function(self.kernel, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        _function(self.kernel, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD))
        _function(self.kernel, "GetProcessTimes", w.BOOL, w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4))
        _function(self.kernel, "GetPackageFullName", w.LONG, w.HANDLE, ctypes.POINTER(w.UINT), w.LPWSTR)
        _function(self.kernel, "GetPackagePathByFullName", w.LONG, w.LPCWSTR, ctypes.POINTER(w.UINT), w.LPWSTR)

    def process_identity(self, pid: int) -> tuple[str, int] | None:
        handle = self.kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            length = w.UINT(0)
            if self.kernel.GetPackageFullName(handle, ctypes.byref(length), None) != 122:
                return None
            package = ctypes.create_unicode_buffer(length.value)
            if self.kernel.GetPackageFullName(handle, ctypes.byref(length), package) != 0:
                return None
            if not (package.value.startswith("OpenAI.Codex_") and package.value.endswith("_2p2nqsd0c76g0")):
                return None
            length = w.UINT(0)
            if self.kernel.GetPackagePathByFullName(package.value, ctypes.byref(length), None) != 122:
                return None
            root = ctypes.create_unicode_buffer(length.value)
            if self.kernel.GetPackagePathByFullName(package.value, ctypes.byref(length), root) != 0:
                return None
            size = w.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not self.kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
                return None
            expected = os.path.join(root.value, "app", "ChatGPT.exe")
            if os.path.normcase(image.value) != os.path.normcase(expected):
                return None
            times = [w.FILETIME() for _ in range(4)]
            if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            started = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return image.value, started
        finally:
            self.kernel.CloseHandle(handle)

    def window_identity(self, hwnd: int) -> tuple[str, int]:
        name = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, name, len(name))
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return name.value, pid.value

    def hosts(self) -> list[Host]:
        candidates, titles = {}, {}

        @self.callback_type
        def collect(hwnd, _):
            name, pid = self.window_identity(hwnd)
            if name in HOST_CLASSES:
                candidates[pid] = hwnd
            elif name == "Chrome_WidgetWin_1":
                title = ctypes.create_unicode_buffer(256)
                self.user.GetWindowTextW(hwnd, title, len(title))
                if title.value.startswith("ChatGPT"):
                    score = (title.value.startswith("ChatGPT ("), bool(self.user.IsWindowVisible(hwnd)))
                    if pid not in titles or score > titles[pid][0]:
                        titles[pid] = score, title.value
            return True

        if not self.user.EnumWindows(collect, 0):
            raise OSError("Desktop tray enumeration failed")
        result = []
        for pid, hwnd in candidates.items():
            identity = self.process_identity(pid)
            if identity:
                image, started = identity
                title = titles.get(pid, ((), f"ChatGPT (PID {pid})"))[1]
                result.append(Host(hwnd, pid, started, image, title))
        return sorted(result, key=lambda host: host.started)

    def valid(self, host: Host) -> bool:
        name, pid = self.window_identity(host.hwnd)
        return (name in HOST_CLASSES and pid == host.pid
                and self.process_identity(pid) == (host.executable, host.started))

    @staticmethod
    def data(host: Host, guid: uuid.UUID) -> NotifyIconData:
        data = NotifyIconData()
        data.size = ctypes.sizeof(data)
        data.hwnd, data.id, data.flags = host.hwnd, 3, 0x20
        data.guid = Guid.from_uuid(guid)
        return data

    def ensure(self, host: Host, guid: uuid.UUID, title: str) -> bool:
        if not self.valid(host):
            return False
        import winreg
        light = True
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                light = winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] != 0
        except OSError:
            pass
        icon_path = Path(host.executable).parent / "resources" / f"chatgpt-tray-{'light' if light else 'dark'}.ico"
        icon = self.user.LoadImageW(None, str(icon_path), 1, 16, 16, 0x10)
        if not icon:
            return False
        try:
            data = self.data(host, guid)
            data.flags |= 0x01 | 0x02 | 0x04 | 0x80
            data.message, data.icon = 0x8001, icon
            data.tip = "".join(c for c in title if c.isprintable()).encode("utf-16-le")[:254].decode("utf-16-le", "ignore")
            return bool(self.shell.Shell_NotifyIconW(0, ctypes.byref(data))
                        or self.shell.Shell_NotifyIconW(1, ctypes.byref(data)))
        finally:
            self.user.DestroyIcon(icon)

    def delete(self, host: Host, guid: uuid.UUID) -> bool:
        data = self.data(host, guid)
        return bool(self.shell.Shell_NotifyIconW(2, ctypes.byref(data)))

    def registered(self, guid: uuid.UUID) -> bool:
        item = IconIdentifier()
        item.size, item.guid = ctypes.sizeof(item), Guid.from_uuid(guid)
        rect = w.RECT()
        return self.shell.Shell_NotifyIconGetRect(ctypes.byref(item), ctypes.byref(rect)) >= 0


def running() -> bool:
    if os.name != "nt":
        return False
    kernel = _kernel()
    handle = kernel.OpenMutexW(0x100000, False, MUTEX)
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True


def start() -> bool:
    if os.name != "nt":
        return False
    if running():
        return True
    helper = Path(sys.executable).with_name("pythonw.exe")
    if not helper.is_file():
        return False
    # A tray helper does not need provider credentials, tokens or CODEX_HOME.
    environment = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV}
    try:
        child = subprocess.Popen(
            [str(helper), str(Path(__file__).resolve()), "--run"],
            cwd=str(Path(__file__).resolve().parent), env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if running():
                return True
            if child.poll() is not None:
                return False
            time.sleep(0.05)
    except OSError:
        return False
    return False


def stop() -> None:
    kernel = _kernel()
    event = kernel.OpenEventW(0x02, False, STOP_EVENT)
    if event:
        try:
            kernel.SetEvent(event)
        finally:
            kernel.CloseHandle(event)


def run() -> int:
    kernel = _kernel()
    mutex = kernel.CreateMutexW(None, True, MUTEX)
    if not mutex:
        return 1
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(mutex)
        return 0
    event = kernel.CreateEventW(None, True, False, STOP_EVENT)
    bridge = None
    try:
        if not event:
            return 1
        native = NativeTray()
        bridge = TrayBridge(native)
        empty_since = None
        while True:
            hosts = native.hosts()
            bridge.synchronize(hosts)
            if hosts:
                empty_since = None
            elif empty_since is None:
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since >= 30:
                return 0
            if kernel.WaitForSingleObject(event, 3000) == 0:
                return 0
    finally:
        try:
            if bridge:
                bridge.restore(bridge.native.hosts())
        finally:
            if event:
                kernel.CloseHandle(event)
            kernel.ReleaseMutex(mutex)
            kernel.CloseHandle(mutex)


def status() -> dict:
    native = NativeTray()
    return {"running": running(), "shared_icon": native.registered(SHARED_GUID),
            "instances": [{"pid": h.pid, "started": h.started, "hwnd": h.hwnd,
                           "title": h.title, "icon": native.registered(h.guid)}
                          for h in native.hosts()]}


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Windows Desktop tray management requires Windows.")
    if sys.argv[1:] == ["--run"]:
        raise SystemExit(run())
    if sys.argv[1:] == ["--start"]:
        raise SystemExit(0 if start() else 1)
    if sys.argv[1:] == ["--stop"]:
        stop()
    elif sys.argv[1:] == ["--status"]:
        print(json.dumps(status(), ensure_ascii=True))
    else:
        raise SystemExit("Usage: codex_desktop_tray.py --start|--stop|--status")
