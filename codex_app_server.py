#!/usr/bin/env python3
"""Small synchronous client for the local Codex app-server JSONL protocol."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class AppServerError(RuntimeError):
    """Raised when the local app-server cannot complete a request safely."""


@dataclass(frozen=True)
class AppServerCapability:
    available: bool
    codex_version: str
    detail: str


def _clean_process_environment(
    codex_home: Path,
    extra_env: dict[str, str] | None,
) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "CODEX_THREAD_ID",
        "CODEX_PERMISSION_PROFILE",
        "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
        "CODEX_ACCESS_TOKEN",
        "APICODEX_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        environment.pop(name, None)
    environment["CODEX_HOME"] = str(codex_home)
    environment["RUST_LOG"] = "error"
    if extra_env:
        environment.update(extra_env)
    return environment


def detect_fork_path_capability(
    *,
    codex_command: str = "codex",
    codex_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> AppServerCapability:
    """Inspect the installed schema instead of trusting a version number."""

    executable = shutil.which(codex_command)
    if not executable:
        return AppServerCapability(False, "", f"{codex_command!r} was not found")
    environment = os.environ.copy()
    if codex_home is not None:
        environment = _clean_process_environment(codex_home, extra_env)
    try:
        version_result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=timeout,
            check=False,
        )
        version = (version_result.stdout or version_result.stderr).strip()
        with tempfile.TemporaryDirectory(prefix="apicodex-app-schema-") as directory:
            result = subprocess.run(
                [
                    executable,
                    "app-server",
                    "generate-json-schema",
                    "--experimental",
                    "--out",
                    directory,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                timeout=timeout,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                return AppServerCapability(
                    False,
                    version,
                    f"schema generation failed: {detail}",
                )
            schema_path = Path(directory) / "v2" / "ThreadForkParams.json"
            if not schema_path.is_file():
                return AppServerCapability(
                    False,
                    version,
                    "experimental ThreadForkParams schema is missing",
                )
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            properties = schema.get("properties")
            path_schema = properties.get("path") if isinstance(properties, dict) else None
            if not isinstance(path_schema, dict):
                return AppServerCapability(
                    False,
                    version,
                    "thread/fork.path is not supported",
                )
            return AppServerCapability(
                True,
                version,
                "thread/fork.path is supported by the installed schema",
            )
    except (OSError, subprocess.SubprocessError, ValueError, json.JSONDecodeError) as exc:
        return AppServerCapability(False, "", str(exc))


class CodexAppServer:
    """One initialized app-server connection using newline-delimited JSON."""

    def __init__(
        self,
        codex_home: Path,
        *,
        extra_env: dict[str, str] | None = None,
        codex_command: str = "codex",
        timeout: float = 30.0,
    ) -> None:
        self.codex_home = codex_home.resolve()
        self.extra_env = dict(extra_env or {})
        self.codex_command = codex_command
        self.timeout = timeout
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        self._stderr: list[str] = []
        self._next_id = 1
        self.notifications: list[dict[str, Any]] = []
        self._reader_threads: list[threading.Thread] = []

    def __enter__(self) -> "CodexAppServer":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def start(self) -> None:
        if self._process is not None:
            return
        if not self.codex_home.is_dir():
            raise AppServerError(f"CODEX_HOME does not exist: {self.codex_home}")
        executable = shutil.which(self.codex_command)
        if not executable:
            raise AppServerError(f"{self.codex_command!r} was not found")
        environment = _clean_process_environment(
            self.codex_home,
            self.extra_env,
        )
        try:
            self._process = subprocess.Popen(
                [
                    executable,
                    "app-server",
                    "--stdio",
                    "--disable",
                    "apps",
                    "--disable",
                    "plugins",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                    else 0
                ),
            )
        except OSError as exc:
            raise AppServerError(f"failed to start Codex app-server: {exc}") from exc
        stdout_reader = threading.Thread(target=self._read_stdout, daemon=True)
        stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader_threads = [stdout_reader, stderr_reader]
        stdout_reader.start()
        stderr_reader.start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "apicodex_conversation_pool",
                    "title": "ApiCodex Conversation Pool",
                    "version": "1.0.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "item/agentMessage/delta",
                        "item/reasoning/textDelta",
                    ],
                },
            },
        )
        self.notify("initialized", {})

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._messages.put(
                        AppServerError(f"app-server emitted invalid JSON: {exc}")
                    )
                    continue
                if isinstance(message, dict):
                    self._messages.put(message)
            self._messages.put(None)
        except BaseException as exc:  # pragma: no cover - defensive reader boundary
            self._messages.put(exc)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            if line.strip():
                self._stderr.append(line.rstrip())
                if len(self._stderr) > 80:
                    del self._stderr[:20]

    def _send(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.poll() is not None:
            raise AppServerError("Codex app-server is not running")
        try:
            process.stdin.write(
                json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            process.stdin.flush()
        except OSError as exc:
            raise AppServerError(f"failed to write to Codex app-server: {exc}") from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"method": method, "params": params})

    def request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        deferred: list[dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = "\n".join(self._stderr[-8:])
                    suffix = f"\n{detail}" if detail else ""
                    raise AppServerError(
                        f"timed out waiting for {method!r}{suffix}"
                    )
                try:
                    message = self._messages.get(timeout=remaining)
                except queue.Empty as exc:
                    raise AppServerError(
                        f"timed out waiting for {method!r}"
                    ) from exc
                if message is None:
                    detail = "\n".join(self._stderr[-8:])
                    raise AppServerError(
                        f"Codex app-server exited while handling {method!r}: {detail}"
                    )
                if isinstance(message, BaseException):
                    raise AppServerError(str(message))
                if message.get("id") == request_id:
                    if "error" in message:
                        error = message.get("error")
                        raise AppServerError(f"{method} failed: {error}")
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise AppServerError(
                            f"{method} returned an invalid result"
                        )
                    return result
                if "id" in message and "method" in message:
                    raise AppServerError(
                        f"unsupported server request: {message.get('method')}"
                    )
                deferred.append(message)
        finally:
            self.notifications.extend(deferred)

    def list_threads(self, *, limit: int = 100) -> list[dict[str, Any]]:
        result = self.request(
            "thread/list",
            {
                "limit": limit,
                "sourceKinds": [
                    "cli",
                    "vscode",
                    "exec",
                    "appServer",
                    "subAgent",
                    "subAgentReview",
                    "subAgentCompact",
                    "subAgentThreadSpawn",
                    "subAgentOther",
                    "unknown",
                ],
            },
        )
        data = result.get("data")
        if not isinstance(data, list):
            raise AppServerError("thread/list returned an invalid data field")
        return [item for item in data if isinstance(item, dict)]

    def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
    ) -> dict[str, Any]:
        result = self.request(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
        )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError("thread/read returned an invalid thread")
        return thread

    def fork_path(
        self,
        *,
        source_thread_id: str,
        rollout_path: Path,
        model_provider: str,
        cwd: Path,
        model: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "threadId": source_thread_id,
            "path": str(rollout_path.resolve()),
            "modelProvider": model_provider,
            "cwd": str(cwd.resolve()),
            "ephemeral": False,
            "excludeTurns": False,
        }
        if model:
            params["model"] = model
        result = self.request("thread/fork", params)
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise AppServerError("thread/fork returned an invalid thread")
        return thread

    def set_thread_name(self, thread_id: str, name: str) -> None:
        self.request("thread/name/set", {"threadId": thread_id, "name": name})

    def delete_thread(self, thread_id: str) -> None:
        self.request("thread/delete", {"threadId": thread_id})

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        for reader in self._reader_threads:
            reader.join(timeout=1)
        self._reader_threads = []
        for stream in (process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
