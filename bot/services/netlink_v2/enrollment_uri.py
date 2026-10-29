"""Canonical v1 client-side enrollment URI: netlink://enroll?code=<code>."""
from __future__ import annotations

from urllib.parse import parse_qs, quote, urlparse

SCHEME = "netlink"
HOST = "enroll"


def build(code: str) -> str:
    return f"{SCHEME}://{HOST}?code={quote(code, safe='')}"


def build_enrollment_uri(code: str) -> str:
    """Alias matching the Control Plane emulator contract."""
    return build(code)


def parse(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != SCHEME:
        raise ValueError("not a netlink enrollment URI")
    if parsed.netloc != HOST:
        raise ValueError(f"unexpected enrollment URI host: {parsed.netloc!r}")
    codes = parse_qs(parsed.query).get("code")
    if not codes or not codes[0]:
        raise ValueError("enrollment URI has no code parameter")
    return codes[0]
