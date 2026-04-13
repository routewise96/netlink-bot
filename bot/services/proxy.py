"""Read x-ui database and generate VLESS links."""
import json
import sqlite3
import urllib.parse
from bot.config import XUI_DB_PATH, SERVER_IP


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


def get_free_uuids(used_emails: set[str]) -> list[dict]:
    """Return clients not yet assigned to any bot user."""
    clients = get_all_clients()
    return [c for c in clients if c["email"] not in used_emails and c.get("enable", True)]


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
    """Batch update limitIp for multiple clients in x-ui DB (single write)."""
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
