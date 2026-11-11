"""CGT (MEF DocTrib, ambito G): one calendar-month window into raw search pages and detail pages (port of DocTribPages + DocTribGiurisGrab).

The source serves at most 50 decisions per answer, has no pagination and declares the real total in ``contatoreGiurisprudenza``: a window is
complete when ``served == counter``; otherwise a month is split into days. A day that is still truncated is a transient error naming the day (the
legacy took what was there; here the item is retried instead of silently incomplete). The list mixes bodies: merit courts are kept, the rest is
counted (``classify.py``). Detail pages are fetched one at a time, paced, with one patient retry after a long pause (P79-16).
"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

import httpx

from dp_it_giurisprudenza_grab.cc import safe_name
from dp_it_giurisprudenza_grab.classify import SearchSlice, parse_search
from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_it_giurisprudenza_grab.http import status_error

if TYPE_CHECKING:
    from pathlib import Path

    from dp_it_giurisprudenza_grab.cassazione import Pacer

BASE_URL = "https://def.finanze.it/DocTribFrontend/"
HOME_URL = BASE_URL + "RS2_HomePage.jsp"
SEARCH_URL = BASE_URL + "executeAdvancedGiurisprudenzaSearch.do"
DETAIL_URL = BASE_URL + "getGiurisprudenzaDetail.do"
MAX_RETRIES = 3
BACKOFF_SEC = 5.0
DETAIL_PATIENCE_SEC = 30.0
HEADERS = {"Accept-Language": "it", "Referer": HOME_URL}


@dataclass(frozen=True)
class CgtOptions:
    max_items: int | None = None


@dataclass(frozen=True)
class Window:
    """A month (``day`` None) or a day: the only two shapes the search form is known to answer."""

    year: int
    month: int
    day: int | None = None

    @property
    def label(self) -> str:
        return f"{self.year}-{self.month:02d}" + (f"-{self.day:02d}" if self.day else "")


def parse_date(key: str, meta: dict[str, Any], name: str) -> date:
    try:
        return date.fromisoformat(meta[name])
    except (KeyError, TypeError, ValueError):
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.{name} must be an ISO date YYYY-MM-DD") from None


def validate_meta(key: str, meta: dict[str, Any]) -> None:
    if not isinstance(meta.get("itemId"), str) or not meta["itemId"]:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.itemId is required")
    first, last = parse_date(key, meta, "from"), parse_date(key, meta, "to")
    if last < first:
        raise GrabError(CONFIG_ERROR, f"item {key}: meta.to must not be before meta.from")
    if (first.year, first.month) != (last.year, last.month):
        raise GrabError(CONFIG_ERROR, f"item {key}: an item is one calendar month, got {first} .. {last}")


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


def search_form(window: Window) -> dict[str, str]:
    """The advanced-search form of the legacy DocTribPages (empty fields included: omitting them makes the POST fail)."""
    day = str(window.day) if window.day else ""
    return {
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
        "parole": "",
        "giornoDataEmissioneDa": day,
        "meseDataEmissioneDa": str(window.month),
        "annoDataEmissioneDa": str(window.year),
        "giornoDataEmissioneA": day,
        "meseDataEmissioneA": str(window.month),
        "annoDataEmissioneA": str(window.year),
    }


class DocTribSession:
    """The DocTrib session: the home page sets the JSESSIONID that the client's cookie jar re-sends on every request."""

    def __init__(self, client: httpx.Client, pacer: Pacer) -> None:
        self._client = client
        self._pacer = pacer
        self._ready = False

    def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last: GrabError | None = None
        for attempt in range(MAX_RETRIES + 1):
            self._pacer.wait(BACKOFF_SEC * attempt if attempt else 0.0)
            try:
                response = self._client.request(method, url, headers=HEADERS, **kwargs)
            except httpx.TransportError as error:
                last = GrabError(TRANSIENT_ERROR, f"request to {url} failed: {error}")
                continue
            if response.status_code == httpx.codes.OK:
                return response
            failure = status_error(url, response.status_code)
            if failure.code == CONFIG_ERROR:
                raise failure
            last = failure
        assert last is not None  # the loop ran at least once
        raise last

    def _ensure(self) -> None:
        if not self._ready:
            self._send("GET", HOME_URL)
            self._ready = True

    def search(self, window: Window) -> bytes:
        self._ensure()
        return self._send("POST", SEARCH_URL, data=search_form(window)).content

    def detail(self, guid: str) -> bytes:
        """One detail page; a failure is retried once after a long pause (the source slows down under use and answers 500 now and then)."""
        self._ensure()
        try:
            return self._send("GET", DETAIL_URL, params={"id": guid}).content
        except GrabError:
            self._pacer.wait(DETAIL_PATIENCE_SEC)
            return self._send("GET", DETAIL_URL, params={"id": guid}).content


