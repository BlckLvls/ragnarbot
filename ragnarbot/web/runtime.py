"""Runtime discovery helpers for the profile-local web console."""

import json
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::", "[::]"})


@dataclass(frozen=True)
class WebProbe:
    """Result of probing a configured web console endpoint."""

    reachable: bool
    profile: str | None = None
    error: str | None = None


def browser_host(bind_host: str) -> str:
    """Turn a listener address into an address a local browser can open."""
    return "127.0.0.1" if bind_host.strip() in WILDCARD_HOSTS else bind_host.strip()


def web_url(host_or_config: Any, port: int | None = None) -> str:
    """Return the local browser URL for a WebConfig or explicit host and port."""
    if port is None:
        host = str(host_or_config.host)
        port = int(host_or_config.port)
    else:
        host = str(host_or_config)
    host = browser_host(host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def lan_ip() -> str | None:
    """Best-effort discovery of the machine's outward-facing IPv4 address."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            address = str(sock.getsockname()[0])
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass

    try:
        addresses = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return None
    return next((address for address in addresses if not address.startswith("127.")), None)


def lan_web_url(config: Any) -> str | None:
    """Return a LAN URL when the web console listens on a wildcard address."""
    if str(config.host).strip() not in WILDCARD_HOSTS:
        return None
    address = lan_ip()
    return web_url(address, int(config.port)) if address else None


def probe_web(config: Any, *, expected_profile: str | None = None, timeout: float = 0.35) -> WebProbe:
    """Probe `/api/status/full` and optionally verify which profile answered."""
    url = f"{web_url(config)}/api/status/full"
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - local configured URL
            if response.status != 200:
                return WebProbe(False, error=f"HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return WebProbe(False, error=f"HTTP {exc.code}")
    except (OSError, URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return WebProbe(False, error=str(exc))

    profile = payload.get("profile") if isinstance(payload, dict) else None
    if expected_profile is not None and profile != expected_profile:
        return WebProbe(
            False,
            profile=profile,
            error=f"port is serving profile '{profile or 'unknown'}'",
        )
    return WebProbe(True, profile=profile)


def wait_for_web(
    config: Any,
    *,
    expected_profile: str | None = None,
    timeout: float = 5.0,
    interval: float = 0.1,
) -> WebProbe:
    """Wait briefly for a newly started web console to answer."""
    deadline = time.monotonic() + timeout
    last = WebProbe(False, error="not reachable")
    while time.monotonic() < deadline:
        last = probe_web(config, expected_profile=expected_profile)
        if last.reachable or last.profile is not None:
            return last
        time.sleep(interval)
    return last


def port_is_available(host: str, port: int) -> bool:
    """Return whether a TCP listener can bind the configured address and port."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    bind_host = host.strip() or ("::" if family == socket.AF_INET6 else "0.0.0.0")
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((bind_host, port))
    except OSError:
        return False
    return True
