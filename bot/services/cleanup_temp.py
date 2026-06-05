"""Expire temporary registration links: disable in x-ui + delete row.

Runs as a oneshot every minute via systemd timer. Each run finds active
is_temp=1 rows whose expires_at is past, disables the matching x-ui client
to kick any active session, and removes the row from user_devices.
"""
import os
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

NETLINK_DB_PATH = os.getenv("NETLINK_DB_PATH", "/opt/netlink-bot/netlink.db")


def main() -> None:
    # Late import so that .env is in place before XUI_DB_PATH is read.
    from bot.services.proxy import set_client_enabled

    conn = sqlite3.connect(NETLINK_DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, email FROM user_devices
               WHERE is_temp = 1 AND status = 'active'
                 AND expires_at IS NOT NULL
                 AND datetime(expires_at) < datetime('now')"""
        ).fetchall()
        for r in rows:
            email = r["email"]
            # 1) close active VPN sessions of the temp client
            try:
                set_client_enabled(email, False)
            except Exception as e:
                print(f"x-ui disable failed email={email} err={e}", file=sys.stderr)
            # 2) free the row in bot DB (sub_id no longer resolves)
            conn.execute("DELETE FROM user_devices WHERE id=?", (r["id"],))
            # 3) re-enable in x-ui so the UUID returns to the pool
            try:
                set_client_enabled(email, True)
            except Exception as e:
                print(f"x-ui re-enable failed email={email} err={e}", file=sys.stderr)
            print(f"expired email={email}")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
