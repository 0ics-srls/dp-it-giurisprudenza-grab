"""Classification of the DocTrib giurisprudenza search list: keep the merit courts, count the rest.

This module is a COPY of the search part of the convert class (dp_it_giurisprudenza_convert.cgt: js_unescape, classify_ente, parse_cgt_ente,
parse_search); both are checked against the same golden file (tests/fixtures/cgt/search-golden.json), so they cannot drift apart silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from defusedxml import ElementTree as SafeElementTree

_XML_RESULT = re.compile(r"var xmlResult\s*=\s*'(.*?)';", re.DOTALL)
# Estremi layout: tipo, "del" date, optional "n." number, "-" body, optional "- Sezione/Collegio" section
_ESTREMI = re.compile(
    r"^(?P<tipo>.+?)\s+del\s+(?P<data>\d{2}/\d{2}/\d{4})(?:\s+n\.\s*(?P<num>\S+))?\s*-\s*(?P<ente>.+?)(?:\s*-\s*Sezione/Collegio\s*(?P<sez>\S+))?$",
)
# the place comes with its own preposition: di Ragusa, del Piemonte, della Campania, delle Marche, dell'Emilia Romagna
_CGT_ENTE = re.compile(
    r"^Corte di giustizia tributaria di (?P<grado>primo|secondo) grado\s+(?:dell'|delle\s|della\s|del\s|di\s)\s*(?P<luogo>.+)$",
    re.IGNORECASE,
)
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
_JS_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}
_HEX = re.compile(r"[0-9a-fA-F]+")


@dataclass(frozen=True)
class CgtItem:
    """One merit-court decision of a search list (no text yet)."""

    guid: str
    tipo: str
    data: str
    numero: str | None
    grado: int
    luogo: str
    sezione: str | None
    estremi: str


@dataclass(frozen=True)
class SearchSlice:
    declared: int
    items: list[CgtItem]
    discarded_cassazione: int
    discarded_commissioni: int
    discarded_altri: int


def js_unescape(literal: str) -> str:
    """Decodes the body of a JavaScript single-quoted string literal (``\\uXXXX``, ``\\xHH``, ``\\n``..., any other ``\\c`` is ``c``)."""
    out: list[str] = []
    i = 0
    size = len(literal)
    while i < size:
        char = literal[i]
        if char != "\\" or i + 1 >= size:
            out.append(char)
            i += 1
            continue
        nxt = literal[i + 1]
        if nxt == "u" and _HEX.fullmatch(literal[i + 2 : i + 6]) and len(literal[i + 2 : i + 6]) == 4:  # noqa: PLR2004
            out.append(chr(int(literal[i + 2 : i + 6], 16)))
            i += 6
        elif nxt == "x" and _HEX.fullmatch(literal[i + 2 : i + 4]) and len(literal[i + 2 : i + 4]) == 2:  # noqa: PLR2004
            out.append(chr(int(literal[i + 2 : i + 4], 16)))
            i += 4
        elif nxt in _JS_SIMPLE:
            out.append(_JS_SIMPLE[nxt])
            i += 2
        elif nxt == "\n":
            i += 2  # line continuation
        else:
            out.append(nxt)  # identity: \' \" \\ \/ and "\a-grave"
            i += 2
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")  # joins surrogate pairs written as two \uXXXX


def _extract_xml(response: bytes, carrier: re.Pattern[str]) -> Any:
    page = response.decode("iso-8859-1")
    match = carrier.search(page)
    if match is None:
        return None
    xml = _XML_DECLARATION.sub("", js_unescape(match.group(1)))
    return SafeElementTree.fromstring(xml.encode("utf-8"))


def classify_ente(ente: str) -> str:
    """Who issued the decision: ``cgt``, ``cassazione``, ``commissione`` (pre-reform) or ``altro``."""
    if _CGT_ENTE.match(ente):
        return "cgt"
    if "corte di cassazione" in ente.lower():
        return "cassazione"
    lowered = ente.lower()
    if lowered.startswith(("commissione tributaria", "comm. trib.")):
        return "commissione"
    return "altro"


def parse_cgt_ente(ente: str) -> tuple[int, str]:
    """``(grado, luogo)`` of a merit court, the place without its preposition."""
    match = _CGT_ENTE.match(ente)
    if match is None:
        message = f"not a Corte di giustizia tributaria: {ente!r}"
        raise ValueError(message)
    return (1 if match.group("grado").lower() == "primo" else 2), match.group("luogo").strip()


def _iso(date_text: str) -> str:
    return datetime.strptime(date_text, "%d/%m/%Y").date().isoformat()  # noqa: DTZ007 - a calendar date, no time zone involved


def parse_search(response: bytes) -> SearchSlice:
    """Reads one search answer: the declared total of the slice and the served decisions, merit courts kept, the rest counted."""
    root = _extract_xml(response, _XML_RESULT)
    if root is None:
        return SearchSlice(0, [], 0, 0, 0)
    counter = root.find(".//contatoreGiurisprudenza")
    declared = int(counter.text) if counter is not None and counter.text and counter.text.strip().isdigit() else 0
    items: list[CgtItem] = []
    discarded = {"cassazione": 0, "commissione": 0, "altro": 0}
    for provvedimento in root.iter("Provvedimento"):
        guid = provvedimento.get("idProvvedimento")
        estremi_node = provvedimento.find("estremi")
        estremi = (estremi_node.text or "").strip() if estremi_node is not None else ""
        if not guid or not estremi:
            continue
        match = _ESTREMI.match(estremi)
        if match is None:
            message = f"estremi giurisprudenza non riconosciuti: {estremi!r}"
            raise ValueError(message)
        ente = match.group("ente").strip()
        kind = classify_ente(ente)
        if kind != "cgt":
            discarded[kind] += 1
            continue
        grado, luogo = parse_cgt_ente(ente)
        items.append(CgtItem(guid, match.group("tipo").strip(), _iso(match.group("data")), match.group("num"), grado, luogo, match.group("sez"), estremi))
    return SearchSlice(declared, items, discarded["cassazione"], discarded["commissione"], discarded["altro"])


