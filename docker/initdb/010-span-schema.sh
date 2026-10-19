#!/bin/sh
# Applies db/schema/*.sql (mounted at /span-schema) once, when the database
# volume is first created. The timescaledb image's own 000_ script has already
# created the extension in $POSTGRES_DB.
set -e
for f in /span-schema/*.sql; do
    echo "== applying $(basename "$f") =="
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$f"
done
