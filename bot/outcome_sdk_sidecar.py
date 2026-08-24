"""Python boundary for the official Outcome TypeScript SDK sidecar.

Execution is disabled by default twice: callers must opt in with
``allow_execution=True`` and the operator must set
``OUTCOME_SDK_EXECUTION_ENABLED=1`` for the sidecar process. Private keys
remain exclusively in the sidecar environment.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Literal, Mapping


class OutcomeSdkSidecarError(RuntimeError):
    pass


ReadOnlyCommand = Literal["health", "fetch_markets", "fetch_order_book", "fetch_settled_outcome", "fetch_account_snapshot"]
ExecutionCommand = Literal["place_limit_order", "cancel_order", "merge_outcome"]
SidecarCommand = ReadOnlyCommand | ExecutionCommand


class OutcomeSdkSidecarClient:
    def __init__(self, sidecar_dir: str | Path = "outcome_sdk_sidecar") -> None:
        self.sidecar_dir = Path(sidecar_dir).resolve()

    def request(
        self,
        command: SidecarCommand,
        *,
        testnet: bool = False,
        payload: Mapping[str, Any] | None = None,
        allow_execution: bool = False,
    ) -> Any:
        if command in {"place_limit_order", "cancel_order", "merge_outcome"} and not allow_execution:
            raise OutcomeSdkSidecarError("execution requires explicit allow_execution=True")
        if command not in {"health", "fetch_markets", "fetch_order_book", "fetch_settled_outcome", "fetch_account_snapshot", "place_limit_order", "cancel_order", "merge_outcome"}:
            raise OutcomeSdkSidecarError(f"unsupported sidecar command: {command}")
        script = self.sidecar_dir / "dist" / "main.js"
        if not script.exists():
            raise OutcomeSdkSidecarError("SDK sidecar is not built; run `npm install` then `npm run build`")
        request: dict[str, Any] = {"id": uuid.uuid4().hex, "command": command, "testnet": testnet}
        if payload is not None:
            request["payload"] = dict(payload)
        completed = subprocess.run(
            ["node", str(script)], input=json.dumps(request) + "\n", text=True,
            capture_output=True, cwd=self.sidecar_dir, check=False, timeout=30,
        )
        if completed.returncode != 0:
            raise OutcomeSdkSidecarError(completed.stderr.strip() or "SDK sidecar failed")
        try:
            response = json.loads(completed.stdout.strip())
        except json.JSONDecodeError as exc:
            raise OutcomeSdkSidecarError("SDK sidecar returned invalid JSON") from exc
        if not response.get("ok"):
            error = response.get("error") or {}
            raise OutcomeSdkSidecarError(f"{error.get('code', 'UNKNOWN')}: {error.get('message', '')}")
        return response.get("result")
