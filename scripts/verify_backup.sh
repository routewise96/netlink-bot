#!/bin/bash
# Verify that a fresh, valid SQLite backup exists in a directory.
# Used by the stabilize workflow right after running netlink-db-backup.service.
#
# Proves:
#   1. at least one netlink-*.db backup file exists in DIR
#   2. the newest one is non-empty
#   3. the newest one is FRESH (mtime within VERIFY_MAX_AGE_SEC, default 10 min)
#   4. the newest one opens as SQLite and PRAGMA integrity_check == ok
#
# Exit codes: 0 = proven, 1 = any check failed.
set -euo pipefail

DIR="${1:?usage: verify_backup.sh <backup-dir>}"
MAX_AGE_SEC="${VERIFY_MAX_AGE_SEC:-600}"

fail() { echo "[verify-backup] ERROR: $*" >&2; exit 1; }

NEWEST="$(ls -1t "$DIR"/netlink-*.db 2>/dev/null | head -n1 || true)"
[ -n "$NEWEST" ] || fail "no netlink-*.db backups found in $DIR"
echo "[verify-backup] newest: $NEWEST"

SIZE="$(stat -c%s "$NEWEST")"
[ "$SIZE" -gt 0 ] || fail "backup is empty (0 bytes)"

AGE_SEC="$(( $(date +%s) - $(stat -c%Y "$NEWEST") ))"
[ "$AGE_SEC" -le "$MAX_AGE_SEC" ] || fail "backup is stale: ${AGE_SEC}s old (max ${MAX_AGE_SEC}s)"

RESULT="$(python3 - "$NEWEST" <<'PYEOF'
import sqlite3, sys
path = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        print(row[0] if row else "no-result")
    finally:
        conn.close()
except sqlite3.Error as exc:
    print(f"sqlite-error: {exc}")
PYEOF
)"
[ "$RESULT" = "ok" ] || fail "integrity_check failed: $RESULT"

echo "[verify-backup] OK: fresh (${AGE_SEC}s), ${SIZE} bytes, integrity_check=ok"
