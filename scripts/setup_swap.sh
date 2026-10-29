#!/bin/bash
# Idempotent swap setup for the NetLink legacy VPS (1.9 GiB RAM, no swap,
# precedent: Xray OOM-kill on 2026-06-26).
#
# Dry-run by default. Apply with --apply.
#
# Idempotency rules:
#   - if total ACTIVE swap >= SWAP_SIZE_MB already -> skip swap creation entirely
#   - if the fstab line for SWAP_FILE already exists -> never duplicate it
#   - swappiness drop-in is content-compared, rewritten only on change
#
# Safety:
#   - preflight checks free disk space; aborts if insufficient
#   - prints only safe diagnostics (no secrets involved)
set -euo pipefail

SWAP_FILE="${SWAP_FILE:-/swapfile}"
SWAP_SIZE_MB="${SWAP_SIZE_MB:-2048}"
SWAPPINESS="${SWAPPINESS:-10}"
FSTAB="${NETLINK_SWAP_FSTAB:-/etc/fstab}"
SYSCTL_DIR="${NETLINK_SYSCTL_DIR:-/etc/sysctl.d}"
SYSCTL_FILE="$SYSCTL_DIR/99-netlink-swap.conf"
# Margin required on the filesystem IN ADDITION to the swap file size:
MIN_FREE_MARGIN_MB="${MIN_FREE_MARGIN_MB:-2048}"
APPLY=0

log() { echo "[swap-setup] $*"; }
die() { echo "[swap-setup] ERROR: $*" >&2; exit 1; }

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --print-fstab-line) echo "$SWAP_FILE none swap sw 0 0"; exit 0 ;;
    *) die "unknown argument: $arg" ;;
  esac
done

mode="DRY-RUN (no changes)"; [ "$APPLY" -eq 1 ] && mode="APPLY"

active_swap_kb() { awk '/^SwapTotal:/ {print $2}' "${MEMINFO_FILE:-/proc/meminfo}"; }

fstab_has_entry() {
  [ -f "$FSTAB" ] && awk -v f="$SWAP_FILE" '$1 == f && $3 == "swap" {found=1} END {exit !found}' "$FSTAB"
}

free_mb() {
  df -Pm "$(dirname "$SWAP_FILE")" | awk 'NR==2 {print $4}'
}

log "mode        : $mode"
log "target      : $SWAP_FILE (${SWAP_SIZE_MB} MB), swappiness=$SWAPPINESS"

ACTIVE_KB=$(active_swap_kb)
ACTIVE_MB=$((ACTIVE_KB / 1024))
log "active swap : ${ACTIVE_MB} MB"

if [ "$ACTIVE_MB" -ge "$SWAP_SIZE_MB" ]; then
  log "RESULT      : no-op, active swap >= target (${ACTIVE_MB} MB >= ${SWAP_SIZE_MB} MB)"
else
  if [ -e "$SWAP_FILE" ] && [ "$ACTIVE_MB" -lt "$SWAP_SIZE_MB" ]; then
    if fstab_has_entry; then
      log "plan        : swapfile exists + fstab entry exists -> swapon only"
    else
      log "plan        : swapfile exists, fstab entry missing -> add fstab + swapon"
    fi
  else
    # Preflight: enough free space?
    FREE_MB=$(free_mb)
    NEED_MB=$((SWAP_SIZE_MB + MIN_FREE_MARGIN_MB))
    log "free space  : ${FREE_MB} MB (need swap ${SWAP_SIZE_MB} MB + margin ${MIN_FREE_MARGIN_MB} MB = ${NEED_MB} MB)"
    [ "$FREE_MB" -lt "$NEED_MB" ] && die "insufficient free space: ${FREE_MB} MB < ${NEED_MB} MB"
    if fstab_has_entry; then
      log "fstab       : entry already present (no duplication)"
      log "plan        : create swapfile -> mkswap -> swapon"
    else
      log "fstab       : entry missing -> will append '$SWAP_FILE none swap sw 0 0'"
      log "plan        : create swapfile -> mkswap -> swapon -> append fstab"
    fi
  fi

  if [ "$APPLY" -eq 1 ]; then
    if [ ! -e "$SWAP_FILE" ]; then
      # Atomic-ish creation: dd into the final path is standard for swapfiles
      # (fallocate can create sparse/unusable files on some filesystems).
      dd if=/dev/zero of="$SWAP_FILE" bs=1M count="$SWAP_SIZE_MB" status=none
      chmod 0600 "$SWAP_FILE"
      mkswap "$SWAP_FILE" >/dev/null
    else
      chmod 0600 "$SWAP_FILE" 2>/dev/null || true
    fi
    if ! swapon "$SWAP_FILE" 2>/dev/null; then
      if swapon --show=NAME --noheadings | grep -qx "$SWAP_FILE"; then
        log "swap        : already active"
      else
        die "swapon $SWAP_FILE failed"
      fi
    fi
    if ! fstab_has_entry; then
      cp "$FSTAB" "${FSTAB}.netlink-bak-$(date +%Y%m%d-%H%M%S)"
      echo "$SWAP_FILE none swap sw 0 0" >> "$FSTAB"
    fi
    log "applied     : swap active + fstab persistent"
  fi
fi

# Swappiness drop-in (content-verified, idempotent)
WANT_CONF="vm.swappiness = $SWAPPINESS"
if [ -f "$SYSCTL_FILE" ] && grep -qx "$WANT_CONF" "$SYSCTL_FILE"; then
  log "swappiness  : already $SWAPPINESS (no-op)"
else
  log "swappiness  : will write $SYSCTL_FILE"
  if [ "$APPLY" -eq 1 ]; then
    mkdir -p "$SYSCTL_DIR"
    printf '%s\n' "$WANT_CONF" > "$SYSCTL_FILE"
    chmod 0644 "$SYSCTL_FILE"
    sysctl -q "vm.swappiness=$SWAPPINESS"
    log "applied     : vm.swappiness=$SWAPPINESS (drop-in + runtime)"
  fi
fi

[ "$APPLY" -eq 1 ] && { log "final state :"; swapon --show; free -h | sed -n '1,3p'; }
log "done"
