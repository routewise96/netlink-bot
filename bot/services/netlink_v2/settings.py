"""NetLink 2.0 feature flag / pilot allowlist settings.

All values come from the environment only. The v2 flow is DISABLED unless
every required value is present and correct; an empty pilot allowlist means
NOBODY has v2 access (no wildcards, no defaults).

Control Plane origin policy: HTTPS only, except explicit loopback hosts
(localhost / 127.0.0.1 / ::1) which may use plain HTTP for local development.
Userinfo, query strings, fragments and path prefixes are rejected.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

ENV_ENABLED = "NETLINK_V2_ENABLED"
ENV_URL = "NETLINK_CONTROL_PLANE_URL"
ENV_API_KEY = "NETLINK_CONTROL_PLANE_INTERNAL_API_KEY"
ENV_PILOT_IDS = "NETLINK_V2_PILOT_TELEGRAM_IDS"

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def normalize_base_url(raw: str) -> str | None:
    """Validate and normalize the Control Plane origin.

    Returns the normalized base URL (scheme://host[:port], no trailing slash)
    or None when the value does not satisfy the origin policy.
    """
    try:
        parts = urlsplit(raw.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = parts.hostname
    if not host:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment:
        return None
    if parts.path not in ("", "/"):
        return None
    if parts.scheme == "http" and host.lower() not in LOOPBACK_HOSTS:
        return None
    netloc = parts.netloc
    return f"{parts.scheme}://{netloc}"


@dataclass(frozen=True)
class V2Settings:
    enabled_flag: bool
    base_url: str
    internal_api_key: str
    pilot_telegram_ids: frozenset[int]
    url_valid: bool


def load_v2_settings() -> V2Settings:
    raw_ids = os.environ.get(ENV_PILOT_IDS, "")
    pilot_ids = frozenset(
        int(part.strip())
        for part in raw_ids.split(",")
        if part.strip().isascii() and part.strip().isdigit()
    )
    raw_url = os.environ.get(ENV_URL, "").strip()
    base_url = normalize_base_url(raw_url)
    return V2Settings(
        enabled_flag=os.environ.get(ENV_ENABLED, "").strip().lower() == "true",
        base_url=base_url if base_url is not None else "",
        internal_api_key=os.environ.get(ENV_API_KEY, "").strip(),
        pilot_telegram_ids=pilot_ids,
        url_valid=bool(raw_url) and base_url is not None,
    )


def is_configured() -> bool:
    settings_ = load_v2_settings()
    return (
        settings_.enabled_flag
        and settings_.url_valid
        and bool(settings_.internal_api_key)
    )


def is_enabled() -> bool:
    """True only when the flag is 'true' AND a valid origin AND key are set."""
    return is_configured()


def is_pilot_user(telegram_user_id: int) -> bool:
    if not is_configured():
        return False
    return telegram_user_id in load_v2_settings().pilot_telegram_ids


def config_problems() -> list[str]:
    """Human-readable missing-configuration hints (never secret values)."""
    settings_ = load_v2_settings()
    problems: list[str] = []
    if not settings_.enabled_flag:
        problems.append(f"{ENV_ENABLED} is not 'true'")
    if not settings_.url_valid:
        problems.append(
            f"{ENV_URL} must be a valid https origin (plain http is allowed "
            "only for localhost/127.0.0.1/::1, without userinfo, query, "
            "fragment or path prefix)"
        )
    if not settings_.internal_api_key:
        problems.append(f"{ENV_API_KEY} is empty")
    if settings_.enabled_flag and not settings_.pilot_telegram_ids:
        problems.append(f"{ENV_PILOT_IDS} is empty: nobody has v2 access")
    return problems
