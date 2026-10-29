#!/bin/bash
# Idempotent journald limits for Ubuntu 24.04 (systemd drop-in).
#   SystemMaxUse=256M  RuntimeMaxUse=64M  MaxRetentionSec=14day
#
# Dry-run by default. --apply also:
#   - restarts systemd-journald (safe by design, required to re-read config)
#   - vacuums existing archives down to the new limit
set -euo pipefail

DROPIN_DIR="${NETLINK_JOURNALD_DIR:-/etc/systemd/journald.conf.d}"
DROPIN="$DROPIN_DIR/90-netlink-hardening.conf"
APPLY=0

log() { echo "[journald-limits] $*"; }
die() { echo "[journald-limits] ERROR: $*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

mode="DRY-RUN (no changes)"; [ "$APPLY" -eq 1 ] && mode="APPLY"
log "mode   : $mode"
log "target : $DROPIN"

WANT='[Journal]
SystemMaxUse=256M
RuntimeMaxUse=64M
MaxRetentionSec=14day'

current_usage() { journalctl --disk-usage 2>/dev/null | head -1 || echo "n/a"; }
log "now    : $(current_usage)"

if [ -f "$DROPIN" ] && [ "$(cat "$DROPIN")" = "$WANT" ]; then
  log "RESULT : no-op, drop-in already present with exact content"
else
  log "plan   : write drop-in (SystemMaxUse=256M, RuntimeMaxUse=64M, MaxRetentionSec=14day)"
  if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$DROPIN_DIR"
    if [ -f "$DROPIN" ]; then
      cp "$DROPIN" "$DROPIN.bak-$(date +%Y%m%d-%H%M%S)"
    fi
    printf '%s\n' "$WANT" > "$DROPIN"
    chmod 0644 "$DROPIN"
    systemctl restart systemd-journald
    journalctl --vacuum-size=256M >/dev/null
    log "applied: drop-in + journald restart + vacuum"
  fi
fi

[ "$APPLY" -eq 1 ] && log "after  : $(current_usage)"
log "done"
