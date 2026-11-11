"""ProcMon rejects a class manifest whose description is longer than 200 characters (found by the live registration of phase 78)."""
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

MAX_DESCRIPTION = 200


def _manifest_path() -> Path:
    return Path(__file__).resolve().parent.parent / "manifest.json"


def _descriptions(node: Any, path: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield f"{path}/description", value
            yield from _descriptions(value, f"{path}/{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _descriptions(value, f"{path}[{index}]")


def test_all_descriptions_within_200_chars() -> None:
    manifest = json.loads(_manifest_path().read_text(encoding="utf-8"))

    too_long = {path: len(text) for path, text in _descriptions(manifest) if len(text) > MAX_DESCRIPTION}

    assert too_long == {}
    assert manifest["description"]
