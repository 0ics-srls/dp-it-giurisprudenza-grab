"""TLS trust: DocTrib (def.finanze.it) sends only its leaf certificate; the Sectigo intermediate is pinned in the repo (79b)."""
from __future__ import annotations

import hashlib
import re
import ssl
from collections.abc import Mapping
from pathlib import Path

import pytest

from dp_it_giurisprudenza_grab import http

CERTS = Path(__file__).resolve().parent.parent.parent / "src" / "dp_it_giurisprudenza_grab" / "certs"
PEM = CERTS / "sectigo-public-server-authentication-ca-ov-r36.pem"
INTERMEDIATE_SUBJECT = "Sectigo Public Server Authentication CA OV R36"


def _der(pem: Path) -> bytes:
    return ssl.PEM_cert_to_DER_cert(pem.read_text(encoding="ascii"))


def _common_names(cert: Mapping[str, object]) -> list[str]:
    subject = cert["subject"]
    assert isinstance(subject, tuple)
    return [value for rdn in subject for key, value in rdn if key == "commonName"]


@pytest.mark.unit
def test_should_pin_the_documented_intermediate_when_the_bundle_file_is_read() -> None:
    fingerprint = hashlib.sha256(_der(PEM)).hexdigest().upper()
    readme = (CERTS / "README.md").read_text(encoding="utf-8")
    documented = re.search(r"SHA-256[^0-9A-F]*((?:[0-9A-F]{2}:){31}[0-9A-F]{2})", readme)
    assert documented is not None, "certs/README.md must document the SHA-256 fingerprint"
    assert documented.group(1).replace(":", "") == fingerprint
    assert "http://crt.sectigo.com/SectigoPublicServerAuthenticationCAOVR36.crt" in readme
    assert INTERMEDIATE_SUBJECT in readme


@pytest.mark.unit
def test_should_trust_the_intermediate_when_a_verifying_client_is_built() -> None:
    context = http.ssl_context(verify_tls=True)
    assert context.verify_mode == ssl.CERT_REQUIRED
    names = [name for cert in context.get_ca_certs() for name in _common_names(cert)]
    assert INTERMEDIATE_SUBJECT in names
    assert "Sectigo Public Server Authentication Root R46" in names, "the certifi roots stay trusted"
    client = http.build_client(["def.finanze.it"], verify_tls=True)
    client.close()


@pytest.mark.unit
def test_should_keep_the_escape_when_verify_tls_is_false() -> None:
    context = http.ssl_context(verify_tls=False)
    assert context.verify_mode == ssl.CERT_NONE
    assert context.check_hostname is False


@pytest.mark.integration
def test_should_verify_doctrib_when_the_pinned_intermediate_is_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket  # noqa: PLC0415

    monkeypatch.undo()  # conftest forbids the network in unit tests; this one is marked integration
    assert socket.socket.connect is not None
    with http.build_client(["def.finanze.it"], verify_tls=True) as client:
        response = client.get("https://def.finanze.it/DocTribWeb/")
    assert response.status_code < 600  # any HTTP answer means the handshake verified
