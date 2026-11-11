"""Corte costituzionale dump grab: index check, conditional GET, atomic write, sidecar."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import CC_INDEX_URL, CC_PRONUNCE_2001, Server, bundle_zip, make_zip
from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_giurisprudenza_grab.grab import GrabDeps, run_grab

ITEM: dict[str, Any] = {
    "key": "cc:pronunce_2001_oggi|v1",
    "url": CC_PRONUNCE_2001,
    "meta": {"kind": "cc", "itemId": "cc:pronunce_2001_oggi", "version": "1", "bundle": "pronunce_2001_oggi"},
}
REGISTRY_ITEM: dict[str, Any] = {
    "key": "cc:norme_ordinanze|v1",
    "url": "https://dati.cortecostituzionale.it/opendata/Norme_impugnate/Cc_Opendata_NormeImpugnate_RegistroOrdinanze_json.zip",
    "meta": {"kind": "cc", "itemId": "cc:norme_ordinanze", "version": "1", "bundle": "norme_ordinanze"},
}


def params(tmp_path: Path, *items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"items": list(items), "dir": str(tmp_path / "raw"), **extra}


def deps(server: Server, sleeps: list[float]) -> GrabDeps:
    return GrabDeps(transport=server.transport, sleep=sleeps.append)


def progress() -> tuple[list[tuple[int, str]], Any]:
    seen: list[tuple[int, str]] = []
    return seen, lambda percent, message: seen.append((percent, message))


def test_happy_path_writes_zip_and_sidecar(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    seen, sink = progress()

    result = run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), sink)

    zip_path = tmp_path / "raw" / "cc_pronunce_2001_oggi.zip"
    sidecar = json.loads((tmp_path / "raw" / "cc_pronunce_2001_oggi.dump.json").read_text(encoding="utf-8"))
    data = bundle_zip()
    assert zip_path.read_bytes() == data
    assert sidecar["sha256"] == hashlib.sha256(data).hexdigest()
    assert sidecar["bytes"] == len(data)
    assert sidecar["last_modified"] == "Wed, 11 Nov 2026 04:32:02 GMT"
    assert sidecar["etag"] == 'W/"abc-1"'
    assert sidecar["status"] == "downloaded"
    assert sidecar["url"] == CC_PRONUNCE_2001
    assert result["downloaded"] == 1
    assert result["unchanged"] == 0
    assert result["files"][0]["path"] == str(zip_path)
    assert not list((tmp_path / "raw").glob("*.part"))
    assert [percent for percent, _ in seen][-1] == 100
    assert cc_server.urls() == [CC_INDEX_URL, CC_PRONUNCE_2001]  # the index is read once, then the dump


def test_second_run_with_304_keeps_the_file_and_reports_unchanged(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)
    zip_path = tmp_path / "raw" / "cc_pronunce_2001_oggi.zip"
    first_bytes = zip_path.read_bytes()
    seen_headers: list[httpx.Headers] = []

    def conditional(request: httpx.Request) -> httpx.Response:
        seen_headers.append(request.headers)
        return httpx.Response(304)

    cc_server.routes[CC_PRONUNCE_2001] = conditional

    result = run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert seen_headers[0]["If-Modified-Since"] == "Wed, 11 Nov 2026 04:32:02 GMT"
    assert seen_headers[0]["If-None-Match"] == 'W/"abc-1"'
    assert zip_path.read_bytes() == first_bytes
    assert result["downloaded"] == 0
    assert result["unchanged"] == 1
    assert result["files"][0]["status"] == "unchanged"
    assert json.loads((tmp_path / "raw" / "cc_pronunce_2001_oggi.dump.json").read_text(encoding="utf-8"))["status"] == "unchanged"


def test_lost_zip_is_downloaded_again_without_conditional_headers(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)
    (tmp_path / "raw" / "cc_pronunce_2001_oggi.zip").unlink()
    cc_server.requests.clear()

    result = run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert result["downloaded"] == 1
    assert "If-Modified-Since" not in cc_server.requests[-1].headers


def test_index_without_the_dump_is_a_config_error_before_any_download(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    cc_server.routes[CC_INDEX_URL] = lambda _r: httpx.Response(200, text="<html>no dumps listed</html>")

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert "pronunce_2001_oggi" in str(error.value)
    assert cc_server.urls() == [CC_INDEX_URL]
    assert not (tmp_path / "raw" / "cc_pronunce_2001_oggi.zip").exists()


def test_non_zip_body_is_transient_and_leaves_nothing(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    cc_server.routes[CC_PRONUNCE_2001] = lambda _r: httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert not list((tmp_path / "raw").glob("cc_pronunce_2001_oggi*"))


def test_bundle_without_year_entries_is_transient(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    flat = make_zip({"readme.txt": "no year zips here"})
    cc_server.routes[CC_PRONUNCE_2001] = lambda _r: httpx.Response(200, content=flat)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert "year" in str(error.value)
    assert not list((tmp_path / "raw").glob("cc_pronunce_2001_oggi*"))


def test_registry_zip_needs_a_json_entry(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    url = REGISTRY_ITEM["url"]
    cc_server.routes[url] = lambda _r: httpx.Response(200, content=make_zip({"Cc_Opendata_NormeImpugnate_RegistroOrdinanze.json": "{}"}))

    result = run_grab(params(tmp_path, REGISTRY_ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert result["downloaded"] == 1
    assert (tmp_path / "raw" / "cc_norme_ordinanze.zip").exists()
    cc_server.routes[url] = lambda _r: httpx.Response(200, content=make_zip({"readme.txt": "x"}))
    (tmp_path / "raw" / "cc_norme_ordinanze.dump.json").unlink()
    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, REGISTRY_ITEM), deps(cc_server, sleeps), lambda *_: None)
    assert error.value.code == TRANSIENT_ERROR


def test_truncated_body_leaves_no_part_and_no_final_file(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    data = bundle_zip()
    cc_server.routes[CC_PRONUNCE_2001] = lambda _r: httpx.Response(200, content=data[: len(data) // 2], headers={"Content-Length": str(len(data))})

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert "truncated" in str(error.value)
    assert not list((tmp_path / "raw").glob("cc_pronunce_2001_oggi*"))


def test_connection_broken_mid_stream_is_transient_and_cleans_up(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    data = bundle_zip()

    def broken(_request: httpx.Request) -> httpx.Response:
        def body() -> Any:
            yield data[:100]
            raise httpx.ReadError("connection reset")

        return httpx.Response(200, content=body())

    cc_server.routes[CC_PRONUNCE_2001] = broken

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert not list((tmp_path / "raw").glob("cc_pronunce_2001_oggi*"))


@pytest.mark.parametrize(("status", "code"), [(500, TRANSIENT_ERROR), (503, TRANSIENT_ERROR), (429, TRANSIENT_ERROR), (404, CONFIG_ERROR), (403, CONFIG_ERROR)])
def test_http_status_classification(cc_server: Server, sleeps: list[float], tmp_path: Path, status: int, code: str) -> None:
    cc_server.routes[CC_PRONUNCE_2001] = lambda _r: httpx.Response(status, text="no")

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == code
    assert str(status) in str(error.value)


def test_index_failure_is_classified_too(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    cc_server.routes[CC_INDEX_URL] = lambda _r: httpx.Response(503)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert cc_server.urls() == [CC_INDEX_URL]


def test_network_error_is_transient(sleeps: list[float], tmp_path: Path) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM), GrabDeps(transport=httpx.MockTransport(refuse), sleep=sleeps.append), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR


def test_host_outside_the_list_is_refused_before_the_request(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, ITEM, allowedHosts=["example.org"]), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert "dati.cortecostituzionale.it" in str(error.value)
    assert cc_server.requests == []


def test_dry_run_makes_no_request_and_writes_nothing(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    result = run_grab(params(tmp_path, ITEM, dryRun=True), deps(cc_server, sleeps), lambda *_: None)

    assert cc_server.requests == []
    assert result["dryRun"] is True
    assert result["downloaded"] == 0
    assert result["files"][0]["path"].endswith("cc_pronunce_2001_oggi.zip")
    assert not (tmp_path / "raw").exists()


@pytest.mark.parametrize(
    "bad_items",
    [
        None,
        [],
        [{"key": "", "meta": {"kind": "cc", "bundle": "pronunce_2001_oggi", "itemId": "cc:pronunce_2001_oggi"}}],
        [{"key": "k", "meta": {"kind": "xyz", "itemId": "x"}}],
        [{"key": "k", "meta": {"kind": "cc", "itemId": "cc:nonexistent", "bundle": "nonexistent"}}],
        [{"key": "k", "meta": {"kind": "cc", "bundle": "pronunce_2001_oggi"}}],
    ],
)
def test_invalid_items_are_config_errors(cc_server: Server, sleeps: list[float], tmp_path: Path, bad_items: Any) -> None:
    with pytest.raises(GrabError) as error:
        run_grab({"items": bad_items, "dir": str(tmp_path)}, deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR
    assert cc_server.requests == []


def test_dir_is_required(cc_server: Server, sleeps: list[float]) -> None:
    with pytest.raises(GrabError) as error:
        run_grab({"items": [ITEM]}, deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == CONFIG_ERROR


def test_one_failing_item_does_not_stop_the_others_and_the_run_reports_it(cc_server: Server, sleeps: list[float], tmp_path: Path) -> None:
    cc_server.routes[REGISTRY_ITEM["url"]] = lambda _r: httpx.Response(500)

    with pytest.raises(GrabError) as error:
        run_grab(params(tmp_path, REGISTRY_ITEM, ITEM), deps(cc_server, sleeps), lambda *_: None)

    assert error.value.code == TRANSIENT_ERROR
    assert "cc:norme_ordinanze|v1" in str(error.value)
    assert (tmp_path / "raw" / "cc_pronunce_2001_oggi.zip").exists()  # the healthy item was still written
