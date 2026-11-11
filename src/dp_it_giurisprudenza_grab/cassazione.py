"""Cassazione: one ItalGiure Solr partition (kind x anno x month; month 13 = residual) into raw page files (port of CassazioneSolrClient + CassazioneGrab).

The partition is counted (``rows=0``), then paged with ``sort=id asc`` (a stable order, so a resume by offset is valid) until ``start >= numFound`` or
``maxDocs``. Pages are written as the source sent them (``<dir>/<stem>/pages/<kind>-<anno><mm>-p<NNN>.json``) for the convert class. Requests are
paced (1.5 s) and a 5xx is retried with back-off inside the item (P79-15).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from dp_it_giurisprudenza_grab.cc import safe_name
from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_giurisprudenza_grab.http import status_error

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

DEFAULT_BASE_URL = "https://www.italgiure.giustizia.it"
ENDPOINT_PATH = "/sncass/isapi/hc.dll/sn.solr/sn-collection/select?app.query"
FL = "id,ocr,ocrdis,anno,numdec,szdec,tipoprov,datdec,datdep,materia,presidente,relatore,kind"
KINDS = ("snciv", "snpen")
RESIDUAL_MONTH = 13
PAGE_ROWS = 500
PAUSE_SEC = 1.5
MAX_RETRIES = 3
BACKOFF_SEC = 5.0
_PAGE_NAME = re.compile(r"^(?:snciv|snpen)-\d{6}-p(\d{3})\.json$")


@dataclass(frozen=True)
class CassOptions:
    """Source settings of the Cassazione grab."""

    base_url: str = DEFAULT_BASE_URL
    max_docs: int | None = None
    rows: int = PAGE_ROWS

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + ENDPOINT_PATH


class Pacer:
    """Sleeps ``delay`` before every request but the first of the run, so a burst never reaches the source."""

    def __init__(self, sleep: Callable[[float], None], delay: float = PAUSE_SEC) -> None:
        self._sleep = sleep
        self._delay = delay
        self._first = True

    def wait(self, extra: float = 0.0) -> None:
        if not self._first or extra:
            self._sleep(self._delay + extra)
        self._first = False


def host_of(base_url: str) -> str:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise GrabError(CONFIG_ERROR, f"solrBaseUrl must be an http(s) URL, got {base_url!r}")
    return parts.hostname.lower()


def validate_meta(key: str, meta: dict[str, Any]) -> None:
    anno, mese = meta.get("anno"), meta.get("mese")
    if meta.get("cassKind") not in KINDS:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.cassKind must be one of {list(KINDS)}")
    if not isinstance(anno, int) or isinstance(anno, bool):
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.anno must be an integer")
    if not isinstance(mese, int) or isinstance(mese, bool) or not 1 <= mese <= RESIDUAL_MONTH:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.mese must be an integer 1..{RESIDUAL_MONTH}")
    if not isinstance(meta.get("itemId"), str) or not meta["itemId"]:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.itemId is required")


def query_of(kind: str, anno: int, mese: int) -> str:
    """The partition query exactly as the legacy CassazioneSolrClient.QueryOf (day 31 covers every month of the lexicographic range)."""
    if mese == RESIDUAL_MONTH:
        return f'kind:"{kind}" AND anno:"{anno}" AND NOT datdec:[{anno}0101 TO {anno}1231]'
    return f'kind:"{kind}" AND anno:"{anno}" AND datdec:[{anno}{mese:02d}01 TO {anno}{mese:02d}31]'


def planned(base: Path, entry: dict[str, Any]) -> dict[str, str]:
    return {"key": entry["key"], "path": str(base / safe_name(entry["meta"]["itemId"])), "status": "planned"}


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _post(client: httpx.Client, options: CassOptions, form: dict[str, str], pacer: Pacer) -> dict[str, Any]:
    """One paced POST with retry on 5xx and transport errors (3 retries, growing back-off); 4xx other than 429 is final."""
    last: GrabError | None = None
    for attempt in range(MAX_RETRIES + 1):
        pacer.wait(BACKOFF_SEC * attempt if attempt else 0.0)
        try:
            response = client.post(options.endpoint, data=form)
        except httpx.TransportError as error:
            last = GrabError(TRANSIENT_ERROR, f"Solr request to {host_of(options.base_url)} failed: {error}")
            continue
        if response.status_code == httpx.codes.OK:
            try:
                payload = response.json()
                payload["response"]["numFound"]
            except (ValueError, KeyError, TypeError) as error:
                raise GrabError(TRANSIENT_ERROR, f"unexpected Solr answer from {host_of(options.base_url)}: {error}") from error
            return payload  # type: ignore[no-any-return]
        failure = status_error(options.endpoint, response.status_code)
        if failure.code == CONFIG_ERROR:
            raise failure
        last = failure
    assert last is not None
    raise last


def grab_cassazione_item(
    entry: dict[str, Any], base: Path, client: httpx.Client, options: CassOptions, pacer: Pacer,
) -> dict[str, Any]:
    """One partition into ``<base>/<stem>/pages``; returns ``{key, path, pages, numFound, written, status}``."""
    meta = entry["meta"]
    kind, anno, mese = meta["cassKind"], meta["anno"], meta["mese"]
    query = query_of(kind, anno, mese)
    form = {"q": query, "wt": "json", "fl": FL, "sort": "id asc"}
    stem_dir = base / safe_name(meta["itemId"])
    pages_dir = stem_dir / "pages"

    num_found = int(_post(client, options, {**form, "rows": "0", "start": "0"}, pacer)["response"]["numFound"])
    limit = num_found if options.max_docs is None else min(num_found, options.max_docs)
    written = pages = 0
    while written < limit:
        rows = options.rows if options.max_docs is None else min(options.rows, limit - written)
        payload = _post(client, options, {**form, "rows": str(rows), "start": str(written)}, pacer)
        docs = payload["response"].get("docs") or []
        if not docs:
            break  # the source ran dry before numFound (documents removed meanwhile)
        payload["response"]["docs"] = docs[:rows]
        _write_atomic(pages_dir / f"{kind}-{anno}{mese:02d}-p{pages:03d}.json", json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        written += len(payload["response"]["docs"])
        pages += 1
    for stale in pages_dir.glob("*.json") if pages_dir.exists() else []:
        match = _PAGE_NAME.match(stale.name)
        if match and int(match.group(1)) >= pages:
            stale.unlink()  # a page of an earlier, larger run
    summary = {"key": entry["key"], "itemId": meta["itemId"], "query": query, "numFound": num_found, "pages": pages, "written": written, "rows": options.rows}
    _write_atomic(stem_dir / "partition.json", json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"key": entry["key"], "path": str(stem_dir), "pages": pages, "numFound": num_found, "written": written, "status": "downloaded"}
