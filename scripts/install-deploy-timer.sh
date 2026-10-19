#!/usr/bin/env bash
# Install (or refresh) the span-viewer-deploy systemd timer that polls
# origin/main every minute and restarts the app on new commits.
# Idempotent: safe to re-run after editing the .service / .timer files.
#
# Usage (from inside the LXC, after the repo is at /opt/span-viewer):
#   bash scripts/install-deploy-timer.sh

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "must run as root" >&2
    exit 1
fi

SCRIPT_DIR=$(dirname "$(readlink -f "$0")")
SRC_DIR="$SCRIPT_DIR/../systemd"

chmod +x "$SCRIPT_DIR/auto-pull.sh"

cp "$SRC_DIR/span-viewer-deploy.service" /etc/systemd/system/
cp "$SRC_DIR/span-viewer-deploy.timer"   /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now span-viewer-deploy.timer

echo
echo "== timer status =="
systemctl status span-viewer-deploy.timer --no-pager --lines=0 || true
echo
echo "== next 5 fires =="
systemctl list-timers span-viewer-deploy.timer --no-pager
