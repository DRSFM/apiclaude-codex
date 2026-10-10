"""Native Desktop/exec metadata diagnostics, without an HTTP proxy.

Only protocol metadata is appended to the audit file. Diagnostics are consumed
in memory before reaching Desktop logging. Uncertain associations stay unknown.
Interactive TUI invocations retain the original native path without collection.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zlib

EFFORTS = frozenset(("none", "minimal", "low", "medium", "high", "xhigh", "max"))
TARGETS = ("codex_http_client::transport", "codex_api::sse::responses", "tungstenite::protocol")
LOG_FILTER = "error," + ",".join(target + "=trace" for target in TARGETS)
AUDIT_NAME = "native-reasoning.jsonl"
MAX_LINE = 8 * 1024 * 1024
_ENTRY_SOURCE = r'''
using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;
class NativeAuditEntry {
    static string FallbackNative = __FALLBACK_NATIVE__;
    [DllImport("kernel32.dll")] static extern IntPtr GetConsoleWindow();
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attrs, string name);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job, int kind, IntPtr info, uint size);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {
        public long ProcessTime, JobTime; public uint Flags;
        public UIntPtr Minimum, Maximum; public uint Active; public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public BasicLimits Basic;
        public ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    static IntPtr MakeJob() {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        ExtendedLimits info = new ExtendedLimits(); info.Basic.Flags = 0x2000;
        int size = Marshal.SizeOf(info); IntPtr buffer = Marshal.AllocHGlobal(size);
        try {
            Marshal.StructureToPtr(info, buffer, false);
            if (job == IntPtr.Zero || !SetInformationJobObject(job, 9, buffer, (uint)size)) {
                if (job != IntPtr.Zero) CloseHandle(job);
                throw new Exception();
            }
            return job;
        } finally { Marshal.FreeHGlobal(buffer); }
    }
    static string Quote(string value) {
        StringBuilder result = new StringBuilder("\""); int slashes = 0;
        foreach (char ch in value) {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { result.Append('\\', slashes * 2 + 1); result.Append(ch); }
            else { result.Append('\\', slashes); result.Append(ch); }
            slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"'); return result.ToString();
    }
    static void Pump(Stream input, Stream output) {
        byte[] buffer = new byte[4096]; int count;
        while ((count = input.Read(buffer, 0, buffer.Length)) > 0) {
            output.Write(buffer, 0, count); output.Flush();
        }
    }
    static int Main(string[] args) {
        try {
            string python = Environment.GetEnvironmentVariable("APICODEX_NATIVE_AUDIT_PYTHON");
            string script = Environment.GetEnvironmentVariable("APICODEX_NATIVE_AUDIT_SCRIPT");
            bool collected = !String.IsNullOrEmpty(python) && !String.IsNullOrEmpty(script)
                && !String.IsNullOrEmpty(Environment.GetEnvironmentVariable("APICODEX_NATIVE_AUDIT_BINARY"));
            // Desktop also saves CODEX_CLI_PATH in its generated MCP config.
            // Auxiliary clients without the launch context must stay usable.
            StringBuilder arguments = new StringBuilder(collected ? Quote(script) + " --native-entry" : "");
            foreach (string arg in args) arguments.Append(" " + Quote(arg));
            ProcessStartInfo info = new ProcessStartInfo(collected ? python : FallbackNative, arguments.ToString());
            info.UseShellExecute = false;
            if (!collected) {
                info.EnvironmentVariables["CODEX_CLI_PATH"] = FallbackNative;
                info.EnvironmentVariables["RUST_LOG"] = "warn";
            }
            bool detached = GetConsoleWindow() == IntPtr.Zero;
            info.CreateNoWindow = detached;
            info.RedirectStandardInput = detached;
            info.RedirectStandardOutput = detached;
            info.RedirectStandardError = detached;
            IntPtr job = MakeJob();
            try {
                using (Process child = Process.Start(info)) {
                    if (!AssignProcessToJobObject(job, child.Handle)) { child.Kill(); return 1; }
                    Task output = null, error = null;
                    if (detached) {
                        Task.Run(() => { try { Pump(Console.OpenStandardInput(), child.StandardInput.BaseStream); child.StandardInput.Close(); } catch {} });
                        output = Task.Run(() => Pump(child.StandardOutput.BaseStream, Console.OpenStandardOutput()));
                        error = Task.Run(() => Pump(child.StandardError.BaseStream, Console.OpenStandardError()));
                    }
                    child.WaitForExit();
                    // A background helper can retain a pipe handle after the
                    // native client exits. Bound draining, then close the job.
                    if (detached) Task.WaitAll(new Task[] { output, error }, 3000);
                    return child.ExitCode;
                }
            } finally { CloseHandle(job); }
        } catch { Console.Error.WriteLine("Error: native audit entry could not start."); return 1; }
    }
}
'''


def _identifier(value: object) -> str:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", value) else ""


def _profile(value: object) -> str:
    return value if isinstance(value, str) and len(value) <= 128 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value) else ""


def profile_from_home(home: Path) -> str:
    if home.name == ".codex":
        return "官方"
    if home.name == ".codex-api":
        return "default"
    return _profile(("账号/" if home.parent.name == "accounts" else "") + home.name)


def _effort(value: dict) -> dict:
    result = {}
    nested = value.get("reasoning")
    if isinstance(nested, dict) and isinstance(nested.get("effort"), str) and nested["effort"] in EFFORTS:
        result["reasoning"] = {"effort": nested["effort"]}
    flat = value.get("reasoning_effort")
    if isinstance(flat, str) and flat in EFFORTS:
        result["reasoning_effort"] = flat
    return result


def _timestamp(value: object) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo:
                return parsed.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return datetime.now(timezone.utc).isoformat()


def _rust_bytes(literal: str) -> bytes:
    text = literal[2:-1]
    output = bytearray()
    escapes = {"0": 0, "n": 10, "r": 13, "t": 9, '"': 34, "\\": 92}
    index = 0
    while index < len(text):
        if text[index] != "\\":
            output.append(ord(text[index]))
            index += 1
        elif text[index + 1] == "x":
            output.append(int(text[index + 2:index + 4], 16))
            index += 4
        else:
            output.append(escapes[text[index + 1]])
            index += 2
    return bytes(output)


def _metadata_only(row: dict) -> dict:
    """Reapply the field contract at the file boundary, even for future callers."""
    events = {"request_sent", "response_created", "response_completed", "response_failed", "response_incomplete"}
    if row.get("event_type") not in events:
        raise ValueError("Unsupported audit metadata")
    safe = {"schema_version": 1, "event_type": row["event_type"], "timestamp": _timestamp(row.get("timestamp")),
            "protocol": "responses", "observation_boundary": "client_to_provider",
            "collection": "native_diagnostics"}
    for key in ("source_id", "request_id", "attempt_id", "session_id", "turn_id"):
        value = _identifier(row.get(key))
        if value:
            safe[key] = value
    if row.get("client") in ("Desktop", "CLI", "Other"):
        safe["client"] = row["client"]
    profile = row.get("profile")
    if isinstance(profile, str) and len(profile) <= 128 and not any(ord(ch) < 32 for ch in profile):
        safe["profile"] = profile
    for key in ("request", "response"):
        obj = row.get(key)
        if not isinstance(obj, dict):
            continue
        data = _effort(obj)
        for field in (("model",) if key == "request" else ("id", "model")):
            value = _identifier(obj.get(field))
            if value:
                data[field] = value
        if key == "request" and isinstance(obj.get("stream"), bool):
            data["stream"] = obj["stream"]
        safe[key] = data
    return safe


def _redirected(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _append_metadata(path: Path, payload: bytes) -> None:
    """Serialize Windows writers without locking out read-only TPS readers."""
    kernel = None
    mutex = None
    owned = False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = "Local\\ApiCodexNativeAudit-" + hashlib.sha256(os.path.normcase(str(path.absolute())).encode()).hexdigest()
        mutex = kernel.CreateMutexW(None, False, name)
        if not mutex:
            raise OSError("Audit append lock unavailable")
    try:
        if mutex:
            owned = kernel.WaitForSingleObject(mutex, 100) in (0, 0x80)
            if not owned:
                raise OSError("Audit append lock busy")
        if _redirected(path) or _redirected(path.parent):
            raise OSError("Redirected audit directory")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(descriptor, payload) != len(payload):
                raise OSError("Incomplete audit append")
        finally:
            os.close(descriptor)
    finally:
        if mutex:
            if owned:
                kernel.ReleaseMutex(mutex)
            kernel.CloseHandle(mutex)


class MetadataWriter:
    """Bounded asynchronous append; audit failures never stop the client."""
    def __init__(self, home: Path):
        self.path = home / "audits" / AUDIT_NAME
        self.queue: queue.Queue = queue.Queue(maxsize=256)
        self.failed = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def emit(self, row: dict) -> None:
        try:
            self.queue.put_nowait(row)
        except queue.Full:
            self.failed = True

    def _run(self) -> None:
        while True:
            row = self.queue.get()
            if row is None:
                return
            try:
                if _redirected(self.path) or _redirected(self.path.parent):
                    raise OSError("Redirected audit directory")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                payload = (json.dumps(_metadata_only(row), ensure_ascii=True, separators=(",", ":")) + "\n").encode()
                _append_metadata(self.path, payload)
            except (OSError, TypeError, ValueError):
                self.failed = True

    def close(self) -> None:
        try:
            self.queue.put(None, timeout=1)
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                self.failed = True
        except queue.Full:
            self.failed = True


class DiagnosticCollector:
    """Parse native payloads; never keep text in the pending-request state."""
    def __init__(self, emit, *, profile: str, client: str):
        self.emit = emit
        self.profile = _profile(profile)
        self.client = client
        self.source = "native-" + uuid.uuid4().hex
        self.pending: deque = deque()
        self.responses: dict[str, dict] = {}
        self.finished: deque[str] = deque(maxlen=128)
        self.decoder = zlib.decompressobj(-15)
        self.suspended = False

    def reset(self) -> None:
        self.pending.clear()
        self.responses.clear()
        self.decoder = zlib.decompressobj(-15)
        self.suspended = True

    def _base(self, timestamp: str) -> dict:
        return {"schema_version": 1, "timestamp": timestamp, "source_id": self.source,
                "protocol": "responses", "observation_boundary": "client_to_provider",
                "client": self.client, "profile": self.profile}

    def _request(self, obj: dict, timestamp: str, *, websocket: bool) -> None:
        metadata = obj.get("client_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        row = {**self._base(timestamp), "request_id": uuid.uuid4().hex, "attempt_id": uuid.uuid4().hex,
               "session_id": _identifier(metadata.get("session_id")),
               "turn_id": _identifier(metadata.get("turn_id"))}
        request = _effort(obj)
        model = _identifier(obj.get("model"))
        if model:
            request["model"] = model
        request["stream"] = True if websocket else obj.get("stream") is True
        warmup = websocket and obj.get("generate") is False
        entry = {"row": row, "request": request, "warmup": warmup, "ambiguous": self.suspended}
        if self.pending:
            for prior in self.pending:
                prior["ambiguous"] = True
            entry["ambiguous"] = True
        self.pending.append(entry)
        if len(self.pending) > 32:
            self.reset()

    def _response(self, obj: dict, timestamp: str) -> None:
        kind = obj.get("type")
        if kind not in ("response.created", "response.completed", "response.failed", "response.incomplete"):
            return
        response = obj.get("response")
        if not isinstance(response, dict):
            return
        rid = _identifier(response.get("id"))
        if not rid:
            return
        if rid in self.finished:
            return
        entry = self.responses.get(rid)
        if entry is None:
            if len(self.pending) == 1 and not self.pending[0]["ambiguous"]:
                entry = self.pending.popleft()
            else:
                for prior in self.pending:
                    prior["ambiguous"] = True
                entry = {"row": {**self._base(timestamp), "request_id": uuid.uuid4().hex,
                                 "attempt_id": uuid.uuid4().hex},
                         "request": {"stream": True}, "warmup": False, "ambiguous": True}
            self.responses[rid] = entry
            if not entry["warmup"]:
                self.emit({**entry["row"], "event_type": "request_sent", "request": entry["request"],
                           "collection": "native_diagnostics"})
        if not entry["warmup"]:
            safe = _effort(response)
            safe["id"] = rid
            model = _identifier(response.get("model"))
            if model:
                safe["model"] = model
            self.emit({**entry["row"], "timestamp": timestamp, "event_type": kind.replace(".", "_"),
                       "response": safe, "collection": "native_diagnostics"})
        if kind != "response.created":
            self.finished.append(rid)
            self.responses.pop(rid, None)
            if entry["ambiguous"] and self.pending:
                self.pending.popleft()
            if not self.pending and not self.responses:
                self.suspended = False
        if len(self.responses) > 64:
            self.reset()

    def feed(self, line: str) -> bool:
        """Return True when a sensitive diagnostic must not be forwarded."""
        sensitive = any(target in line for target in TARGETS)
        if len(line) > MAX_LINE:
            self.reset()
            return sensitive
        line = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", line)
        try:
            outer = json.loads(line)
            message = outer.get("fields", {}).get("message", "")
            target = outer.get("target", "")
            timestamp = _timestamp(outer.get("timestamp"))
        except (ValueError, AttributeError):
            match = re.match(r"^(\S+)\s+\w+\s+(?:.*?\s)?(codex_http_client::transport|codex_api::sse::responses|tungstenite::protocol): (.*)$", line)
            if not match:
                return sensitive
            timestamp, target, message = match.groups()
            timestamp = _timestamp(timestamp)
        if not isinstance(target, str) or not isinstance(message, str):
            self.reset()
            return sensitive
        if not any(target.startswith(item) for item in TARGETS):
            return sensitive
        try:
            if target == TARGETS[0] and message.startswith("POST to "):
                start = message.find(": {")
                if start >= 0 and re.search(r"/responses(?:[?\s:]|$)", message[:start]):
                    obj = json.JSONDecoder().raw_decode(message[start + 2:])[0]
                    if isinstance(obj, dict):
                        self._request(obj, timestamp, websocket=False)
            elif target == TARGETS[1] and message.startswith("SSE event: "):
                obj = json.JSONDecoder().raw_decode(message[len("SSE event: "):])[0]
                if isinstance(obj, dict):
                    self._response(obj, timestamp)
            elif target.startswith(TARGETS[2]) and message.startswith("Received message "):
                obj = json.JSONDecoder().raw_decode(message[len("Received message "):])[0]
                if isinstance(obj, dict):
                    self._response(obj, timestamp)
            elif target.startswith(TARGETS[2]) and "Sending frame:" in message and "Data(Text)" in message:
                match = re.search(r'payload: (b"(?:[^"\\]|\\.)*")', message)
                if not match:
                    self.reset()
                    return True
                raw = _rust_bytes(match[1])
                if "rsv1: true" in message:
                    raw = self.decoder.decompress(raw + b"\x00\x00\xff\xff", MAX_LINE + 1)
                if len(raw) > MAX_LINE:
                    self.reset()
                    return True
                obj = json.loads(raw)
                if isinstance(obj, dict) and obj.get("type") == "response.create":
                    self._request(obj, timestamp, websocket=True)
        except (ValueError, KeyError, IndexError, TypeError, zlib.error):
            self.reset()
        return True


def _drain(process: subprocess.Popen, collector: DiagnosticCollector) -> None:
    # Read bounded lines even if a native version changes its framing.
    if process.stderr is None:
        return
    dropping = False
    while raw := process.stderr.readline(MAX_LINE + 1):
        if len(raw) > MAX_LINE:
            dropping = not raw.endswith(b"\n")
            collector.reset()
            continue
        if dropping:
            dropping = not raw.endswith(b"\n")
            continue
        line = raw.decode("utf-8", errors="replace")
        try:
            sensitive = collector.feed(line)
        except Exception:
            collector.reset()
            sensitive = any(target in line for target in TARGETS)
        if not sensitive:
            # Native JSON diagnostic messages outside our targets may contain
            # provider error bodies. Never forward their free-form fields.
            try:
                diagnostic = json.loads(line)
                if isinstance(diagnostic, dict) and "target" in diagnostic and "fields" in diagnostic:
                    if diagnostic.get("level") == "ERROR":
                        print("Error: the native client reported a diagnostic error.", file=sys.stderr)
                    continue
            except ValueError:
                pass
            # Baseline errors and UI output retain their existing destination.
            sys.stderr.write(line)
            sys.stderr.flush()


def _native_environment() -> dict[str, str]:
    result = os.environ.copy()
    packaged = result.get("APICODEX_NATIVE_AUDIT_PACKAGED") == "1"
    for name in list(result):
        if name.startswith("APICODEX_NATIVE_AUDIT_"):
            result.pop(name, None)
    result["CODEX_CLI_PATH"] = os.environ["APICODEX_NATIVE_AUDIT_BINARY"]
    if packaged and os.name == "nt":
        # Electron removes this hint for custom executables. Restore it only
        # from this process's OS package identity, never from caller input.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        query = kernel.GetCurrentPackageFamilyName
        query.argtypes = [ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
        query.restype = wintypes.LONG
        length = wintypes.UINT(0)
        if query(ctypes.byref(length), None) == 122:
            family = ctypes.create_unicode_buffer(length.value)
            if query(ctypes.byref(length), family) == 0:
                result["CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY"] = family.value
    return result


def _collected_run(executable: str, args: list[str], *, home: Path, client: str, profile: str) -> int:
    writer = MetadataWriter(home)
    collector = DiagnosticCollector(writer.emit, profile=profile, client=client)
    environment = _native_environment()
    environment.update(RUST_LOG=LOG_FILTER, LOG_FORMAT="json")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if client == "Desktop" else 0
    # Trace filtering belongs to this native process. Shell tools must not
    # pass it to nested clients and capture their raw diagnostics as output.
    child_args = ["-c", 'shell_environment_policy.set.RUST_LOG="warn"', *args]
    process = subprocess.Popen([executable, *child_args], env=environment, stderr=subprocess.PIPE,
                               creationflags=flags)
    reader = threading.Thread(target=_drain, args=(process, collector), daemon=True)
    reader.start()
    try:
        return process.wait()
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=3)
        writer.close()
        if writer.failed:
            print("Warning: some audit metadata could not be saved; client requests continued.", file=sys.stderr)


def native_entry(args: list[str]) -> int:
    binary = os.environ.get("APICODEX_NATIVE_AUDIT_BINARY", "")
    raw_home = os.environ.get("CODEX_HOME", "")
    home = Path(raw_home)
    profile = os.environ.get("APICODEX_NATIVE_AUDIT_PROFILE", "")
    if not binary or not Path(binary).is_file() or not raw_home or (home.exists() and not home.is_dir()):
        print("Error: native audit launch context is unavailable.", file=sys.stderr)
        return 1
    # Management commands and explicit remote connections keep the native path.
    first = _command(args)
    if first == "app-server":
        return _collected_run(binary, args, home=home, client="Desktop", profile=profile)
    if first in ("exec", "e"):
        return _collected_run(binary, args, home=home, client="CLI", profile=profile)
    known_commands = {"login", "logout", "mcp", "plugin", "features", "update", "completion", "debug", "sandbox", "agents", "app"}
    if any(arg in ("--version", "-V", "--help", "-h", "--remote") for arg in args) or first in known_commands:
        return subprocess.run([binary, *args], env=_native_environment()).returncode
    # --remote changes worktree, auth and resume semantics even for loopback.
    # Never insert it into an ordinary TUI invocation merely to obtain logs.
    print("Note: interactive CLI outbound/echo metadata is not collected.", file=sys.stderr)
    return subprocess.run([binary, *args], env=_native_environment()).returncode


def _command(args: list[str]) -> str:
    """Find the first positional argument without interpreting option values."""
    takes_value = {"-c", "--config", "-m", "--model", "-p", "--profile", "-C", "--cd",
                   "--enable", "--disable", "--sandbox", "-s", "--ask-for-approval", "-a",
                   "--add-dir", "--image", "-i", "--local-provider", "--remote-auth-token-env"}
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--":
            return ""
        elif arg in takes_value:
            skip = True
        elif not arg.startswith("-"):
            return arg
    return ""


def prepare_environment(binary: str, home: Path, *, profile: str, cache: Path | None = None) -> dict[str, str]:
    """Build a stable Windows entry; return only process-scoped overrides."""
    if os.name != "nt":
        raise OSError("Native metadata audit currently requires Windows")
    resolved = shutil.which(binary)
    if not resolved or not Path(resolved).is_file():
        raise OSError("Native client was not found")
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise OSError("Windows native entry compiler is unavailable")
    cache = cache or Path(__file__).resolve().parent / "native-audit"
    cache.mkdir(parents=True, exist_ok=True)
    source_text = _ENTRY_SOURCE.replace("__FALLBACK_NATIVE__", json.dumps(str(Path(resolved).resolve())))
    stamp = hashlib.sha256(source_text.encode()).hexdigest()[:16]
    executable = cache / f"codex-audit-{stamp}.exe"
    if not executable.exists():
        source = cache / f"entry-{stamp}.cs"
        source.write_text(source_text, encoding="utf-8")
        temporary = cache / f"entry-{uuid.uuid4().hex}.exe"
        result = subprocess.run([str(compiler), "/nologo", "/optimize+", f"/out:{temporary}", str(source)],
            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=30)
        if result.returncode:
            raise OSError("Native entry compilation failed")
        try:
            temporary.replace(executable)
        except OSError:
            temporary.unlink(missing_ok=True)
            if not executable.exists():
                raise
    return {"CODEX_CLI_PATH": str(executable), "CODEX_HOME": str(home.expanduser().resolve()),
            "APICODEX_NATIVE_AUDIT_BINARY": str(Path(resolved).resolve()),
            "APICODEX_NATIVE_AUDIT_PYTHON": sys.executable,
            "APICODEX_NATIVE_AUDIT_SCRIPT": str(Path(__file__).resolve()),
            "APICODEX_NATIVE_AUDIT_PROFILE": _profile(profile)}


def desktop_environment(executable: Path, home: Path) -> dict[str, str]:
    """Use the selected app's own native binary and sibling sandbox helpers."""
    if os.name != "nt":
        return {}
    try:
        binary = executable.parent / "resources" / "codex.exe"
        environment = prepare_environment(str(binary), home, profile=profile_from_home(home))
        if (executable.parent.parent / "AppxManifest.xml").is_file():
            environment["APICODEX_NATIVE_AUDIT_PACKAGED"] = "1"
        return environment
    except (OSError, subprocess.SubprocessError):
        print("Warning: Desktop audit is unavailable; starting the native client normally.", file=sys.stderr)
        return {}


def exec_environment(executable: str, args: list[str], home: Path) -> tuple[str, dict[str, str]]:
    """Leave interactive and management invocations completely unchanged."""
    if os.name != "nt" or _command(args) not in ("exec", "e") or any(a in ("--help", "-h", "--version", "-V") for a in args):
        return executable, {}
    try:
        environment = prepare_environment(executable, home, profile=profile_from_home(home))
        return environment["CODEX_CLI_PATH"], environment
    except (OSError, subprocess.SubprocessError):
        print("Warning: exec audit is unavailable; starting the native client normally.", file=sys.stderr)
        return executable, {}


def environment_remove() -> tuple[str, ...]:
    return ("CODEX_CLI_PATH",) + tuple(name for name in os.environ if name.upper().startswith("APICODEX_NATIVE_AUDIT_"))


if __name__ == "__main__":
    if sys.argv[1:2] != ["--native-entry"]:
        raise SystemExit(1)
    try:
        raise SystemExit(native_entry(sys.argv[2:]))
    except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
        print("Error: native audit launch could not complete.", file=sys.stderr)
        raise SystemExit(1)
