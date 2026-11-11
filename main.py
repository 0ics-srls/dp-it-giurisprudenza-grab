"""Entrypoint of dp-it-giurisprudenza-grab: proc-protocol stdin loop. Action ``execute`` downloads the grab files of the given items; ``terminate``
(or stdin EOF) shuts down. stdout carries protocol events only."""
from __future__ import annotations

import logging
import sys
from typing import TextIO

import procclass
from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, SYSTEM_ERROR, GrabError
from dp_it_giurisprudenza_grab.grab import GrabDeps, run_grab

log = logging.getLogger("dp_it_giurisprudenza_grab")


def main(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout, deps: GrabDeps | None = None) -> int:
    """One JSON request per stdin line until terminate/EOF; returns 1 if any request failed."""
    ports = deps or GrabDeps()
    exit_code = 0
    for line in stdin:
        if not line.strip():
            continue
        try:
            request = procclass.parse(line)
        except procclass.ProcProtocolError as ex:
            procclass.emit_error(stdout, CONFIG_ERROR, str(ex))
            exit_code = 1
            continue
        if request.action == "terminate":
            break
        if request.action != "execute":
            procclass.emit_error(stdout, CONFIG_ERROR, f"unknown action: {request.action}")
            exit_code = 1
            continue
        try:
            result = run_grab(request.params, ports, lambda percent, message: procclass.emit_progress(stdout, percent, message))
        except GrabError as error:
            procclass.emit_error(stdout, error.code, str(error))
            exit_code = 1
            continue
        except Exception:
            log.exception("unexpected failure in execute")
            procclass.emit_error(stdout, SYSTEM_ERROR, "unexpected failure in execute")
            exit_code = 1
            continue
        procclass.emit_result(stdout, result)
    return exit_code


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    sys.exit(main())
