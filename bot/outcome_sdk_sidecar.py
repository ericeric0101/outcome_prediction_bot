"""Constrained Python client for the isolated official-SDK sidecar.

The allowed command set intentionally contains no trading command while P4 is
hard-disabled.  This module never receives private keys.
"""
from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from typing import Any, Literal


class OutcomeSdkSidecarError(RuntimeError):
    pass


ReadOnlyCommand = Literal["health", "fetch_markets"]


class OutcomeSdkSidecarClient:
    def __init__(self, sidecar_dir: str | Path = "outcome_sdk_sidecar") -> None:
        self.sidecar_dir = Path(sidecar_dir).resolve()

    def request(self, command: ReadOnlyCommand, *, testnet: bool = False) -> Any:
        if command not in {"health", "fetch_markets"}:
            raise OutcomeSdkSidecarError(f"P4 hard-disabled command: {command}")
        script = self.sidecar_dir / "dist" / "main.js"
        if not script.exists():
            raise OutcomeSdkSidecarError("SDK sidecar is not built; run `npm install` then `npm run build`")
        request = {"id": uuid.uuid4().hex, "command": command, "testnet": testnet}
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
