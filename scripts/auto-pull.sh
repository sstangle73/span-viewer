#!/usr/bin/env bash
# Polled by span-viewer-deploy.timer. Pulls main and restarts the app
# service only when HEAD actually moved — quiet no-op otherwise.
#
# Ingest is a oneshot-timer job, so it picks up new code on
# their next fire without a restart. Systemd unit changes still need a
# manual `bash scripts/install-deploy-timer.sh` to be daemon-reloaded.

set -euo pipefail

cd /opt/span-viewer

git fetch --quiet origin main
before=$(git rev-parse HEAD)
after=$(git rev-parse origin/main)

if [[ "$before" == "$after" ]]; then
  exit 0
fi

# Only fast-forward when origin/main is genuinely ahead of us. If HEAD has
# diverged from origin -- a commit made directly on the box that was never
# pushed, or a rebased remote -- then `git merge --ff-only` is a silent
# no-op. Restarting the app on that no-op is exactly what produced an
# every-minute restart loop after a fix got committed on the box (2026-05).
# Bail loudly instead of churning the service.
if ! git merge-base --is-ancestor "$before" "$after"; then
  echo "span-viewer: local HEAD ${before:0:7} is not an ancestor of origin/main ${after:0:7} (diverged/ahead) -> skipping, no restart" >&2
  exit 0
fi

git merge --ff-only --quiet origin/main

# Belt-and-suspenders: only proceed to restart if HEAD actually moved.
after=$(git rev-parse HEAD)
if [[ "$before" == "$after" ]]; then
  exit 0
fi
echo "span-viewer: HEAD ${before:0:7} -> ${after:0:7}"

# Pip-install if any requirements.txt changed in the pull. Cheap to run
# unconditionally on the venv, but skip when nothing moved so the deploy
# log stays quiet.
if git diff --name-only "$before" "$after" | grep -qE '(^|/)requirements\.txt$'; then
  echo "span-viewer: requirements.txt changed -> pip install"
  /opt/span-viewer/.venv/bin/pip install -q -r app/requirements.txt
  /opt/span-viewer/.venv/bin/pip install -q -r ingester/requirements.txt
fi

echo "span-viewer: restarting app"
systemctl restart span-viewer-app.service
