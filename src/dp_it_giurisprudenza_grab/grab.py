"""Per-item grab into ``<dir>``; the item's ``meta.kind`` picks the source: ``cc`` (open data dumps), ``cassazione`` (ItalGiure Solr pages),
``cgt`` (DocTrib search and detail pages)."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dp_it_giurisprudenza_grab import cassazione, cc
from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_giurisprudenza_grab.http import DEFAULT_ALLOWED_HOSTS, build_client

if TYPE_CHECKING:
    import httpx

Progress = Callable[[int, str], None]
KIND_CC = "cc"
KIND_CASSAZIONE = "cassazione"
KINDS = (KIND_CC, KIND_CASSAZIONE)


@dataclass(frozen=True)
class GrabDeps:
    """The ports of a run: transport (None = real network) and sleep (pacing)."""

    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = field(default=time.sleep)


def validate_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise GrabError(CONFIG_ERROR, "items must be a non-empty array")
    for entry in items:
        if not isinstance(entry, dict) or not entry.get("key"):
            raise GrabError(CONFIG_ERROR, f"item needs a key: {entry!r}")
        meta = entry.get("meta")
        if not isinstance(meta, dict) or meta.get("kind") not in KINDS:
            raise GrabError(CONFIG_ERROR, f"item {entry['key']}: meta.kind must be one of {list(KINDS)}")
        if meta["kind"] == KIND_CC:
            cc.validate_meta(entry["key"], meta)
        else:
            cassazione.validate_meta(entry["key"], meta)
    return items


def _cass_options(params: dict[str, Any]) -> cassazione.CassOptions:
    base_url = params.get("solrBaseUrl", cassazione.DEFAULT_BASE_URL)
    if not isinstance(base_url, str):
        raise GrabError(CONFIG_ERROR, "solrBaseUrl must be a string")
    cassazione.host_of(base_url)  # validates the URL
    max_docs = params.get("maxDocs")
    if max_docs is not None and (not isinstance(max_docs, int) or isinstance(max_docs, bool) or max_docs < 1):
        raise GrabError(CONFIG_ERROR, "maxDocs must be an integer >= 1")
    return cassazione.CassOptions(base_url=base_url, max_docs=max_docs)


def _planned(base: Path, entry: dict[str, Any]) -> dict[str, str]:
    return cc.planned(base, entry) if entry["meta"]["kind"] == KIND_CC else cassazione.planned(base, entry)


def run_grab(params: dict[str, Any], deps: GrabDeps, on_progress: Progress) -> dict[str, Any]:
    """Tries every item; if any failed raises GrabError (config_error if any failure is a config error, else transient)."""
    items = validate_items(params.get("items"))
    directory = params.get("dir")
    if not isinstance(directory, str) or not directory:
        raise GrabError(CONFIG_ERROR, "dir is required")
    hosts = params.get("allowedHosts", list(DEFAULT_ALLOWED_HOSTS))
    if not isinstance(hosts, list) or not all(isinstance(h, str) for h in hosts):
        raise GrabError(CONFIG_ERROR, "allowedHosts must be an array of host names")
    verify_tls = params.get("verifyTls", True)
    if not isinstance(verify_tls, bool):
        raise GrabError(CONFIG_ERROR, "verifyTls must be a boolean")
    options = _cass_options(params)
    if "solrBaseUrl" in params and "allowedHosts" not in params:
        # a replay server stands in for ItalGiure: the Source config names it explicitly, so its host is allowed too
        hosts = [*hosts, cassazione.host_of(options.base_url)]
    base = Path(directory)

    if params.get("dryRun") is True:
        return {"downloaded": 0, "unchanged": 0, "files": [_planned(base, e) for e in items], "dryRun": True}

    client = build_client(hosts, deps.transport, verify_tls=verify_tls)
    index = cc.IndexPage(client)
    pacer = cassazione.Pacer(deps.sleep)
    files: list[dict[str, Any]] = []
    failures: list[tuple[str, GrabError]] = []
    for position, entry in enumerate(items):
        try:
            if entry["meta"]["kind"] == KIND_CC:
                files.append(cc.grab_cc_item(entry, base, client, index))
            else:
                files.append(cassazione.grab_cassazione_item(entry, base, client, options, pacer))
        except GrabError as error:
            failures.append((entry["key"], error))
        on_progress(int((position + 1) * 100 / len(items)), f"{position + 1}/{len(items)} {entry['key']}")
    if failures:
        code = CONFIG_ERROR if any(error.code == CONFIG_ERROR for _, error in failures) else TRANSIENT_ERROR
        detail = "; ".join(f"{key} ({error})" for key, error in failures)
        msg = f"{len(failures)} of {len(items)} items failed: {detail}"
        raise GrabError(code, msg)
    return {
        "downloaded": sum(1 for f in files if f["status"] == "downloaded"),
        "unchanged": sum(1 for f in files if f["status"] == "unchanged"),
        "files": files,
    }
