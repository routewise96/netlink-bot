"""HTTP server that serves sing-box subscription JSON per device.

GET /profiles/<sub_id>.json  →  rendered sing-box config (lookup by sub_id in netlink.db)
GET /healthz                 →  200 OK plain text
All other paths              →  404 (aiohttp default)
"""
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from aiohttp import web

from bot.config import NETLINK_DB_PATH, SERVER_IP
from bot.services.proxy import get_stream_settings

PROFILE_PORT = 8080
TEMPLATE_PATH = Path(__file__).with_name("singbox_template.json")

log = logging.getLogger("netlink.profile")


def _load_template() -> str:
    """Read template fresh each request so edits land without restart."""
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _get_device(sub_id: str) -> dict | None:
    """Look up uuid/status by sub_id from netlink.db (read-only)."""
    conn = sqlite3.connect(f"file:{NETLINK_DB_PATH}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT uuid, status FROM user_devices WHERE sub_id = ?",
            (sub_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"uuid": row[0], "status": row[1]}


def _render(uuid: str, stream: dict) -> str:
    reality = stream.get("realitySettings", {}) or {}
    settings = reality.get("settings", {}) or {}
    public_key = settings.get("publicKey", "")
    short_ids = reality.get("shortIds") or [""]
    server_names = reality.get("serverNames") or ["microsoft.com"]
    sni = server_names[0]
    fp = settings.get("fingerprint", "chrome")

    template = _load_template()
    return (
        template
        .replace("{{SERVER_IP}}", SERVER_IP)
        .replace("{{UUID}}", uuid)
        .replace("{{SNI}}", sni)
        .replace("{{FP}}", fp)
        .replace("{{PUBLIC_KEY}}", public_key)
        .replace("{{SHORT_ID}}", short_ids[0])
    )


async def profile_handler(request: web.Request) -> web.Response:
    sub_id = request.match_info.get("sub_id", "")
    device = await asyncio.to_thread(_get_device, sub_id)
    if not device or device["status"] != "active":
        log.info("profile miss sub_id=%s reason=%s",
                 sub_id, "not_found" if not device else f"status={device['status']}")
        return web.Response(status=404, text="Not Found\n")

    stream = await asyncio.to_thread(get_stream_settings)
    try:
        rendered = await asyncio.to_thread(_render, device["uuid"], stream)
        json.loads(rendered)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        log.error("profile render failed sub_id=%s: %s", sub_id, e)
        return web.Response(status=500, text="Server Error\n")

    log.info("profile served sub_id=%s", sub_id)
    return web.Response(
        body=rendered,
        content_type="application/json",
        headers={"profile-update-interval": "24"},
    )


async def healthz(_: web.Request) -> web.Response:
    return web.Response(text="OK\n")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = web.Application()
    app.router.add_get("/profiles/{sub_id}.json", profile_handler)
    app.router.add_get("/healthz", healthz)
    log.info("NetLink profile server listening on 0.0.0.0:%d", PROFILE_PORT)
    web.run_app(app, host="0.0.0.0", port=PROFILE_PORT, access_log=log)


if __name__ == "__main__":
    main()
