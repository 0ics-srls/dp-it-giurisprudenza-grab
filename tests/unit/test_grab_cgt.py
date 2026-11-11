"""CGT grab: month window -> day windows while the counter exceeds the 50 served, then the detail page of every kept decision."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from conftest import Server, fixture_bytes
from dp_it_giurisprudenza_grab.classify import parse_search
from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_giurisprudenza_grab.grab import GrabDeps, run_grab

BASE = "https://def.finanze.it/DocTribFrontend/"
HOME = BASE + "RS2_HomePage.jsp"
SEARCH = BASE + "executeAdvancedGiurisprudenzaSearch.do"
DETAIL = BASE + "getGiurisprudenzaDetail.do"
CAMPANIA = "{E69E3100-5261-4D7D-B967-2BC6ABDDAF2A}"
CGT_ENTE = "Corte di giustizia tributaria di primo grado di Ragusa"


def item(first: str, last: str) -> dict[str, Any]:
    item_id = f"cgt:{first}:{last}"
    return {
        "key": f"{item_id}|v1",
        "url": f"{SEARCH}?from={first}&to={last}",
        "meta": {"kind": "cgt", "itemId": item_id, "version": "1", "from": first, "to": last},
    }


def search_page(declared: int, estremi: list[str]) -> bytes:
    """A DocTrib answer: HTML in ISO-8859-1 carrying the XML in a JS string with escaped quotes and slashes."""
    provvedimenti = "".join(
        f'<Provvedimento idProvvedimento=\\"{{00000000-0000-0000-0000-{abs(hash(text)) % 10**12:012d}}}\\"><estremi link=\\"true\\">{text.replace("/", "\\/")}<\\/estremi><\\/Provvedimento>'
        for text in estremi
    )
    xml = (
        '<?xml version=\\"1.0\\" encoding=\\"UTF-8\\"?><risultatiRicerca><contatori><contatoreGiurisprudenza>'
        f"{declared}<\\/contatoreGiurisprudenza><\\/contatori><risultati>{provvedimenti}<\\/risultati><\\/risultatiRicerca>"
    )
    return f"<html><script>var xmlResult = '{xml}';</script></html>".encode("iso-8859-1")


def detail_page(text: str) -> bytes:
    return f"<html><script>var xmlDettaglio = '<r><testo><![CDATA[<p>{text}</p>]]></testo></r>';</script></html>".encode("iso-8859-1")


class DocTrib:
    """A scripted DocTrib host: a home page that sets the session cookie, a search that answers per window, a detail per guid."""

    def __init__(self) -> None:
        self.server = Server()
        self.forms: list[dict[str, str]] = []
        self.search: dict[tuple[str, str, str], bytes] = {}  # (giorno, mese, anno) -> page
        self.details: dict[str, httpx.Response] = {}
        self.server.routes[HOME] = lambda _r: httpx.Response(200, text="home", headers={"Set-Cookie": "JSESSIONID=ABC123; Path=/DocTribFrontend"})
        self.server.routes[SEARCH] = self._search
        self.server.routes[DETAIL] = self._detail

    def _search(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode("utf-8"), keep_blank_values=True).items()}
        self.forms.append(form)
        key = (form["giornoDataEmissioneDa"], form["meseDataEmissioneDa"], form["annoDataEmissioneDa"])
        return httpx.Response(200, content=self.search.get(key, search_page(0, [])))

    def _detail(self, request: httpx.Request) -> httpx.Response:
        return self.details.get(request.url.params["id"], httpx.Response(200, content=detail_page(f"testo di {request.url.params['id']}")))

    def deps(self, sleeps: list[float]) -> GrabDeps:
        return GrabDeps(transport=self.server.transport, sleep=sleeps.append)


def day_page(guid_estremi: list[str], declared: int | None = None) -> bytes:
    return search_page(declared if declared is not None else len(guid_estremi), guid_estremi)


def params(tmp_path: Path, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"items": list(items), "dir": str(tmp_path / "raw"), **extra}


def slice_json(tmp_path: Path, stem: str) -> dict[str, Any]:
    return json.loads((tmp_path / "raw" / stem / "slice.json").read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def estremi(day: int, number: int, ente: str = CGT_ENTE) -> str:
    return f"Sentenza del {day:02d}/12/2024 n. {number} - {ente} - Sezione/Collegio 1"


def test_search_form_equals_the_legacy_form(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()

    run_grab(params(tmp_path, item("2024-12-01", "2024-12-31")), doc.deps(sleeps), lambda *_: None)

    assert doc.forms[0] == {
        "js_enabled": "1",
        "tipoComplessitaRicerca": "avanzata",
        "tipoRicerca": "RA",
        "device": "D",
        "ambitoRicerca": "G",
        "tipoCriterioRicerca": "0",
        "tipo_ord": "DATA",
        "tipoEstremi": "",
        "ente": "",
        "numero": "",
        "parole": "",  # empty but obligatory
        "giornoDataEmissioneDa": "",
        "meseDataEmissioneDa": "12",
        "annoDataEmissioneDa": "2024",
        "giornoDataEmissioneA": "",
        "meseDataEmissioneA": "12",
        "annoDataEmissioneA": "2024",
    }
    assert "pagina" not in doc.forms[0]  # the source has no pagination


def test_session_cookie_from_the_home_page_is_resent(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()

    run_grab(params(tmp_path, item("2024-12-01", "2024-12-31")), doc.deps(sleeps), lambda *_: None)

    assert doc.server.urls()[0] == HOME
    search = next(r for r in doc.server.requests if str(r.url).startswith(SEARCH))
    assert "JSESSIONID=ABC123" in search.headers["Cookie"]
    assert search.headers["Referer"] == HOME
    assert search.headers["Accept-Language"] == "it"


def test_a_complete_month_is_taken_as_one_window(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    doc.search[("", "12", "2024")] = day_page([estremi(2, 11), estremi(3, 12, "Corte di Cassazione")])

    result = run_grab(params(tmp_path, item("2024-12-01", "2024-12-31")), doc.deps(sleeps), lambda *_: None)

    assert len(doc.forms) == 1
    info = slice_json(tmp_path, "cgt_2024-12-01_2024-12-31")
    assert (info["counter"], info["served"], info["kept"]) == (2, 2, 1)
    assert info["discarded"] == {"cassazione": 1, "commissioni": 0, "altri": 0}
    assert result["files"][0]["kept"] == 1


def test_month_with_counter_120_and_50_served_is_split_into_days(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    doc.search[("", "12", "2024")] = day_page([estremi(1, n) for n in range(1, 51)], declared=120)
    doc.search[("1", "12", "2024")] = day_page([estremi(1, 1), estremi(1, 2)])
    doc.search[("2", "12", "2024")] = day_page([estremi(2, 3)])

    run_grab(params(tmp_path, item("2024-12-01", "2024-12-31")), doc.deps(sleeps), lambda *_: None)

    windows = [(f["giornoDataEmissioneDa"], f["meseDataEmissioneDa"]) for f in doc.forms]
    assert windows[0] == ("", "12")  # the month first
    assert windows[1:] == [(str(day), "12") for day in range(1, 32)]  # then every day, the month page is not used
    info = slice_json(tmp_path, "cgt_2024-12-01_2024-12-31")
    assert info["kept"] == 3
    assert info["counter"] == 3  # the sum of the complete day windows


def test_day_with_counter_2_keeps_one_cgt_and_counts_one_cassazione(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    doc.search[("31", "12", "2024")] = fixture_bytes("cgt/search-g-day-real.html")

    result = run_grab(params(tmp_path, item("2024-12-31", "2024-12-31")), doc.deps(sleeps), lambda *_: None)

    info = slice_json(tmp_path, "cgt_2024-12-31_2024-12-31")
    assert (info["counter"], info["served"], info["kept"]) == (2, 2, 1)
    assert info["discarded"] == {"cassazione": 1, "commissioni": 0, "altri": 0}
    assert info["guids"] == [CAMPANIA]
    assert (tmp_path / "raw" / "cgt_2024-12-31_2024-12-31" / f"{CAMPANIA}.html").read_bytes().startswith(b"<html>")
    assert result["downloaded"] == 1
    assert doc.forms[0]["giornoDataEmissioneDa"] == "31"  # a single day is never queried as a month


def test_partial_month_is_taken_day_by_day(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()

    run_grab(params(tmp_path, item("2024-11-28", "2024-11-30")), doc.deps(sleeps), lambda *_: None)

    assert [(f["giornoDataEmissioneDa"], f["meseDataEmissioneDa"]) for f in doc.forms] == [("28", "11"), ("29", "11"), ("30", "11")]


def test_a_day_that_is_still_truncated_is_a_transient_error_naming_the_day(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    doc.search[("31", "12", "2024")] = day_page([estremi(31, n) for n in range(1, 51)], declared=80)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item("2024-12-31", "2024-12-31")), doc.deps(sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert "2024-12-31" in str(error.value)
    assert "80" in str(error.value)


def test_a_failing_detail_is_recorded_and_the_rest_continues(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    first, second = estremi(2, 11), estremi(2, 12)
    page = day_page([first, second])
    doc.search[("2", "12", "2024")] = page
    guids = [i.guid for i in parse_search(page).items]
    doc.details[guids[0]] = httpx.Response(500, text="boom")

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item("2024-12-02", "2024-12-02")), doc.deps(sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR  # the item is retried later
    info = slice_json(tmp_path, "cgt_2024-12-02_2024-12-02")
    assert info["failed"] == [guids[0]]
    stem = tmp_path / "raw" / "cgt_2024-12-02_2024-12-02"
    assert (stem / f"{guids[1]}.html").exists()  # the healthy document was still written
    assert not (stem / f"{guids[0]}.html").exists()
    assert any(delay >= 30 for delay in sleeps)  # one patient retry after a long pause, as the legacy


def test_existing_detail_files_are_not_downloaded_again(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    page = day_page([estremi(2, 11)])
    doc.search[("2", "12", "2024")] = page
    guid = parse_search(page).items[0].guid
    stem = tmp_path / "raw" / "cgt_2024-12-02_2024-12-02"
    stem.mkdir(parents=True)
    (stem / f"{guid}.html").write_bytes(detail_page("già preso"))

    result = run_grab(params(tmp_path, item("2024-12-02", "2024-12-02")), doc.deps(sleeps), lambda *_: None)

    assert not [r for r in doc.server.requests if str(r.url).startswith(DETAIL)]
    assert slice_json(tmp_path, "cgt_2024-12-02_2024-12-02")["existing"] == 1
    assert result["files"][0]["kept"] == 1


def test_pause_between_requests(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    doc.search[("2", "12", "2024")] = day_page([estremi(2, 11)])

    run_grab(params(tmp_path, item("2024-12-02", "2024-12-02")), doc.deps(sleeps), lambda *_: None)

    assert len(doc.server.requests) == 3  # home, search, detail
    assert sleeps == [1.5, 1.5]


def test_max_items_stops_early(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    doc.search[("2", "12", "2024")] = day_page([estremi(2, 11), estremi(2, 12), estremi(2, 13)])
    doc.search[("3", "12", "2024")] = day_page([estremi(3, 14)])

    result = run_grab(params(tmp_path, item("2024-12-02", "2024-12-03"), maxItems=2), doc.deps(sleeps), lambda *_: None)

    assert len(doc.forms) == 1  # the second day is never queried
    details = [r for r in doc.server.requests if str(r.url).startswith(DETAIL)]
    assert len(details) == 2
    assert result["files"][0]["kept"] == 2


def test_hosts_outside_the_list_are_refused(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item("2024-12-02", "2024-12-02"), allowedHosts=["dati.cortecostituzionale.it"]), doc.deps(sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert doc.server.requests == []


def test_dry_run_makes_no_request(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()

    result = run_grab(params(tmp_path, item("2024-12-02", "2024-12-02"), dryRun=True), doc.deps(sleeps), lambda *_: None)

    assert doc.server.requests == []
    assert result["dryRun"] is True
    assert result["files"][0]["path"].endswith("cgt_2024-12-02_2024-12-02")


def test_search_5xx_is_retried_then_transient(sleeps: list[float], tmp_path: Path) -> None:
    doc = DocTrib()
    doc.server.routes[SEARCH] = lambda _r: httpx.Response(503)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item("2024-12-02", "2024-12-02")), doc.deps(sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert len([r for r in doc.server.requests if str(r.url).startswith(SEARCH)]) == 4  # the try and three retries


@pytest.mark.parametrize(
    "meta",
    [
        {"kind": "cgt", "itemId": "cgt:x", "from": "2024-12-02"},
        {"kind": "cgt", "itemId": "cgt:x", "from": "2024-12-02", "to": "2024-12-01"},
        {"kind": "cgt", "itemId": "cgt:x", "from": "2024-12-01", "to": "2025-01-31"},  # more than one month
        {"kind": "cgt", "itemId": "cgt:x", "from": "bad", "to": "2024-12-02"},
        {"kind": "cgt", "from": "2024-12-01", "to": "2024-12-02"},
    ],
)
def test_invalid_meta_is_a_config_error(sleeps: list[float], tmp_path: Path, meta: dict[str, Any]) -> None:
    doc = DocTrib()

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, {"key": "k", "meta": meta}), doc.deps(sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert doc.server.requests == []


def test_classification_copy_agrees_with_the_convert_class_golden() -> None:
    """classify.py is a copy of the search part of the convert class' cgt.py; both are checked against the same golden file."""
    golden = json.loads(fixture_bytes("cgt/search-golden.json"))

    for name, expected in golden.items():
        result = parse_search(fixture_bytes(f"cgt/{name}"))
        assert result.declared == expected["declared"], name
        assert [result.discarded_cassazione, result.discarded_commissioni, result.discarded_altri] == expected["discarded"], name
        assert [i.__dict__ for i in result.items] == expected["items"], name
