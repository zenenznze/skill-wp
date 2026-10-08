#!/usr/bin/env python3
"""Minimal line-delimited JSON-RPC client for Codex App Server."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable


class AppServerError(RuntimeError):
    """Base error for App Server transport failures."""


class AppServerStopped(AppServerError):
    """Raised when the App Server exits before the requested operation completes."""


class AppServerRequestTimeout(AppServerError):
    """Raised when a JSON-RPC response or event does not arrive in time."""


class AppServerRpcError(AppServerError):
    """Raised for a JSON-RPC error response."""

    def __init__(self, method: str, error: Any) -> None:
        super().__init__(f"{method} failed: {error}")
        self.method = method
        self.error = error


_STOPPED = object()
ServerRequestHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


class CodexAppServer:
    """Own one App Server process and correlate requests with JSONL responses."""

    def __init__(
        self,
        command: list[str],
        cwd: Path,
        event_log: Path,
        stderr_log: Path,
        request_handler: ServerRequestHandler,
        environment: dict[str, str] | None = None,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.event_log = event_log
        self.stderr_log = stderr_log
        self.request_handler = request_handler
        self.environment = environment
        self.process: subprocess.Popen[str] | None = None
        self._event_handle: Any = None
        self._stderr_handle: Any = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[Any]] = {}
        self._events: queue.Queue[Any] = queue.Queue()
        self._next_id = 1
        self._stopped = threading.Event()

    def start(self) -> None:
        if self.process is not None:
            raise AppServerError("App Server is already started")
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        self.stderr_log.parent.mkdir(parents=True, exist_ok=True)
        self._event_handle = self.event_log.open("a", encoding="utf-8")
        self._stderr_handle = self.stderr_log.open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                env=self.environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr_handle,
                text=True,
                bufsize=1,
            )
        except OSError:
            self._event_handle.close()
            self._stderr_handle.close()
            self._event_handle = None
            self._stderr_handle = None
            raise
        self._record("process", {"event": "started", "pid": self.process.pid})
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _record(self, direction: str, message: Any) -> None:
        if self._event_handle is None:
            return
        audited_message = self._redact_provider_fields(message)
        if isinstance(audited_message, dict) and audited_message.get("method") in {
            "thread/start",
            "thread/resume",
        }:
            params = audited_message.get("params")
            if isinstance(params, dict) and "config" in params:
                audited_params = dict(params)
                audited_params["config"] = "<redacted-thread-config>"
                audited_message["params"] = audited_params
        entry = {
            "timestamp": time.time(),
            "direction": direction,
            "message": audited_message,
        }
        with self._log_lock:
            self._event_handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._event_handle.flush()

    @classmethod
    def _redact_provider_fields(cls, value: Any) -> Any:
        provider_keys = {"modelProvider", "model_provider"}
        if isinstance(value, dict):
            return {
                key: "<redacted-model-provider>"
                if key in provider_keys
                else cls._redact_provider_fields(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact_provider_fields(item) for item in value]
        return value

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise AppServerStopped("App Server is not running")
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(payload + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise AppServerStopped(f"App Server stdin closed: {exc}") from exc
        self._record("outbound", message)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params")
        try:
            if not isinstance(method, str) or not isinstance(params, dict):
                raise ValueError("server request requires string method and object params")
            result = self.request_handler(method, params)
            self._send({"id": request_id, "result": result})
        except Exception as exc:
            try:
                self._send(
                    {
                        "id": request_id,
                        "error": {"code": -32603, "message": str(exc)},
                    }
                )
            except AppServerError:
                pass

    def _read_loop(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._record("invalid-inbound", {"line": line, "error": str(exc)})
                    continue
                self._record("inbound", message)
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" in message:
                    self._handle_server_request(message)
                    continue
                if "id" in message:
                    with self._pending_lock:
                        waiter = self._pending.get(message["id"])
                    if waiter is not None:
                        waiter.put(message)
                    continue
                if "method" in message:
                    self._events.put(message)
        finally:
            return_code = process.poll()
            if return_code is None:
                try:
                    return_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    return_code = None
            self._record("process", {"event": "stopped", "return_code": return_code})
            self._stopped.set()
            with self._pending_lock:
                waiters = list(self._pending.values())
            for waiter in waiters:
                waiter.put(_STOPPED)
            self._events.put(_STOPPED)

    def request(
        self, method: str, params: dict[str, Any], timeout: float = 30
    ) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("request timeout must be positive")
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
            waiter: queue.Queue[Any] = queue.Queue(maxsize=1)
            self._pending[request_id] = waiter
        try:
            self._send({"id": request_id, "method": method, "params": params})
            try:
                response = waiter.get(timeout=timeout)
            except queue.Empty as exc:
                raise AppServerRequestTimeout(
                    f"timed out waiting for {method} response"
                ) from exc
            if response is _STOPPED:
                raise AppServerStopped(f"App Server stopped during {method}")
            if "error" in response:
                raise AppServerRpcError(method, response["error"])
            result = response.get("result")
            if not isinstance(result, dict):
                raise AppServerError(f"{method} returned a non-object result")
            return result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def next_event(self, timeout: float) -> dict[str, Any]:
        if timeout <= 0:
            raise ValueError("event timeout must be positive")
        try:
            event = self._events.get(timeout=timeout)
        except queue.Empty as exc:
            raise AppServerRequestTimeout("timed out waiting for App Server event") from exc
        if event is _STOPPED:
            raise AppServerStopped("App Server stopped while waiting for an event")
        return event

    def close(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
        if self._reader is not None and self._reader.is_alive():
            self._reader.join(timeout=2)
        if self._event_handle is not None:
            self._event_handle.close()
            self._event_handle = None
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None

    @property
    def return_code(self) -> int | None:
        return self.process.poll() if self.process is not None else None


def inherited_environment(**values: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(values)
    return environment
