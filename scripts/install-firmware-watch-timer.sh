#!/usr/bin/env bash
# Install (or refresh) the span-viewer-firmware-watch systemd timer.
# Idempotent: safe to re-run after editing the .service / .timer files.
#
# Usage (from inside the LXC, after the repo is at /opt/span-viewer):
#   bash scripts/install-firmware-watch-timer.sh

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
SRC_DIR="$SCRIPT_DIR/../systemd"

cp "$SRC_DIR/span-viewer-firmware-watch.service" /etc/systemd/system/
cp "$SRC_DIR/span-viewer-firmware-watch.timer"   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now span-viewer-firmware-watch.timer

# Prime the state file so the first real fire has a baseline to compare
# against — otherwise the next firmware bump is swallowed as "first run".
systemctl start span-viewer-firmware-watch.service

echo
echo "== last run =="
journalctl -u span-viewer-firmware-watch.service -n 10 --no-pager || true
echo
echo "== next 5 fires =="
systemctl list-timers span-viewer-firmware-watch.timer --no-pager
