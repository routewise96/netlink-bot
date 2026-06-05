"""Detect subscription sharing by checking xray access.log for multiple
source IPs per email within a 1-minute window.

Runs as a oneshot every minute via systemd timer. Each run:
  1. List admin emails (Daniel's devices) — fully ignored.
  2. Parse the last ~minute of /var/log/xray-access.log.
  3. For each non-admin email with >1 distinct source IP: UPSERT a violations row.
  4. If an email accumulates >=3 distinct minute_buckets in the last hour AND
     no alerted=1 rows exist in that window — POST a Telegram alert and mark
     all of that email's last-hour rows alerted=1.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

NETLINK_DB_PATH = os.getenv("NETLINK_DB_PATH", "/opt/netlink-bot/netlink.db")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
ACCESS_LOG = "/var/log/xray-access.log"
TAIL_LINES = 20000
ADMIN_TG_ID = 738922628

# Examples:
#   2026/06/05 11:13:02.617132 from 128.71.241.130:47306 accepted tcp:8.8.8.8:443 [inbound-8443 >> direct] email: user-068
#   2026/06/05 13:16:10.364679 from tcp:212.100.129.82:60689 accepted udp:8.8.8.8:53 [inbound-8443 >> direct] email: user-035
LOG_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\.\d+ from "
    r"(?:\w+:)?(\d{1,3}(?:\.\d{1,3}){3}):\d+ accepted .+ email: (\S+)"
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(NETLINK_DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _admin_emails() -> set[str]:
    with _conn() as c:
        rows = c.execute(
            "SELECT email FROM user_devices "
            "WHERE user_id IN (SELECT id FROM users WHERE telegram_id=?)",
            (ADMIN_TG_ID,),
        ).fetchall()
    return {r["email"] for r in rows}


def _tail_lines() -> list[str]:
    try:
        r = subprocess.run(
            ["tail", "-n", str(TAIL_LINES), ACCESS_LOG],
            capture_output=True, text=True, check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        print(f"tail failed: {e}", file=sys.stderr)
        return []
    return r.stdout.splitlines()


def _parse_window(lines: list[str], since: datetime) -> dict[str, set[str]]:
    """Return {email: {ips}} for log entries with timestamp >= since."""
    by_email: dict[str, set[str]] = {}
    for line in lines:
        m = LOG_RE.match(line)
        if not m:
            continue
        ts_str, ip, email = m.group(1), m.group(2), m.group(3)
        try:
            ts = datetime.strptime(ts_str, "%Y/%m/%d %H:%M:%S")
        except ValueError:
            continue
        if ts < since:
            continue
        by_email.setdefault(email, set()).add(ip)
    return by_email


def _record_violations(by_email: dict[str, set[str]], admin_emails: set[str],
                       minute_bucket: str) -> list[str]:
    """Upsert violations for emails with >1 IP. Return list of violating emails."""
    violators = []
    with _conn() as c:
        for email, ips in by_email.items():
            if email in admin_emails or len(ips) <= 1:
                continue
            ips_json = json.dumps(sorted(ips))
            c.execute(
                """INSERT INTO violations (email, ips_json, minute_bucket)
                   VALUES (?, ?, ?)
                   ON CONFLICT(email, minute_bucket) DO UPDATE
                       SET ips_json = excluded.ips_json""",
                (email, ips_json, minute_bucket),
            )
            violators.append(email)
        c.commit()
    return violators


def _alert_targets(emails: list[str]) -> list[dict]:
    """For each email, check if we should alert: >=3 unignored windows in last hour,
    AND no alerted=1 rows in last hour. Return user/device info needed for the alert."""
    if not emails:
        return []
    targets = []
    with _conn() as c:
        for email in emails:
            windows = c.execute(
                """SELECT COUNT(DISTINCT minute_bucket) FROM violations
                   WHERE email=? AND created_at >= datetime('now','-1 hour')
                         AND ignored=0""",
                (email,),
            ).fetchone()[0]
            if windows < 3:
                continue
            already = c.execute(
                """SELECT COUNT(*) FROM violations
                   WHERE email=? AND created_at >= datetime('now','-1 hour')
                         AND alerted=1""",
                (email,),
            ).fetchone()[0]
            if already > 0:
                continue
            row = c.execute(
                """SELECT ud.id AS device_id, ud.uuid, ud.device_number,
                          u.fio, u.username, u.telegram_id
                   FROM user_devices ud
                   JOIN users u ON ud.user_id = u.id
                   WHERE ud.email = ? LIMIT 1""",
                (email,),
            ).fetchone()
            if not row:
                continue
            ips_union = c.execute(
                """SELECT ips_json FROM violations
                   WHERE email=? AND created_at >= datetime('now','-1 hour')
                         AND ignored=0""",
                (email,),
            ).fetchall()
            ip_set = set()
            for r in ips_union:
                ip_set.update(json.loads(r["ips_json"]))
            targets.append({
                "email": email,
                "device_id": row["device_id"],
                "uuid": row["uuid"],
                "device_number": row["device_number"],
                "fio": row["fio"] or "?",
                "username": row["username"] or "",
                "telegram_id": row["telegram_id"],
                "windows": windows,
                "ips": sorted(ip_set),
            })
    return targets


def _mark_alerted(emails: list[str]) -> None:
    with _conn() as c:
        for email in emails:
            c.execute(
                """UPDATE violations SET alerted=1
                   WHERE email=? AND created_at >= datetime('now','-1 hour')""",
                (email,),
            )
        c.commit()


def _send_alert(t: dict) -> bool:
    username = f"@{t['username']}" if t["username"] else "—"
    ip_list = ", ".join(t["ips"])
    text = (
        "⚠️ <b>Подозрение на шаринг подписки</b>\n\n"
        f"User: {t['fio']} ({username}, tg_id {t['telegram_id']})\n"
        f"Email: <code>{t['email']}</code>\n"
        f"UUID: <code>{t['uuid']}</code>\n"
        f"Уникальных IP за последний час: <b>{len(t['ips'])}</b>\n"
        f"Список IP: {ip_list}\n"
        f"Окон с нарушениями: <b>{t['windows']}</b>"
    )
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": json.dumps({
            "inline_keyboard": [
                [{"text": "🚫 Заблокировать UUID",
                  "callback_data": f"block_uuid:{t['device_id']}"}],
                [{"text": "✅ Игнорировать",
                  "callback_data": f"ignore_violation:{t['device_id']}"}],
            ]
        }),
    }
    data = urllib.parse.urlencode(payload).encode()
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        with urllib.request.urlopen(url, data=data, timeout=15) as resp:
            ok = resp.status == 200
    except Exception as e:
        print(f"alert send failed email={t['email']} err={e}", file=sys.stderr)
        return False
    return ok


def main() -> None:
    if not BOT_TOKEN or not ADMIN_CHAT_ID:
        print("BOT_TOKEN or ADMIN_CHAT_ID missing in .env", file=sys.stderr)
        sys.exit(1)

    now = datetime.now()
    since = now - timedelta(seconds=60)
    minute_bucket = since.strftime("%Y-%m-%d %H:%M")

    admin = _admin_emails()
    lines = _tail_lines()
    by_email = _parse_window(lines, since)
    violators = _record_violations(by_email, admin, minute_bucket)

    print(f"[{now.isoformat()}] window>={since.isoformat()} "
          f"lines_parsed={len(lines)} emails_seen={len(by_email)} "
          f"admin_skipped={len(admin & by_email.keys())} "
          f"violators_this_minute={len(violators)}")

    if not violators:
        return

    targets = _alert_targets(violators)
    if not targets:
        return

    sent_emails = []
    for t in targets:
        if _send_alert(t):
            sent_emails.append(t["email"])
            print(f"alerted email={t['email']} windows={t['windows']} "
                  f"ips={len(t['ips'])}")
    if sent_emails:
        _mark_alerted(sent_emails)


if __name__ == "__main__":
    main()
