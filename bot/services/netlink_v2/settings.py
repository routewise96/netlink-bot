"""NetLink 2.0 feature flag / pilot allowlist settings.

All values come from the environment only. The v2 flow is DISABLED unless
every required value is present and correct; an empty pilot allowlist means
NOBODY has v2 access (no wildcards, no defaults).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

ENV_ENABLED = "NETLINK_V2_ENABLED"
ENV_URL = "NETLINK_CONTROL_PLANE_URL"
ENV_API_KEY = "NETLINK_CONTROL_PLANE_INTERNAL_API_KEY"
ENV_PILOT_IDS = "NETLINK_V2_PILOT_TELEGRAM_IDS"


@dataclass(frozen=True)
class V2Settings:
    enabled_flag: bool
    base_url: str
    internal_api_key: str
    pilot_telegram_ids: frozenset[int]


def load_v2_settings() -> V2Settings:
    raw_ids = os.environ.get(ENV_PILOT_IDS, "")
    pilot_ids = frozenset(
        int(part.strip())
        for part in raw_ids.split(",")
        if part.strip().isdigit()
    )
    return V2Settings(
        enabled_flag=os.environ.get(ENV_ENABLED, "").strip().lower() == "true",
        base_url=os.environ.get(ENV_URL, "").strip(),
        internal_api_key=os.environ.get(ENV_API_KEY, "").strip(),
        pilot_telegram_ids=pilot_ids,
    )


def is_configured() -> bool:
    settings_ = load_v2_settings()
    return (
        settings_.enabled_flag
        and bool(settings_.base_url)
        and bool(settings_.internal_api_key)
    )


def is_enabled() -> bool:
    """True only when the flag is 'true' AND url AND key are configured."""
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
    if not settings_.base_url:
        problems.append(f"{ENV_URL} is empty")
    if not settings_.internal_api_key:
        problems.append(f"{ENV_API_KEY} is empty")
    if settings_.enabled_flag and not settings_.pilot_telegram_ids:
        problems.append(f"{ENV_PILOT_IDS} is empty: nobody has v2 access")
    return problems
