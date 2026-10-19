#!/usr/bin/env bash
# Install Postgres 17 + TimescaleDB on the span-viewer LXC.
# Idempotent: safe to re-run. Designed for Debian 13 (trixie).
#
# Usage (from a workstation, against the machine itself rather than any reverse proxy):
#   scp scripts/install-timescaledb.sh root@<span-viewer-host>:/root/
#   ssh root@<span-viewer-host> bash /root/install-timescaledb.sh

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
    echo "Must run as root" >&2
    exit 1
fi

echo "== 1. Add Timescale APT repo (idempotent) =="
if ! [[ -f /usr/share/keyrings/timescaledb.gpg ]]; then
    apt-get install -y -qq curl gnupg lsb-release ca-certificates
    curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
        | gpg --dearmor -o /usr/share/keyrings/timescaledb.gpg
fi
echo "deb [signed-by=/usr/share/keyrings/timescaledb.gpg] https://packagecloud.io/timescale/timescaledb/debian/ $(lsb_release -cs) main" \
    > /etc/apt/sources.list.d/timescaledb.list

apt-get update -qq

echo "== 2. Install Postgres 17 + TimescaleDB =="
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    postgresql-17 postgresql-client-17 \
    timescaledb-2-postgresql-17 timescaledb-tools

PG_CONF=/etc/postgresql/17/main/postgresql.conf
HBA_CONF=/etc/postgresql/17/main/pg_hba.conf

echo "== 3. Run timescaledb-tune =="
# timescaledb-tune reads memory via syscalls that bypass lxcfs, so it sees the
# host's RAM (~94 GiB) instead of the LXC's 4 GiB cap. Pass explicit limits to
# match the LXC's `pct config` (4 vCPU / 4 GB). Re-running tune is safe — it
# overwrites the same managed block in postgresql.conf.
timescaledb-tune --quiet --yes --conf-path "$PG_CONF" --memory 4GB --cpus 4

echo "== 4. listen_addresses: localhost only =="
# The app and every job connect to localhost, and nothing else uses the database (2026-09-30). Don't add the
# LXC's own address back: it comes from DHCP and isn't up yet when Postgres starts at boot, so binding it failed
# at every boot, and this CT's network-online.target doesn't wait for the lease (ifupdown2). A remote client,
# say a Grafana datasource, would want listen_addresses = '*' with pg_hba scoped to that one client, plus a
# read-only role.
if ! grep -qE "^listen_addresses\s*=\s*'localhost'" "$PG_CONF"; then
    sed -i "s|^#\?listen_addresses\s*=.*|listen_addresses = 'localhost'|" "$PG_CONF"
fi

echo "== 5. pg_hba.conf: local connections only =="
# Debian's default allows the Unix socket and loopback only; this script adds nothing.

echo "== 6. Restart Postgres =="
systemctl restart postgresql@17-main

echo "== 7. Create span database + app role + extension =="
# Use peer auth for the local postgres superuser
runuser -u postgres -- psql -v ON_ERROR_STOP=1 <<'SQL'
-- Create app role if missing. Password set separately below.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'span_app') THEN
        CREATE ROLE span_app WITH LOGIN;
    END IF;
END $$;

-- Create span database if missing
SELECT 'CREATE DATABASE span OWNER span_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'span')
\gexec
SQL

# Load timescaledb extension in the span DB (idempotent)
runuser -u postgres -- psql -d span -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS timescaledb;
SQL

echo "== 8. Generate / store span_app password if not present =="
PWFILE=/root/.span-viewer-db-password
if [[ ! -s "$PWFILE" ]]; then
    # 32 chars, alphanumeric (URL-safe in connection strings)
    head -c 24 /dev/urandom | base64 | tr -d '+/=' | head -c 32 > "$PWFILE"
    chmod 600 "$PWFILE"
fi
SPAN_APP_PW=$(cat "$PWFILE")
runuser -u postgres -- psql -v ON_ERROR_STOP=1 \
    -c "ALTER ROLE span_app WITH PASSWORD '$SPAN_APP_PW';"

echo "== 9. Smoke test =="
runuser -u postgres -- psql -d span -v ON_ERROR_STOP=1 <<'SQL'
\dx
SELECT version();
SELECT extversion AS timescaledb_version FROM pg_extension WHERE extname='timescaledb';
SQL

echo
echo "== Done =="
echo "DB:       span"
echo "App role: span_app"
echo "Password: stored at $PWFILE on this host (chmod 600)"
echo "Connect:  runuser -u postgres -- psql -d span   (TCP on localhost only)"
