"""Cassazione grab: one ItalGiure Solr partition (kind x anno x month, month 13 = residual) into raw page files."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import SOLR_URL, SolrFake
from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_giurisprudenza_grab.grab import GrabDeps, run_grab

FL = "id,ocr,ocrdis,anno,numdec,szdec,tipoprov,datdec,datdep,materia,presidente,relatore,kind"


def item(month: int = 3, kind: str = "snciv", anno: int = 2025) -> dict[str, Any]:
    item_id = f"cass:{kind}:{anno}:{month:02d}"
    return {
        "key": f"{item_id}|v1",
        "url": f"https://www.italgiure.giustizia.it/sncass/#/ricerca?kind={kind}&anno={anno}&mese={month}",
        "meta": {"kind": "cassazione", "itemId": item_id, "version": "1", "cassKind": kind, "anno": anno, "mese": month},
    }


def params(tmp_path: Path, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"items": list(items), "dir": str(tmp_path / "raw"), **extra}


def deps(solr: SolrFake, sleeps: list[float]) -> GrabDeps:
    return GrabDeps(transport=solr.transport, sleep=sleeps.append)


def page_files(tmp_path: Path, stem: str = "cass_snciv_2025_03") -> list[Path]:
    return sorted((tmp_path / "raw" / stem / "pages").glob("*.json"))


def test_query_for_a_month_equals_the_legacy_query_of(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=3)

    run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    first = solr.forms[0]
    assert first["q"] == 'kind:"snciv" AND anno:"2025" AND datdec:[20250301 TO 20250331]'
    assert first["wt"] == "json"
    assert first["sort"] == "id asc"
    assert first["fl"] == FL


def test_query_for_the_residual_partition_is_month_13(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=2)

    run_grab(params(tmp_path, item(13, kind="snpen")), deps(solr, sleeps), lambda *_: None)

    assert solr.forms[0]["q"] == 'kind:"snpen" AND anno:"2025" AND NOT datdec:[20250101 TO 20251231]'
    assert page_files(tmp_path, "cass_snpen_2025_13")


def test_the_partition_is_counted_first_then_paged(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=1200)

    result = run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    assert [(f["rows"], f["start"]) for f in solr.forms] == [("0", "0"), ("500", "0"), ("500", "500"), ("500", "1000")]
    assert [p.name for p in page_files(tmp_path)] == ["snciv-202503-p000.json", "snciv-202503-p001.json", "snciv-202503-p002.json"]
    assert result["files"][0]["pages"] == 3
    assert result["files"][0]["numFound"] == 1200
    assert result["files"][0]["written"] == 1200
    assert result["downloaded"] == 1
    last = json.loads(page_files(tmp_path)[2].read_text(encoding="utf-8"))
    assert len(last["response"]["docs"]) == 200


def test_page_files_are_the_raw_solr_answer(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=2)

    run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    page = json.loads(page_files(tmp_path)[0].read_text(encoding="utf-8"))
    assert page["response"]["numFound"] == 2
    assert page["response"]["docs"][0]["id"] == "snciv2025000001O"
    assert page["response"]["docs"][0]["ocr"] == ["testo della sentenza 1"]  # list-valued fields are kept as the source sent them
    sidecar = json.loads((tmp_path / "raw" / "cass_snciv_2025_03" / "partition.json").read_text(encoding="utf-8"))
    assert (sidecar["numFound"], sidecar["pages"], sidecar["written"]) == (2, 1, 2)


def test_max_docs_stops_after_the_first_page_and_trims(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=1200)

    result = run_grab(params(tmp_path, item(3), maxDocs=5), deps(solr, sleeps), lambda *_: None)

    assert [(f["rows"], f["start"]) for f in solr.forms] == [("0", "0"), ("5", "0")]
    assert len(page_files(tmp_path)) == 1
    assert len(json.loads(page_files(tmp_path)[0].read_text(encoding="utf-8"))["response"]["docs"]) == 5
    assert result["files"][0]["written"] == 5
    assert result["files"][0]["numFound"] == 1200


def test_a_server_that_ignores_rows_is_still_trimmed_to_max_docs(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=50)
    original = solr.handle

    def greedy(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if solr.forms[-1]["rows"] != "0":
            body = response.json()
            body["response"]["docs"] = [{"id": f"snciv2025{n:06d}O", "ocr": ["x"]} for n in range(1, 21)]
            return httpx.Response(200, json=body)
        return response

    run_grab(params(tmp_path, item(3), maxDocs=5), GrabDeps(transport=httpx.MockTransport(greedy), sleep=sleeps.append), lambda *_: None)

    assert len(json.loads(page_files(tmp_path)[0].read_text(encoding="utf-8"))["response"]["docs"]) == 5


def test_pause_between_requests(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=1200)

    run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    assert len(solr.forms) == 4
    assert sleeps == [1.5, 1.5, 1.5]  # before every request except the first


def test_an_empty_partition_writes_no_page_and_succeeds(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=0)

    result = run_grab(params(tmp_path, item(9)), deps(solr, sleeps), lambda *_: None)

    assert len(solr.forms) == 1
    assert page_files(tmp_path, "cass_snciv_2025_09") == []
    assert result["files"][0]["written"] == 0


def test_a_500_once_is_retried_with_back_off(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=3, fail_first=1)

    result = run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    assert result["downloaded"] == 1
    assert solr.calls == 3  # failed count, retried count, page
    assert any(delay > 1.5 for delay in sleeps)  # a back-off longer than the normal pause


def test_a_persistent_500_is_transient_after_three_retries(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=3, fail_first=99)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert solr.calls == 4  # the first try and three retries


def test_403_is_a_config_error_naming_the_host(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=3, status=403)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert "www.italgiure.giustizia.it" in str(error.value)
    assert "403" in str(error.value)
    assert solr.calls == 1  # not retried


def test_connect_error_is_transient(sleeps: list[float], tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item(3)), GrabDeps(transport=httpx.MockTransport(refuse), sleep=sleeps.append), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert "www.italgiure.giustizia.it" in str(error.value)


def test_solr_base_url_override_is_honoured(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=2)
    seen: list[str] = []

    def replay(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return solr.handle(request)

    result = run_grab(
        params(tmp_path, item(3), solrBaseUrl="http://replay.local:8080"), GrabDeps(transport=httpx.MockTransport(replay), sleep=sleeps.append), lambda *_: None,
    )

    assert result["downloaded"] == 1
    assert seen[0] == "http://replay.local:8080/sncass/isapi/hc.dll/sn.solr/sn-collection/select?app.query"


def test_a_host_outside_the_list_is_refused_before_the_request(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=2)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, item(3), allowedHosts=["dati.cortecostituzionale.it"]), deps(solr, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert solr.calls == 0


def test_resume_overwrites_an_existing_page_file_atomically(sleeps: list[float], tmp_path: Path) -> None:
    pages = tmp_path / "raw" / "cass_snciv_2025_03" / "pages"
    pages.mkdir(parents=True)
    (pages / "snciv-202503-p000.json").write_text("STALE FILE OF AN EARLIER RUN", encoding="utf-8")
    (pages / "snciv-202503-p007.json").write_text("STALE PAGE BEYOND THE PARTITION", encoding="utf-8")
    solr = SolrFake(total=3)

    run_grab(params(tmp_path, item(3)), deps(solr, sleeps), lambda *_: None)

    assert [p.name for p in page_files(tmp_path)] == ["snciv-202503-p000.json"]  # the stale tail page is gone
    assert json.loads(page_files(tmp_path)[0].read_text(encoding="utf-8"))["response"]["numFound"] == 3
    assert not list(pages.glob("*.tmp"))


def test_dry_run_makes_no_request(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=3)

    result = run_grab(params(tmp_path, item(3), dryRun=True), deps(solr, sleeps), lambda *_: None)

    assert solr.calls == 0
    assert result["dryRun"] is True
    assert result["files"][0]["path"].endswith("cass_snciv_2025_03")


@pytest.mark.parametrize(
    "meta",
    [
        {"kind": "cassazione", "itemId": "cass:snciv:2025:03", "cassKind": "snxyz", "anno": 2025, "mese": 3},
        {"kind": "cassazione", "itemId": "cass:snciv:2025:03", "cassKind": "snciv", "anno": "2025", "mese": 3},
        {"kind": "cassazione", "itemId": "cass:snciv:2025:14", "cassKind": "snciv", "anno": 2025, "mese": 14},
        {"kind": "cassazione", "itemId": "cass:snciv:2025:03", "cassKind": "snciv", "anno": 2025},
    ],
)
def test_invalid_meta_is_a_config_error(sleeps: list[float], tmp_path: Path, meta: dict[str, Any]) -> None:
    solr = SolrFake(total=1)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, {"key": "k", "meta": meta}), deps(solr, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert solr.calls == 0


def test_the_endpoint_constant_is_the_official_one(sleeps: list[float], tmp_path: Path) -> None:
    solr = SolrFake(total=1)
    seen: list[str] = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.copy_with(query=None)))
        return solr.handle(request)

    run_grab(params(tmp_path, item(3)), GrabDeps(transport=httpx.MockTransport(spy), sleep=sleeps.append), lambda *_: None)

    assert set(seen) == {SOLR_URL}
