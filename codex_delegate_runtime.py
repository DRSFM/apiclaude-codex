"""Concurrent official app-server transport for delegated tasks (no HTTP proxy)."""
from __future__ import annotations

import json
import queue
import threading
from typing import Any

from codex_app_server import AppServerError, CodexAppServer


class DelegateClient(CodexAppServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self.events: queue.Queue = queue.Queue(maxsize=4096)
        self.dropped_events = 0
        self.dead = False

    def _send(self, message: dict[str, Any]) -> None:
        with self._send_lock:
            super()._send(message)

    def _read_stdout(self) -> None:
        process = self._process
        try:
            for line in process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    # One stray non-JSON line must not end the transport.
                    continue
                if not isinstance(message, dict):
                    continue
                if 'method' in message:
                    # Never let another model approve a request on the user's
                    # behalf. Delegates use a fixed host-selected sandbox.
                    if 'id' in message:
                        self._send({'id': message['id'], 'error': {
                            'code': -32601, 'message': 'Interactive requests require the user; delegation cannot approve them.'}})
                        message = {'method': 'delegate/needsAttention', 'params': {
                            'requestMethod': str(message['method'])}}
                    if message.get('method') in {'item/completed', 'turn/started', 'turn/completed',
                                                 'delegate/needsAttention', 'error'}:
                        self._queue_event(message)
                elif 'id' in message:
                    with self._pending_lock:
                        waiter = self._pending.get(message['id'])
                    if waiter:
                        waiter.put(message)
        except (OSError, ValueError, AppServerError):
            pass
        finally:
            self.dead = True
            with self._pending_lock:
                for waiter in self._pending.values():
                    waiter.put(None)

    def _queue_event(self, message: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(message)
        except queue.Full:
            # Keep the newest events: turn/completed arrives last and decides
            # the task status. Only this reader thread ever puts.
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.dropped_events += 1
            self.events.put_nowait(message)

    def request(self, method: str, params: dict[str, Any], *, timeout=None) -> dict[str, Any]:
        if self.dead:
            raise AppServerError('Official delegated runtime disconnected.')
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue = queue.Queue()
            self._pending[request_id] = waiter
        try:
            self._send({'method': method, 'id': request_id, 'params': params})
            try:
                message = waiter.get(timeout=self.timeout if timeout is None else timeout)
            except queue.Empty:
                raise AppServerError('Official delegated request timed out; inspect task status before retrying.') from None
            if message is None:
                raise AppServerError('Official delegated runtime exited.')
            if 'error' in message:
                # Raw server errors can contain credentials, user prompts and
                # private paths. Report a stable classification instead.
                error = str(message['error']).lower()
                category = next((x for x in ('refresh_token_reused', 'refresh_token_expired',
                    'refresh_token_invalidated', 'invalid_grant', 'rate_limit', 'model_not_found') if x in error), 'request_failed')
                raise AppServerError(f'Official {method} failed ({category}); no account fallback was attempted.')
            result = message.get('result')
            if not isinstance(result, dict):
                raise AppServerError('Invalid official delegated response.')
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def drain(self) -> list[dict[str, Any]]:
        result = []
        while True:
            try:
                result.append(self.events.get_nowait())
            except queue.Empty:
                return result
