"""Read x-ui database and generate VLESS links."""
import json
import sqlite3
import urllib.parse
from bot.config import XUI_DB_PATH, SERVER_IP, RESERVED_EMAILS


def _get_xui_data() -> tuple[list[dict], dict]:
    """Read clients and stream settings from x-ui DB (sync, read-only)."""
    conn = sqlite3.connect(f"file:{XUI_DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT settings, stream_settings FROM inbounds WHERE id=1"
        ).fetchone()
        if not row:
            return [], {}
        settings = json.loads(row[0])
        stream = json.loads(row[1])
        return settings.get("clients", []), stream
    finally:
        conn.close()


def get_all_clients() -> list[dict]:
    clients, _ = _get_xui_data()
    return clients


def get_stream_settings() -> dict:
    _, stream = _get_xui_data()
    return stream


def _is_pool_email(email: str) -> bool:
    """Only "user-NNN" emails (where NNN is digits) and not in RESERVED_EMAILS
    are part of the bot pool. Family/personal clients (guest-temp, etc.) and
    reserved family-shared emails (RESERVED_EMAILS) are never handed out."""
    if email in RESERVED_EMAILS:
        return False
    if not email.startswith("user-"):
        return False
    suffix = email[5:]
    return suffix.isdigit()


def get_free_uuids(used_emails: set[str]) -> list[dict]:
    """Return clients not yet assigned to any bot user.
    Only pool clients (user-NNN) are considered; family UUIDs are excluded."""
    clients = get_all_clients()
    return [
        c for c in clients
        if _is_pool_email(c["email"])
        and c["email"] not in used_emails
        and c.get("enable", True)
    ]


def check_reserved_emails() -> tuple[list[str], list[str]]:
    """Verify each RESERVED_EMAILS entry exists and is enabled in x-ui pool.

    Catches typos in RESERVED_EMAILS or accidental delete/disable in the x-ui panel.
    Returns (ok, problems) where each problem is a short human-readable reason.
    """
    by_email = {c["email"]: c for c in get_all_clients()}
    ok, problems = [], []
    for email in sorted(RESERVED_EMAILS):
        cl = by_email.get(email)
        if cl is None:
            problems.append(f"{email}: not found in x-ui")
        elif not cl.get("enable", True):
            problems.append(f"{email}: disabled in x-ui")
        else:
            ok.append(email)
    return ok, problems


def get_client_by_email(email: str) -> dict | None:
    clients = get_all_clients()
    for c in clients:
        if c["email"] == email:
            return c
    return None


def generate_vless_link(uuid: str, label: str = "NetLink") -> str:
    stream = get_stream_settings()
    reality = stream.get("realitySettings", {})
    settings = reality.get("settings", {})
    public_key = settings.get("publicKey", "")
    short_id = reality.get("shortIds", [""])[0]
    sni = reality.get("serverNames", ["microsoft.com"])[0]
    fp = settings.get("fingerprint", "chrome")

    params = urllib.parse.urlencode({
        "encryption": "none",
        "flow": "xtls-rprx-vision",
        "security": "reality",
        "sni": sni,
        "fp": fp,
        "pbk": public_key,
        "sid": short_id,
        "type": "tcp",
    })
    fragment = urllib.parse.quote(label)
    return f"vless://{uuid}@{SERVER_IP}:443?{params}#{fragment}"


def update_client_limit_ip(email: str, limit_ip: int) -> None:
    """Update limitIp for a client in x-ui DB. This is the ONLY write operation."""
    conn = sqlite3.connect(XUI_DB_PATH)
    try:
        row = conn.execute(
            "SELECT id, settings FROM inbounds WHERE id=1"
        ).fetchone()
        if not row:
            return
        settings = json.loads(row[1])
        for client in settings.get("clients", []):
            if client["email"] == email:
                client["limitIp"] = limit_ip
                break
        conn.execute(
            "UPDATE inbounds SET settings = ? WHERE id = ?",
            (json.dumps(settings), row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def update_clients_limit_ip(emails: list[str], limit_ip: int) -> None:
    """Batch update limitIp for multiple clients in x-ui DB (single write).

    TODO: limitIp игнорируется с flow=xtls-rprx-vision (3x-ui issue #3255).
    Текущий механизм "1 устройство = 1 ссылка" фактически не работает.
    Нужно либо менять flow, либо реализовать enforcement на уровне бота
    (например, периодически проверять количество активных IP через access.log).
    """
    conn = sqlite3.connect(XUI_DB_PATH)
    try:
        row = conn.execute(
            "SELECT id, settings FROM inbounds WHERE id=1"
        ).fetchone()
        if not row:
            return
        email_set = set(emails)
        settings = json.loads(row[1])
        for client in settings.get("clients", []):
            if client["email"] in email_set:
                client["limitIp"] = limit_ip
        conn.execute(
            "UPDATE inbounds SET settings = ? WHERE id = ?",
            (json.dumps(settings), row[0]),
        )
        conn.commit()
    finally:
        conn.close()


def set_client_enabled(email: str, enabled: bool) -> None:
    """Set enable flag for one client in x-ui DB (kills sharing in flight)."""
    conn = sqlite3.connect(XUI_DB_PATH)
    try:
        row = conn.execute(
            "SELECT id, settings FROM inbounds WHERE id=1"
        ).fetchone()
        if not row:
            return
        settings = json.loads(row[1])
        for client in settings.get("clients", []):
            if client["email"] == email:
                client["enable"] = enabled
                break
        conn.execute(
            "UPDATE inbounds SET settings = ? WHERE id = ?",
            (json.dumps(settings), row[0]),
        )
        conn.commit()
    finally:
        conn.close()
