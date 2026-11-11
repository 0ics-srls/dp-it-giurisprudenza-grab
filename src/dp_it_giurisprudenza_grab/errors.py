"""The three Proc-protocol error codes and the run-level failure that carries one of them."""
from __future__ import annotations

CONFIG_ERROR = "config_error"
TRANSIENT_ERROR = "transient_error"
SYSTEM_ERROR = "system_error"


class GrabError(Exception):
    """A failure carrying one of the three protocol error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
