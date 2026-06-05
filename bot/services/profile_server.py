"""HTTP server that serves sing-box subscription JSON per device and a
self-service recovery page for users who can't reach the Telegram bot.

GET  /profiles/<sub_id>.json   → rendered sing-box config (lookup by sub_id)
GET  /healthz                  → 200 OK plain text
GET  /recover                  → recovery form (HTML)
POST /recover                  → validate FIO → issue token → redirect
GET  /recover_show/<token>     → one-shot subscription page
All other paths                → 404
"""
import asyncio
import html
import json
import logging
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web

from bot.config import NETLINK_DB_PATH, SERVER_IP
from bot.services.proxy import get_stream_settings

PROFILE_PORT = 8080
TEMPLATE_PATH = Path(__file__).with_name("singbox_template.json")
TOKEN_TTL_SECONDS = 3600
RATE_LIMIT_WINDOW = 3600
RATE_LIMIT_MAX = 5

log = logging.getLogger("netlink.profile")

# in-memory rate limit: ip → list[float epoch of successful issue]
_issue_history: dict[str, list[float]] = {}


# ───── profile (sing-box JSON) ─────

def _load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _get_device(sub_id: str) -> dict | None:
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


# ───── recover (self-service via web) ─────

_CSS = """
  *{box-sizing:border-box}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
       background:#f4f5f7;margin:0;padding:24px;color:#1a1a1a;line-height:1.4}
  .card{max-width:480px;margin:32px auto;background:#fff;border-radius:12px;
        padding:24px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
  h1{font-size:20px;margin:0 0 16px}
  h2{font-size:16px;margin:24px 0 8px}
  label{display:block;font-size:14px;color:#444;margin:8px 0 6px}
  input[type=text]{width:100%;font-size:16px;padding:12px;border:1px solid #d0d2d6;
                   border-radius:8px;background:#fff}
  input[type=text]:focus{outline:none;border-color:#2d7ae5}
  button{width:100%;margin-top:12px;font-size:16px;padding:12px;background:#2d7ae5;
         color:#fff;border:0;border-radius:8px;cursor:pointer;font-weight:500}
  button:active{background:#1f5dbb}
  .hint{font-size:13px;color:#666;margin:16px 0;line-height:1.5}
  .error{background:#fde8e8;color:#9b1c1c;padding:12px;border-radius:8px;
         font-size:14px;margin:16px 0}
  .device{margin:16px 0;padding:12px;background:#f7f9fc;border-radius:8px;
          border:1px solid #e6eaf0}
  .label{font-weight:600;margin-bottom:8px;font-size:14px}
  code{font-family:ui-monospace,"SF Mono",Monaco,Menlo,monospace;background:#fff;
       border:1px solid #e0e3e8;padding:10px;border-radius:6px;display:block;
       word-break:break-all;font-size:13px;color:#222}
  .copy-btn{width:auto;margin:10px 0 0;padding:8px 14px;font-size:14px}
  ol{padding-left:20px;margin:8px 0}
  ol li{margin:6px 0}
  a{color:#2d7ae5;text-decoration:none}
"""


def _page(title: str, body: str, extra_script: str = "") -> str:
    return (
        '<!DOCTYPE html>\n'
        '<html lang="ru">\n'
        '<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{html.escape(title)}</title>\n'
        f'<style>{_CSS}</style>\n'
        '</head>\n'
        f'<body>\n<div class="card">\n{body}\n</div>\n{extra_script}\n</body>\n</html>\n'
    )


def _form_body(error_msg: str | None = None) -> str:
    err = f'<div class="error">{html.escape(error_msg)}</div>' if error_msg else ""
    return (
        '<h1>NetLink — восстановление подписки</h1>\n'
        f'{err}\n'
        '<form method="POST" action="/recover" autocomplete="off">\n'
        '  <label for="fio">ФИО (как при регистрации):</label>\n'
        '  <input type="text" id="fio" name="fio" placeholder="Иванов Иван Иванович" '
        'required autofocus inputmode="text">\n'
        '  <button type="submit">Получить ссылку</button>\n'
        '</form>\n'
        '<p class="hint">Ссылка действительна 1 час и открывается один раз. '
        'После открытия сохраните URL и закройте вкладку.</p>\n'
        '<p class="hint">Безопаснее использовать через VPN, если можете.</p>\n'
    )


