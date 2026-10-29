#!/usr/bin/env python3
"""Consistent local backup of the NetLink SQLite database (netlink.db).

- Uses sqlite3 Online Backup API (connection.backup) -> consistent even while
  the bot writes; NOT a plain cp of a live database.
- Verifies the copy opens and passes PRAGMA integrity_check.
- Keeps NETLINK_BACKUP_KEEP newest backups (default 14).
- Backup directory outside the git repo, root-only (0700/0600).

Scope: LOCAL mistake protection only. This is NOT disaster recovery for a lost
VPS (off-site backup is a NetLink 2.0 task).

Prints only safe diagnostics (paths, sizes, counts). Never dumps DB content.
"""
import os
import sqlite3
import sys
import time
from pathlib import Path

SRC = os.environ.get("NETLINK_DB_PATH", "/opt/netlink-bot/netlink.db")
BACKUP_DIR = Path(os.environ.get("NETLINK_BACKUP_DIR", "/var/backups/netlink"))
KEEP = int(os.environ.get("NETLINK_BACKUP_KEEP", "14"))
PREFIX = "netlink-"
SUFFIX = ".db"


def log(msg: str) -> None:
    print(f"[db-backup] {msg}")


def die(msg: str) -> int:
    print(f"[db-backup] ERROR: {msg}", file=sys.stderr)
    return 1


def make_consistent_copy(src: str, dst: Path) -> None:
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def verify(path: Path) -> str:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return row[0] if row else "no-result"
    finally:
        conn.close()


def prune(directory: Path, keep: int) -> int:
    backups = sorted(
        p for p in directory.iterdir()
        if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)
    )
    removed = 0
    for stale in backups[:-keep] if keep > 0 else backups:
        stale.unlink()
        removed += 1
    return removed


def main() -> int:
    if not os.path.exists(SRC):
        return die(f"source database not found: {SRC}")
    if KEEP < 1:
        return die("NETLINK_BACKUP_KEEP must be >= 1")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BACKUP_DIR, 0o700)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / f"{PREFIX}{stamp}{SUFFIX}"
    tmp = dst.with_suffix(".tmp")

    try:
        make_consistent_copy(SRC, tmp)
        os.chmod(tmp, 0o600)
        result = verify(tmp)
        if result != "ok":
            tmp.unlink(missing_ok=True)
            return die(f"integrity_check failed on copy: {result}")
        os.replace(tmp, dst)
    except sqlite3.Error as exc:
        tmp.unlink(missing_ok=True)
        return die(f"sqlite error: {exc}")

    size = dst.stat().st_size
    removed = prune(BACKUP_DIR, KEEP)
    remaining = len(
        [p for p in BACKUP_DIR.iterdir()
         if p.name.startswith(PREFIX) and p.name.endswith(SUFFIX)]
    )
    log(f"backup ok : {dst} ({size} bytes), integrity_check=ok")
    log(f"retention : removed {removed}, kept {remaining} (limit {KEEP})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
