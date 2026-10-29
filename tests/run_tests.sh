#!/bin/bash
# Minimal test runner (stdlib only, no external dependencies).
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== syntax checks =="
bash -n scripts/setup_swap.sh
bash -n scripts/install_journald_limits.sh
python3 -m py_compile scripts/set_xray_loglevel.py scripts/netlink_db_backup.py
echo "== unit tests =="
python3 -m unittest discover -s tests -v
