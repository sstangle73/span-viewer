#!/usr/bin/env bash
# Apply / re-apply the db/schema/*.sql files against the span database.
# Each file is idempotent (uses IF NOT EXISTS); safe to re-run.
#
# Usage (inside the LXC, as root: it runs psql as the local postgres user):
#   ./scripts/apply-schema.sh
#
# Or from the workstation:
#   scp -r db scripts/apply-schema.sh root@<span-viewer-host>:/tmp/
#   ssh root@<span-viewer-host> 'cd /tmp && bash apply-schema.sh'

set -euo pipefail

SCHEMA_DIR="${SCHEMA_DIR:-$(dirname "$0")/../db/schema}"

if ! command -v psql >/dev/null 2>&1; then
    echo "psql not on PATH; install postgresql-client-17 first" >&2
    exit 1
fi

for f in "$SCHEMA_DIR"/*.sql; do
    [[ -e "$f" ]] || { echo "no .sql files in $SCHEMA_DIR" >&2; exit 1; }
    echo "== applying $(basename "$f") =="
    runuser -u postgres -- psql -d span -v ON_ERROR_STOP=1 -f "$f"
done

echo
echo "== final state =="
runuser -u postgres -- psql -d span -v ON_ERROR_STOP=1 <<'SQL'
\dt+
\dx
\d+ panel_readings
\d+ space_readings
SQL
