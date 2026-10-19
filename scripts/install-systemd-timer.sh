#!/usr/bin/env bash
# Install (or refresh) the span-viewer-ingest systemd timer.
# Idempotent: safe to re-run after editing the .service / .timer files.
#
# Usage (from inside the LXC):
#   bash scripts/install-systemd-timer.sh
#
# Or from the workstation:
#   scp -r systemd scripts/install-systemd-timer.sh root@<span-viewer-host>:/tmp/
#   ssh root@<span-viewer-host> 'cd /tmp && bash install-systemd-timer.sh'

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
SRC_DIR="$SCRIPT_DIR/../systemd"

cp "$SRC_DIR/span-viewer-ingest.service" /etc/systemd/system/
cp "$SRC_DIR/span-viewer-ingest.timer"   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now span-viewer-ingest.timer

echo
echo "== timer status =="
systemctl status span-viewer-ingest.timer --no-pager --lines=0 || true
echo
echo "== next 5 fires =="
systemctl list-timers span-viewer-ingest.timer --no-pager
