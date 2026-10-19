#!/usr/bin/env python3
"""apply_schema.py — apply db/schema/*.sql in order over DATABASE_URL.

The psql-free twin of apply-schema.sh, for CI and containers: no superuser
and no local socket needed, just a role that owns the database (TimescaleDB
already created as an extension in it, as the timescaledb image does). Each
file is idempotent, so re-running is safe.

    python scripts/apply_schema.py [--wait SECONDS]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--wait', type=float, default=0,
                    help='keep retrying the connection for this many seconds')
    args = ap.parse_args()
    deadline = time.monotonic() + args.wait
    while True:
        try:
            conn = psycopg.connect(config.database_url(), autocommit=True)
            break
        except psycopg.OperationalError as e:
            if time.monotonic() > deadline:
                print(f'apply_schema: cannot connect: {e}', file=sys.stderr)
                return 1
            time.sleep(1)
    with conn:
        conn.execute('CREATE EXTENSION IF NOT EXISTS timescaledb')
        for f in sorted((ROOT / 'db' / 'schema').glob('*.sql')):
            print(f'== applying {f.name} ==')
            conn.execute(f.read_text(encoding='utf-8'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
