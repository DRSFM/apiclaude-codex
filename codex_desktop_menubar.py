"""A credential-free native macOS menu for isolated Codex Desktop instances."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid


MENUBAR_SCRIPT = r'''
ObjC.import('AppKit');
ObjC.import('Foundation');
ObjC.bindFunction('kill', ['int', ['int', 'int']]);
function run(argv) {
    const root = argv[0], owner = Number(argv[1]);
    const fm = $.NSFileManager.defaultManager;
    const app = $.NSApplication.sharedApplication;
    app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
    const item = $.NSStatusBar.systemStatusBar.statusItemWithLength($.NSVariableStatusItemLength);
    const menu = $.NSMenu.alloc.initWithTitle($('Codex'));
    const cache = new Map();
    let entries = [], idle = 0, menuOpen = false, lastMenu = '', lastStatus = '';
    function readText(path) {
        const value = $.NSString.stringWithContentsOfFileEncodingError($(path), $.NSUTF8StringEncoding, null);
        return value.isNil() ? null : ObjC.unwrap(value);
    }
    function processStart(pid) {
        const task = $.NSTask.alloc.init, pipe = $.NSPipe.pipe;
        task.launchPath = $('/bin/ps');
        task.arguments = $(['-p', String(pid), '-o', 'lstart=']);
        task.standardOutput = pipe;
        task.standardError = $.NSFileHandle.fileHandleWithNullDevice;
        task.launch;
        const data = pipe.fileHandleForReading.readDataToEndOfFile;
        task.waitUntilExit;
        return ObjC.unwrap($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding)).trim();
    }
    function identityKey(record) {
        return JSON.stringify([record.registrationId, record.pid, record.processStart, record.executable, record.dataPath, record.lock]);
    }
    function matchingApplication(record) {
        if (record.schema !== 1 || !Number.isInteger(record.pid) || record.pid <= 0 ||
            typeof record.dataPath !== 'string' || typeof record.name !== 'string' ||
            typeof record.registrationId !== 'string' || typeof record.processStart !== 'string') return null;
        const lock = fm.destinationOfSymbolicLinkAtPathError($(record.dataPath + '/SingletonLock'), null);
        if (lock.isNil() || ObjC.unwrap(lock) !== record.lock) return null;
        const key = identityKey(record);
        let cached = cache.get(key);
        if (cached === undefined) {
            const target = $.NSRunningApplication.runningApplicationWithProcessIdentifier(record.pid);
            if (target.isNil() || target.terminated) return null;
            cached = null;
            if (!target.executableURL.isNil() &&
                ObjC.unwrap(target.bundleIdentifier) === 'com.openai.codex' &&
                ObjC.unwrap(target.executableURL.path) === record.executable &&
                processStart(record.pid) === record.processStart) cached = target;
            cache.set(key, cached);
        }
        return cached === null || cached.terminated ? null : cached;
    }
    function refreshEntries() {
        const names = ObjC.deepUnwrap(fm.contentsOfDirectoryAtPathError($(root), null)) || [];
        const next = [], ids = new Set();
        for (const filename of names) {
            if (!/^[a-f0-9]{64}\.instance\.json$/.test(filename)) continue;
            try {
                const record = JSON.parse(readText(root + '/' + filename));
                ids.add(identityKey(record));
                const target = matchingApplication(record);
                if (target !== null) next.push({record: record, target: target});
            } catch (_) {}
        }
        entries = next.sort((a, b) => a.record.name.localeCompare(b.record.name));
        for (const key of cache.keys()) if (!ids.has(key)) cache.delete(key);
    }
    function focus(pid) {
        const entry = entries.find(e => e.record.pid === pid);
        if (entry && matchingApplication(entry.record) !== null) {
            entry.target.unhide;
            entry.target.activateWithOptions($.NSApplicationActivateIgnoringOtherApps);
        }
    }
    function rebuildMenu(activePid) {
        menu.removeAllItems;
        const heading = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent($('Codex 实例'), null, $(''));
        heading.enabled = false;
        menu.addItem(heading);
        for (const entry of entries) {
            const row = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent($('(' + entry.record.name + ')'), 'choose:', $(''));
            row.target = delegate;
            row.tag = entry.record.pid;
            row.state = entry.record.pid === activePid ? $.NSControlStateValueOn : $.NSControlStateValueOff;
            menu.addItem(row);
        }
        if (!entries.length) {
            const empty = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent($('没有运行中的节点'), null, $(''));
            empty.enabled = false;
            menu.addItem(empty);
        }
        menu.addItem($.NSMenuItem.separatorItem);
        const hide = $.NSMenuItem.alloc.initWithTitleActionKeyEquivalent($('隐藏菜单栏标识'), 'hide:', $(''));
        hide.target = delegate;
        menu.addItem(hide);
    }
    function tick() {
        if ($.kill(owner, 0) !== 0) { app.terminate(null); return; }
        refreshEntries();
        const foreground = $.NSWorkspace.sharedWorkspace.frontmostApplication;
        const activePid = foreground.isNil() ? 0 : foreground.processIdentifier;
        const current = entries.find(e => e.record.pid === activePid);
        const name = current ? current.record.name : '';
        const shortName = Array.from(name).length > 24 ? Array.from(name).slice(0, 23).join('') + '…' : name;
        item.button.title = $(name ? 'Codex (' + shortName + ')' : 'Codex');
        item.button.toolTip = $(name ? 'Codex (' + name + ') · 点击切换实例' : '点击切换 Codex 实例');
        const signature = JSON.stringify([activePid, entries.map(e => [e.record.pid, e.record.name])]);
        if (!menuOpen && signature !== lastMenu) { rebuildMenu(activePid); lastMenu = signature; }
        const status = {
            ownerPid: owner, updatedAt: Date.now() / 1000,
            title: ObjC.unwrap(item.button.title), activePid: current ? activePid : null,
            instances: entries.map(e => ({pid: e.record.pid, name: e.record.name, registrationId: e.record.registrationId})),
            menuTitles: Array.from({length: menu.numberOfItems}, (_, index) => ObjC.unwrap(menu.itemAtIndex(index).title))
        };
        const statusKey = JSON.stringify([status.title, status.activePid, status.instances, status.menuTitles]);
        if (statusKey !== lastStatus) {
            $(JSON.stringify(status)).writeToFileAtomicallyEncodingError($(root + '/status.json'), true, $.NSUTF8StringEncoding, null);
            lastStatus = statusKey;
        }
        idle = entries.length ? 0 : idle + 1;
        if (idle >= 5) app.terminate(null);
    }
    ObjC.registerSubclass({name: 'ApiCodexMenuDelegate', superclass: 'NSObject', protocols: ['NSMenuDelegate'], methods: {
        'choose:': {types: ['void', ['id']], implementation: function(sender) { focus(Number(sender.tag)); tick(); }},
        'hide:': {types: ['void', ['id']], implementation: function() { app.terminate(null); }},
        'refresh:': {types: ['void', ['id']], implementation: function() { tick(); }},
        'menuWillOpen:': {types: ['void', ['id']], implementation: function() { tick(); menuOpen = true; }},
        'menuDidClose:': {types: ['void', ['id']], implementation: function() { menuOpen = false; }}
    }});
    const delegate = $.ApiCodexMenuDelegate.alloc.init;
    menu.delegate = delegate;
    item.menu = menu;
    const timer = $.NSTimer.scheduledTimerWithTimeIntervalTargetSelectorUserInfoRepeats(1, delegate, 'refresh:', null, true);
    $.NSRunLoop.mainRunLoop.addTimerForMode(timer, $.NSRunLoopCommonModes);
    tick();
    app.run;
}
'''


def helper_environment() -> dict[str, str]:
    """An allowlist keeps credentials and parent runtime settings out of the UI."""
    result = {key: os.environ[key] for key in (
        "HOME", "USER", "LOGNAME", "TMPDIR", "__CF_USER_TEXT_ENCODING",
    ) if key in os.environ}
    result.update(PATH="/usr/bin:/bin:/usr/sbin:/sbin", LANG="C", LC_ALL="C")
    return result


def _private_directory(path: Path) -> None:
    if path.resolve() != path.absolute():
        raise OSError("refusing a redirected menu directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _write_record(root: Path, record: dict) -> None:
    name = hashlib.sha256(record["dataPath"].encode()).hexdigest() + ".instance.json"
    temporary = root / f".{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
        temporary.replace(root / name)
    finally:
        temporary.unlink(missing_ok=True)


def register_instance(data_root: Path, data: Path, name: str, executable: Path) -> bool:
    """Register only non-secret instance identity and confirm the native menu."""
    if sys.platform != "darwin":
        return False
    name = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]", "", " ".join(str(name).split()))[:80]
    if not name or data.resolve() != data.absolute() or not data.is_relative_to(data_root):
        return False
    root = data_root / ".menubar"
    try:
        _private_directory(root)
        lock = os.readlink(data / "SingletonLock")
        pid = int(lock.rsplit("-", 1)[1])
        if pid <= 0:
            return False
        result = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "lstart="], env=helper_environment(),
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode or not result.stdout.strip():
            return False
        record = {"schema": 1, "name": name, "dataPath": str(data), "pid": pid,
                  "executable": str(executable.resolve()), "lock": lock,
                  "processStart": result.stdout.strip(), "registrationId": uuid.uuid4().hex}
        _write_record(root, record)
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--menubar", str(root)],
            env=helper_environment(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                status = json.loads((root / "status.json").read_text())
                if time.time() - status["updatedAt"] < 3 and any(
                    entry.get("registrationId") == record["registrationId"]
                    for entry in status["instances"]
                ):
                    return True
            except (OSError, ValueError, KeyError, TypeError):
                pass
            time.sleep(0.1)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return False


def run_menubar(root: Path) -> int:
    import fcntl

    _private_directory(root)
    descriptor = os.open(root / "manager.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            return subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", "-", str(root), str(os.getpid())],
                input=MENUBAR_SCRIPT, text=True, env=helper_environment(),
            ).returncode
        finally:
            try:
                status = root / "status.json"
                if json.loads(status.read_text()).get("ownerPid") == os.getpid():
                    status.unlink()
            except (OSError, ValueError):
                pass


if __name__ == "__main__":
    if sys.platform == "darwin" and len(sys.argv) == 3 and sys.argv[1] == "--menubar":
        raise SystemExit(run_menubar(Path(sys.argv[2])))
    raise SystemExit(2)