def _days(first: date, last: date) -> list[Window]:
    return [Window(first.year, first.month, day) for day in range(first.day, last.day + 1)]


def _whole_month(first: date, last: date) -> bool:
    return first.day == 1 and last.day == calendar.monthrange(last.year, last.month)[1]


def _served(result: SearchSlice) -> int:
    return len(result.items) + result.discarded_cassazione + result.discarded_commissioni + result.discarded_altri


def _require_complete(result: SearchSlice, window: Window) -> None:
    if result.declared > _served(result):
        msg = f"the day {window.label} is still truncated: {_served(result)} served of {result.declared} declared; the source cannot slice it further"
        raise GrabError(TRANSIENT_ERROR, msg)


class _MonthGrab:
    """The state of one item: the windows taken, the totals, the kept decisions."""

    def __init__(self, entry: dict[str, Any], base: Path, session: DocTribSession, options: CgtOptions) -> None:
        self.entry = entry
        self.meta = entry["meta"]
        self.session = session
        self.options = options
        self.first = date.fromisoformat(self.meta["from"])
        self.last = date.fromisoformat(self.meta["to"])
        self.stem_dir = base / safe_name(self.meta["itemId"])
        self.guids: list[str] = []
        self.windows: list[dict[str, Any]] = []
        self.totals = {"counter": 0, "served": 0, "cassazione": 0, "commissioni": 0, "altri": 0}

    def _take(self, window: Window) -> SearchSlice:
        raw = self.session.search(window)
        result = parse_search(raw)
        _write_atomic(self.stem_dir / f"search-{window.label}.html", raw)
        self.windows.append({"window": window.label, "counter": result.declared, "served": _served(result), "kept": len(result.items)})
        return result

    def _add(self, result: SearchSlice) -> None:
        self.totals["counter"] += result.declared
        self.totals["served"] += _served(result)
        self.totals["cassazione"] += result.discarded_cassazione
        self.totals["commissioni"] += result.discarded_commissioni
        self.totals["altri"] += result.discarded_altri
        self.guids.extend(i.guid for i in result.items if i.guid not in self.guids)

    def _enough(self) -> bool:
        return self.options.max_items is not None and len(self.guids) >= self.options.max_items

    def _take_days(self) -> None:
        for day in _days(self.first, self.last):
            if self._enough():
                break
            result = self._take(day)
            _require_complete(result, day)
            self._add(result)

    def collect(self) -> None:
        """A whole month is one window; if the source says it holds more than it served, the month page is dropped and every day is taken."""
        if not _whole_month(self.first, self.last):
            self._take_days()
            return
        month = Window(self.first.year, self.first.month)
        result = self._take(month)
        if result.declared <= _served(result):
            self._add(result)
            return
        self.windows.pop()
        (self.stem_dir / f"search-{month.label}.html").unlink(missing_ok=True)
        self._take_days()

    def fetch_details(self) -> tuple[list[str], int, list[str]]:
        chosen = self.guids if self.options.max_items is None else self.guids[: self.options.max_items]
        existing, failed = 0, []
        for guid in chosen:
            target = self.stem_dir / f"{guid}.html"
            if target.exists():
                existing += 1
                continue
            try:
                _write_atomic(target, self.session.detail(guid))
            except GrabError:
                failed.append(guid)
        return chosen, existing, failed


def grab_cgt_item(entry: dict[str, Any], base: Path, session: DocTribSession, options: CgtOptions) -> dict[str, Any]:
    """One month window into ``<base>/<stem>``: ``search-<window>.html``, ``<guid>.html`` per kept decision and ``slice.json``."""
    month = _MonthGrab(entry, base, session, options)
    month.collect()
    chosen, existing, failed = month.fetch_details()
    totals = month.totals
    info = {
        "key": entry["key"],
        "itemId": month.meta["itemId"],
        "from": month.meta["from"],
        "to": month.meta["to"],
        "counter": totals["counter"],
        "served": totals["served"],
        "kept": len(chosen),
        "discarded": {"cassazione": totals["cassazione"], "commissioni": totals["commissioni"], "altri": totals["altri"]},
        "guids": chosen,
        "windows": month.windows,
        "existing": existing,
        "failed": failed,
    }
    _write_atomic(month.stem_dir / "slice.json", json.dumps(info, ensure_ascii=False, indent=2).encode("utf-8"))
    if failed:
        msg = f"item {entry['key']}: {len(failed)} of {len(chosen)} detail pages failed ({', '.join(failed)}); the others were written, retry the item"
        raise GrabError(TRANSIENT_ERROR, msg)
    return {"key": entry["key"], "path": str(month.stem_dir), "kept": len(chosen), "counter": totals["counter"], "status": "downloaded"}
