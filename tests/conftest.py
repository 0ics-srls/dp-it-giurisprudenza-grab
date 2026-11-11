"""Puts the repo root and src/ on sys.path, forbids real network access and provides scripted HTTP servers (httpx.MockTransport)."""
from __future__ import annotations

import io
import socket
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
for entry in (ROOT, ROOT / "src", ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

CC_INDEX_URL = "https://dati.cortecostituzionale.it/Scarica_i_dati/Scarica_i_dati"
CC_PRONUNCE_2001 = "https://dati.cortecostituzionale.it/opendata/distribuzione/CC_OpenPronunce_2001_oggi.zip"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: hermetic unit test")
    config.addinivalue_line("markers", "integration: needs a network")


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        msg = "tests must not use the network"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)


def fixture_bytes(name: str) -> bytes:
    return (Path(__file__).resolve().parent / "fixtures" / name).read_bytes()


def make_zip(entries: dict[str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def bundle_zip() -> bytes:
    """A Corte costituzionale bundle: an outer zip holding one inner zip per year."""
    inner = make_zip({"Cc_Opendata_Pronunce_2001.xml": "<elenco_pronunce/>"})
    return make_zip({"Cc_Opendata_Pronunce_2001.zip": inner})


Handler = Callable[[httpx.Request], httpx.Response]


@dataclass
class Server:
    """A scripted server: ``routes`` maps a full URL (no query) to a handler; every request is logged."""

    routes: dict[str, Handler] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(str(request.url.copy_with(query=None)))
        if handler is None:
            return httpx.Response(404, text="not found")
        return handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


@pytest.fixture
def sleeps() -> list[float]:
    return []


SOLR_URL = "https://www.italgiure.giustizia.it/sncass/isapi/hc.dll/sn.solr/sn-collection/select"


def solr_doc(number: int) -> dict[str, object]:
    """A synthetic sentence with the list-valued fields the live endpoint returns."""
    return {
        "id": f"snciv2025{number:06d}O",
        "kind": ["snciv"],
        "anno": ["2025"],
        "numdec": [str(number)],
        "szdec": ["5"],
        "datdec": ["20250310"],
        "datdep": ["20250411"],
        "materia": ["TRIBUTI"],
        "ocr": [f"testo della sentenza {number}"],
    }


@dataclass
class SolrFake:
    """A fake ItalGiure Solr over ``total`` documents: answers form POSTs with ``numFound`` and the ``start``/``rows`` slice. Forms are logged."""

    total: int
    forms: list[dict[str, str]] = field(default_factory=list)
    fail_first: int = 0
    status: int = 200
    calls: int = 0

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.status != 200:
            return httpx.Response(self.status, text="denied")
        if self.fail_first > 0:
            self.fail_first -= 1
            return httpx.Response(500, text="boom")
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8")).items()}
        self.forms.append(form)
        start, rows = int(form["start"]), int(form["rows"])
        docs = [solr_doc(n) for n in range(start + 1, min(start + rows, self.total) + 1)]
        body = {"responseHeader": {"status": 0}, "response": {"numFound": self.total, "start": start, "docs": docs}}
        return httpx.Response(200, json=body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


@pytest.fixture
def cc_server() -> Server:
    """The Corte costituzionale host: the real index page and the 2001_oggi dump."""
    server = Server()
    server.routes[CC_INDEX_URL] = lambda _r: httpx.Response(200, content=fixture_bytes("cc-index.html"))
    dump = bundle_zip()
    server.routes[CC_PRONUNCE_2001] = lambda _r: httpx.Response(
        200, content=dump, headers={"Last-Modified": "Wed, 11 Nov 2026 04:32:02 GMT", "ETag": 'W/"abc-1"', "Content-Length": str(len(dump))},
    )
    return server