def _normalize_fio(s: str) -> str:
    return " ".join(s.strip().lower().split())


def _client_ip(request: web.Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote or "unknown"


def _rate_limit_hit(ip: str) -> bool:
    """True ⇒ should block; also prunes old entries for this ip."""
    now = time.monotonic()
    hist = [t for t in _issue_history.get(ip, []) if now - t < RATE_LIMIT_WINDOW]
    _issue_history[ip] = hist
    return len(hist) >= RATE_LIMIT_MAX


def _rate_limit_record(ip: str) -> None:
    _issue_history.setdefault(ip, []).append(time.monotonic())


def _lookup_user(fio_normalized: str) -> dict | None:
    """Fetch approved users and match by normalized fio in Python (fio set ~28)."""
    conn = sqlite3.connect(f"file:{NETLINK_DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, fio FROM users WHERE status='approved' AND fio IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    for uid, fio in rows:
        if _normalize_fio(fio) == fio_normalized:
            return {"id": uid, "fio": fio}
    return None


def _insert_token(user_id: int, ip: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(seconds=TOKEN_TTL_SECONDS)
    conn = sqlite3.connect(NETLINK_DB_PATH, timeout=10.0)
    try:
        conn.execute(
            "INSERT INTO recovery_tokens (token, user_id, created_at, expires_at, ip) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, user_id, now.isoformat(), expires.isoformat(), ip),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def _consume_token(token: str) -> dict | None:
    """Returns {user_id, fio} if token valid+unused+not-expired, else None.
    On success: atomically mark used=1."""
    conn = sqlite3.connect(NETLINK_DB_PATH, timeout=10.0)
    try:
        row = conn.execute(
            """SELECT rt.id, rt.user_id, rt.expires_at, rt.used, u.fio
               FROM recovery_tokens rt
               JOIN users u ON u.id = rt.user_id
               WHERE rt.token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return None
        rt_id, user_id, expires_at, used, fio = row
        if used:
            return {"_denied": "used"}
        try:
            exp = datetime.fromisoformat(expires_at)
        except ValueError:
            return {"_denied": "bad_expires"}
        if datetime.now() > exp:
            return {"_denied": "expired"}
        cur = conn.execute(
            "UPDATE recovery_tokens SET used = 1 WHERE id = ? AND used = 0", (rt_id,)
        )
        conn.commit()
        if cur.rowcount != 1:
            return {"_denied": "race"}
        return {"user_id": user_id, "fio": fio}
    finally:
        conn.close()


def _user_active_devices(user_id: int) -> list[dict]:
    conn = sqlite3.connect(f"file:{NETLINK_DB_PATH}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT device_number, sub_id, platform "
            "FROM user_devices WHERE user_id = ? AND status = 'active' "
            "ORDER BY device_number",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [{"device_number": r[0], "sub_id": r[1], "platform": r[2] or ""} for r in rows]


_PLATFORM_LABEL = {
    "iphone": "iPhone", "android": "Android",
    "windows": "Windows", "macos": "macOS",
}


def _show_body(fio: str, devices: list[dict]) -> str:
    parts = [f'<h1>Привет, {html.escape(fio)}</h1>']
    if not devices:
        parts.append(
            '<p class="hint">У вас нет активных подписок. '
            'Обратитесь к админу @QuentinCostello.</p>'
        )
    else:
        for d in devices:
            plat = _PLATFORM_LABEL.get(d["platform"], "—")
            url = f"http://{SERVER_IP}:8080/profiles/{d['sub_id']}.json"
            parts.append(
                '<div class="device">\n'
                f'  <div class="label">📱 Устройство #{d["device_number"]} ({html.escape(plat)}):</div>\n'
                f'  <code>{html.escape(url)}</code>\n'
                '  <button type="button" class="copy-btn">Скопировать</button>\n'
                '</div>'
            )
        parts.append(
            '<h2>Что делать:</h2>\n'
            '<ol>\n'
            '  <li>Установи Hiddify (App Store / Google Play / hiddify.com).</li>\n'
            '  <li>В Hiddify нажми «+» → «Add Profile from URL» → вставь ссылку выше → Save.</li>\n'
            '  <li>Подключи VPN.</li>\n'
            '</ol>'
        )
    parts.append(
        '<p class="hint">Ссылка сохранена в вашем клиенте навсегда. '
        'Эту страницу можно закрыть. Повторно открыть нельзя.</p>'
    )
    return "\n".join(parts)


_COPY_SCRIPT = """<script>
document.querySelectorAll('.copy-btn').forEach(function(b){
  b.addEventListener('click', function(){
    var url = b.previousElementSibling.textContent;
    navigator.clipboard.writeText(url).then(function(){
      var old = b.textContent;
      b.textContent = 'Скопировано ✓';
      setTimeout(function(){ b.textContent = old; }, 1500);
    });
  });
});
</script>"""


def _invalid_body() -> str:
    return (
        '<h1>Ссылка недействительна</h1>\n'
        '<p class="hint">Ссылка недействительна или уже использована.</p>\n'
        '<p class="hint">Запросите новую: <a href="/recover">/recover</a></p>'
    )


async def recover_form(request: web.Request) -> web.Response:
    log.info("recover form rendered ip=%s", _client_ip(request))
    body = _form_body()
    return web.Response(
        text=_page("NetLink — восстановление подписки", body),
        content_type="text/html",
        charset="utf-8",
    )


async def recover_submit(request: web.Request) -> web.Response:
    ip = _client_ip(request)
    if _rate_limit_hit(ip):
        log.info("recover rate limit hit ip=%s", ip)
        return web.Response(
            status=429,
            text="Слишком много запросов. Попробуйте через час.\n",
            charset="utf-8",
        )

    data = await request.post()
    raw_fio = (data.get("fio") or "").strip()
    norm = _normalize_fio(raw_fio)
    if not norm:
        body = _form_body("Введите ФИО.")
        return web.Response(
            text=_page("NetLink — восстановление подписки", body),
            content_type="text/html", charset="utf-8",
        )

    user = await asyncio.to_thread(_lookup_user, norm)
    if not user:
        log.info("recover failed fio=%s ip=%s", raw_fio, ip)
        body = _form_body(
            "Пользователь не найден или доступ не активен. "
            "Проверьте ФИО или обратитесь к админу."
        )
        return web.Response(
            text=_page("NetLink — восстановление подписки", body),
            content_type="text/html", charset="utf-8",
        )

    token = await asyncio.to_thread(_insert_token, user["id"], ip)
    _rate_limit_record(ip)
    log.info("recover issued user_id=%s token=%s ip=%s", user["id"], token[:8] + "…", ip)
    raise web.HTTPFound(f"/recover_show/{token}")


async def recover_show(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    ip = _client_ip(request)
    result = await asyncio.to_thread(_consume_token, token)
    if not result or "_denied" in (result or {}):
        reason = (result or {}).get("_denied", "not_found")
        log.info("recover_show denied reason=%s token=%s ip=%s",
                 reason, (token[:8] + "…") if token else "", ip)
        return web.Response(
            text=_page("Ссылка недействительна", _invalid_body()),
            content_type="text/html", charset="utf-8", status=410,
        )

    devices = await asyncio.to_thread(_user_active_devices, result["user_id"])
    log.info("recover_show served token=%s user_id=%s devices=%d ip=%s",
             token[:8] + "…", result["user_id"], len(devices), ip)
    body = _show_body(result["fio"], devices)
    return web.Response(
        text=_page(f"Привет, {result['fio']}", body, _COPY_SCRIPT),
        content_type="text/html", charset="utf-8",
    )


# ───── main ─────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = web.Application()
    app.router.add_get("/profiles/{sub_id}.json", profile_handler)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/recover", recover_form)
    app.router.add_post("/recover", recover_submit)
    app.router.add_get("/recover_show/{token}", recover_show)
    log.info("NetLink profile server listening on 0.0.0.0:%d", PROFILE_PORT)
    web.run_app(app, host="0.0.0.0", port=PROFILE_PORT, access_log=log)


if __name__ == "__main__":
    main()
