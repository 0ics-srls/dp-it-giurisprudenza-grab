"""Corte costituzionale open data dumps (``dati.cortecostituzionale.it``): index check, conditional GET, atomic write, sidecar (P79-13).

The class downloads a fixed, validated list of dump names, not whatever the index lists. Before downloading it checks that the dump URL is still
on the index page (a vanished dump is a config_error) and sends ``If-Modified-Since`` / ``If-None-Match`` from the sidecar so an unchanged dump
is not downloaded again. The body is streamed to ``<stem>.zip.part``, verified and renamed atomically.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_giurisprudenza_grab.http import status_error

if TYPE_CHECKING:
    from pathlib import Path

INDEX_URL = "https://dati.cortecostituzionale.it/Scarica_i_dati/Scarica_i_dati"
_BASE = "https://dati.cortecostituzionale.it/opendata"
BUNDLE_URLS: dict[str, str] = {
    "pronunce_1956_1980": f"{_BASE}/distribuzione/CC_OpenPronunce_1956_1980.zip",
    "pronunce_1981_2000": f"{_BASE}/distribuzione/CC_OpenPronunce_1981_2000.zip",
    "pronunce_2001_oggi": f"{_BASE}/distribuzione/CC_OpenPronunce_2001_oggi.zip",
    "massime_1956_1980": f"{_BASE}/distribuzione/CC_OpenMassime_1956_1980.zip",
    "massime_1981_2000": f"{_BASE}/distribuzione/CC_OpenMassime_1981_2000.zip",
    "massime_2001_oggi": f"{_BASE}/distribuzione/CC_OpenMassime_2001_oggi.zip",
    "norme_ordinanze": f"{_BASE}/Norme_impugnate/Cc_Opendata_NormeImpugnate_RegistroOrdinanze_json.zip",
    "norme_ricorsi": f"{_BASE}/Norme_impugnate/Cc_Opendata_NormeImpugnate_RegistroRicorsi_json.zip",
    "norme_conflitti_enti": f"{_BASE}/Norme_impugnate/Cc_Opendata_NormeImpugnate_RegistroConflittiEnti_json.zip",
    "norme_conflitti_poteri": f"{_BASE}/Norme_impugnate/Cc_Opendata_NormeImpugnate_RegistroConflittiPoteri_json.zip",
}
_YEAR_ENTRY = re.compile(r"(\d{4})\.zip$", re.IGNORECASE)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
CHUNK = 1 << 16


def safe_name(item_id: str) -> str:
    """File stem of an item id (the convert class derives the same stem): ``cc:pronunce_2001_oggi`` -> ``cc_pronunce_2001_oggi``."""
    return _UNSAFE.sub("_", item_id.replace(" ", "-"))


def validate_meta(key: str, meta: dict[str, Any]) -> None:
    bundle = meta.get("bundle")
    if bundle not in BUNDLE_URLS:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.bundle must be one of {sorted(BUNDLE_URLS)}, got {bundle!r}")
    if not isinstance(meta.get("itemId"), str) or not meta["itemId"]:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.itemId is required")


def paths(base: Path, item_id: str) -> tuple[Path, Path, Path]:
    stem = safe_name(item_id)
    return base / f"{stem}.zip", base / f"{stem}.zip.part", base / f"{stem}.dump.json"


def planned(base: Path, entry: dict[str, Any]) -> dict[str, str]:
    zip_path, _, sidecar = paths(base, entry["meta"]["itemId"])
    return {"key": entry["key"], "path": str(zip_path), "sidecar": str(sidecar), "status": "planned"}


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


@dataclass(frozen=True)
class DumpHead:
    """What the sidecar remembers of a downloaded dump."""

    last_modified: str | None
    etag: str | None
    sha256: str
    size: int


class IndexPage:
    """The dump index page, fetched once per run."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._text: str | None = None

    def text(self) -> str:
        if self._text is None:
            try:
                response = self._client.get(INDEX_URL)
            except httpx.TransportError as error:
                raise GrabError(TRANSIENT_ERROR, f"cannot read the dump index {INDEX_URL}: {error}") from error
            if response.status_code != httpx.codes.OK:
                raise status_error(INDEX_URL, response.status_code)
            self._text = response.text
        return self._text

    def lists(self, url: str) -> bool:
        return url in self.text()


