"""HTTP client factory: browser User-Agent, no redirect following, host allow-list enforced before every request."""
from __future__ import annotations

import ssl
from pathlib import Path

import certifi
import httpx

from dp_it_giurisprudenza_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError

# def.finanze.it sends only its leaf; the Sectigo intermediate is pinned here (certs/README.md has source and fingerprint)
PINNED_CERTS: tuple[Path, ...] = (Path(__file__).resolve().parent / "certs" / "sectigo-public-server-authentication-ca-ov-r36.pem",)

# Corte costituzionale open data, ItalGiure (Cassazione) and MEF DocTrib (CGT)
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = ("dati.cortecostituzionale.it", "www.italgiure.giustizia.it", "def.finanze.it")
TIMEOUT_SEC = 120.0
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def ssl_context(*, verify_tls: bool) -> ssl.SSLContext:
    """certifi roots plus the pinned intermediates; ``verify_tls=False`` gives a context that does not verify (the documented escape)."""
    context = ssl.create_default_context(cafile=certifi.where())
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    for pinned in PINNED_CERTS:
        context.load_verify_locations(cafile=str(pinned))
    return context


def build_client(
    allowed_hosts: tuple[str, ...] | list[str],
    transport: httpx.BaseTransport | None = None,
    *,
    verify_tls: bool = True,
) -> httpx.Client:
    """A client that refuses (config_error) any request whose host is not in ``allowed_hosts``; redirects are never followed.

    ``verify_tls=False`` is the documented escape for a government certificate chain that a container cannot verify (P79-15); it is only ever
    passed for the configured ItalGiure / DocTrib host.
    """
    allowed = frozenset(host.lower() for host in allowed_hosts)

    def check_host(request: httpx.Request) -> None:
        host = request.url.host.lower()
        if host not in allowed:
            msg = f"host {host} is not in the allowed hosts {sorted(allowed)}"
            raise GrabError(CONFIG_ERROR, msg)

    return httpx.Client(
        transport=transport,
        follow_redirects=False,
        timeout=TIMEOUT_SEC,
        verify=ssl_context(verify_tls=verify_tls),
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        event_hooks={"request": [check_host]},
    )


def status_error(url: str, status: int) -> GrabError:
    """5xx and 429 are worth retrying; any other failing status means the request itself is wrong or the resource is gone."""
    code = TRANSIENT_ERROR if status >= 500 or status == 429 else CONFIG_ERROR  # noqa: PLR2004
    return GrabError(code, f"HTTP {status} from {url}")
