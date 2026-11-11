"""proc-protocol helper (NDJSON over stdio).

Request: one JSON object per stdin line with `_action` (string) + `_meta.executionId`
(string) + user params. Events on stdout: progress / result / error (NDJSON, camelCase).
EOF on stdin = shutdown. Mirrors the C# LexCorpus.ProcClass.Sdk contract.
"""
import json
from typing import Any, TextIO


class ProcProtocolError(Exception):
    """Raised when a stdin line is not a valid proc-protocol request."""


class ProcRequest:
    """A parsed request: the action, the protocol meta, and the user params (no `_` keys)."""

    def __init__(self, action: str, meta: dict[str, Any], params: dict[str, Any]) -> None:
        self.action = action
        self.meta = meta
        self.params = params


def parse(line: str) -> ProcRequest:
    """Parse one JSON-Lines request. Raises ProcProtocolError on malformed input."""
    try:
        obj = json.loads(line)
    except (ValueError, TypeError) as ex:
        raise ProcProtocolError(f"not JSON: {ex}") from ex
    if not isinstance(obj, dict):
        raise ProcProtocolError("request must be a JSON object")

    action = obj.get("_action")
    if not isinstance(action, str) or not action:
        raise ProcProtocolError("_action is required")

    meta = obj.get("_meta")
    if not isinstance(meta, dict) or not meta.get("executionId"):
        raise ProcProtocolError("_meta.executionId is required")

    params = {k: v for k, v in obj.items() if not k.startswith("_")}
    return ProcRequest(action, meta, params)


def _emit(out: TextIO, event: dict[str, Any]) -> None:
    out.write(json.dumps(event, ensure_ascii=False) + "\n")
    out.flush()


def emit_progress(out: TextIO, percent: float, message: str) -> None:
    _emit(out, {"type": "progress", "percent": percent, "message": message})


def emit_result(out: TextIO, data: dict[str, Any]) -> None:
    _emit(out, {"type": "result", "data": data})


def emit_error(out: TextIO, code: str, message: str, data: dict[str, Any] | None = None) -> None:
    _emit(out, {"type": "error", "code": code, "message": message, "data": data})