def _read_sidecar(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _verify_zip(part: Path, bundle: str, key: str) -> None:
    """The body must be a zip that holds what the bundle promises; anything else (an HTML error page, a half file) is a transient source fault."""
    if not zipfile.is_zipfile(part):
        raise GrabError(TRANSIENT_ERROR, f"item {key}: the response is not a zip file")
    try:
        with zipfile.ZipFile(part) as archive:
            names = [info.filename for info in archive.infolist() if not info.is_dir()]
    except zipfile.BadZipFile as error:
        raise GrabError(TRANSIENT_ERROR, f"item {key}: the zip cannot be opened: {error}") from error
    if bundle.startswith("norme_"):
        if not any(name.lower().endswith(".json") for name in names):
            raise GrabError(TRANSIENT_ERROR, f"item {key}: the registry zip holds no .json entry")
    elif not any(_YEAR_ENTRY.search(name) for name in names):
        raise GrabError(TRANSIENT_ERROR, f"item {key}: the bundle holds no year entries")


def _stream_to_part(client: httpx.Client, url: str, headers: dict[str, str], part: Path, key: str) -> DumpHead | None:
    """Streams the dump into ``part``. Returns the response head (headers), or None on 304 Not Modified."""
    sha = hashlib.sha256()
    written = 0
    try:
        with client.stream("GET", url, headers=headers) as response:
            if response.status_code == httpx.codes.NOT_MODIFIED:
                return None
            if response.status_code != httpx.codes.OK:
                raise status_error(url, response.status_code)
            declared = response.headers.get("Content-Length")
            part.parent.mkdir(parents=True, exist_ok=True)
            with part.open("wb") as handle:
                for chunk in response.iter_bytes(CHUNK):
                    handle.write(chunk)
                    sha.update(chunk)
                    written += len(chunk)
            if declared is not None and declared.isdigit() and int(declared) != written:
                raise GrabError(TRANSIENT_ERROR, f"item {key}: the body is truncated ({written} of {declared} bytes)")
            return DumpHead(response.headers.get("Last-Modified"), response.headers.get("ETag"), sha.hexdigest(), written)
    except httpx.TransportError as error:
        raise GrabError(TRANSIENT_ERROR, f"item {key}: the download of {url} failed: {error}") from error


def grab_cc_item(entry: dict[str, Any], base: Path, client: httpx.Client, index: IndexPage) -> dict[str, str]:
    """One dump into ``<base>/<stem>.zip`` plus the sidecar ``<stem>.dump.json``; returns ``{key, path, sidecar, status}``."""
    meta = entry["meta"]
    key = entry["key"]
    bundle = meta["bundle"]
    url = BUNDLE_URLS[bundle]
    zip_path, part, sidecar_path = paths(base, meta["itemId"])
    if not index.lists(url):
        raise GrabError(CONFIG_ERROR, f"item {key}: the dump {bundle} ({url}) is no longer on the index page {INDEX_URL}")

    previous = _read_sidecar(sidecar_path)
    headers: dict[str, str] = {}
    if zip_path.exists():
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = str(previous["last_modified"])
        if previous.get("etag"):
            headers["If-None-Match"] = str(previous["etag"])
    try:
        head = _stream_to_part(client, url, headers, part, key)
        if head is None:
            sidecar = {**previous, "key": key, "status": "unchanged"}
            _write_atomic(sidecar_path, json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"))
            return {"key": key, "path": str(zip_path), "sidecar": str(sidecar_path), "status": "unchanged"}
        _verify_zip(part, bundle, key)
        part.replace(zip_path)
    finally:
        part.unlink(missing_ok=True)
    sidecar = {
        "key": key,
        "itemId": meta["itemId"],
        "bundle": bundle,
        "url": url,
        "last_modified": head.last_modified,
        "etag": head.etag,
        "sha256": head.sha256,
        "bytes": head.size,
        "status": "downloaded",
    }
    _write_atomic(sidecar_path, json.dumps(sidecar, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"key": key, "path": str(zip_path), "sidecar": str(sidecar_path), "status": "downloaded"}
