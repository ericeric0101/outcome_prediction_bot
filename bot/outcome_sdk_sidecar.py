"""Python boundary for the official Outcome TypeScript SDK sidecar.

Execution is disabled by default twice: callers must opt in with
``allow_execution=True`` and the operator must set
``OUTCOME_SDK_EXECUTION_ENABLED=1`` for the sidecar process. Private keys
remain exclusively in the sidecar environment.
"""
from __future__ import annotations

import json
import select
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping


class OutcomeSdkSidecarError(RuntimeError):
    pass


ReadOnlyCommand = Literal["health", "fetch_markets", "fetch_order_book", "fetch_settled_outcome", "fetch_account_snapshot"]
ExecutionCommand = Literal["place_limit_order", "place_emergency_ioc_exit", "cancel_order", "merge_outcome"]
SidecarCommand = ReadOnlyCommand | ExecutionCommand


class OutcomeSdkSidecarClient:
    """One long-lived JSON-lines owner for the official TypeScript SDK.

    Calls are deliberately serialized.  The HIP-4 adapter/auth state lives in
    the child process, while the unique request id makes a response from a
    restarted or unhealthy child impossible to mistake for the current call.
    """

    def __init__(self, sidecar_dir: str | Path = "outcome_sdk_sidecar", *, request_timeout_sec: float = 15.0) -> None:
        self.sidecar_dir = Path(sidecar_dir).resolve()
        self.request_timeout_sec = request_timeout_sec
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.RLock()
        self.last_request_timing: dict[str, Any] | None = None

    def close(self) -> None:
        """Stop the child without sending it an execution command."""
        with self._lock:
            process, self._process = self._process, None
            if process is None:
                return
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def __enter__(self) -> "OutcomeSdkSidecarClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _start(self, script: Path) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        self._process = subprocess.Popen(
            ["node", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            # Errors are returned on the JSON-lines protocol.  Do not leave a
            # stderr pipe unread and let verbose Node diagnostics block trading.
            stderr=subprocess.DEVNULL, text=True, cwd=self.sidecar_dir, bufsize=1,
        )
        return self._process

    def _discard_unhealthy_process(self) -> None:
        process, self._process = self._process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def request(
        self,
        command: SidecarCommand,
        *,
        testnet: bool = False,
        payload: Mapping[str, Any] | None = None,
        allow_execution: bool = False,
    ) -> Any:
        if command in {"place_limit_order", "place_emergency_ioc_exit", "cancel_order", "merge_outcome"} and not allow_execution:
            raise OutcomeSdkSidecarError("execution requires explicit allow_execution=True")
        if command not in {"health", "fetch_markets", "fetch_order_book", "fetch_settled_outcome", "fetch_account_snapshot", "place_limit_order", "place_emergency_ioc_exit", "cancel_order", "merge_outcome"}:
            raise OutcomeSdkSidecarError(f"unsupported sidecar command: {command}")
        script = self.sidecar_dir / "dist" / "main.js"
        if not script.exists():
            raise OutcomeSdkSidecarError("SDK sidecar is not built; run `npm install` then `npm run build`")
        request: dict[str, Any] = {"id": uuid.uuid4().hex, "command": command, "testnet": testnet}
        if payload is not None:
            request["payload"] = dict(payload)
        with self._lock:
            started_at = time.monotonic()
            process = self._start(script)
            if process.stdin is None or process.stdout is None:
                self._discard_unhealthy_process()
                raise OutcomeSdkSidecarError("SDK sidecar stdio is unavailable")
            try:
                process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
                process.stdin.flush()
                ready, _, _ = select.select([process.stdout.fileno()], [], [], self.request_timeout_sec)
                if not ready:
                    raise TimeoutError(f"SDK sidecar response exceeded {self.request_timeout_sec:g}s")
                line = process.stdout.readline()
            except (BrokenPipeError, OSError, TimeoutError) as exc:
                self._discard_unhealthy_process()
                raise OutcomeSdkSidecarError(f"SDK sidecar transport failed: {exc}") from exc
            elapsed_ms = round((time.monotonic() - started_at) * 1000, 3)
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                self._discard_unhealthy_process()
                raise OutcomeSdkSidecarError("SDK sidecar returned invalid JSON") from exc
            if response.get("id") != request["id"]:
                self._discard_unhealthy_process()
                raise OutcomeSdkSidecarError("SDK sidecar response id did not match request")
            self.last_request_timing = {
                "command": command, "python_round_trip_ms": elapsed_ms,
                "sdk_sidecar_ms": response.get("timingMs"),
                "pid": process.pid, "persistent": True,
            }
            if not response.get("ok"):
                error = response.get("error") or {}
                raise OutcomeSdkSidecarError(f"{error.get('code', 'UNKNOWN')}: {error.get('message', '')}")
            return response.get("result")
