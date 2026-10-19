#!/usr/bin/env python3
"""backfill.py — pull SPAN cloud history into the span TimescaleDB.

All historical data comes live from the SPAN cloud GraphQL. Responses past
a metric's retention edge are silent-failure padding (144 zero buckets per
day for any date past SPAN's 10-min retention window), not real data, so the
backfill detects and skips them rather than storing them.

Four-tier resolution strategy per metric (probed boundaries 2026-05-11):

  - **Tier A (10-min)**:  PHONE_PORTRAIT + DAY,    wb = 0..30          → ~30 calls
  - **Tier B (hourly)**:  PHONE_PORTRAIT + WEEK,   wb = 0..12          → ~12 calls
  - **Tier C (daily)**:   CALENDAR       + MONTH,  wb = 1..24          → ~24 calls
  - **Tier D (monthly)**: CALENDAR       + YEAR,   wb = 0..N_YEARS     → ~3 calls

A single panel-level GraphQL call returns solar + grid + battery (charging
+ discharging) + buildingConnection (4 attribution subtypes) in one shot.
A separate per-space call returns all 22 spaces.

Silent-failure detection: SPAN returns either a 2-bucket PT30M "today" probe
(for some queries) or a full-shape response with ALL zero values (for others)
when the requested window is past retention for that resolution. We detect
both and skip them, so historical rows aren't polluted with fake zeros.

Idempotent: ON CONFLICT (ts, metric) DO UPDATE.

Run from span-viewer LXC:
    /opt/span-viewer/.venv/bin/python -m ingester.backfill
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

import psycopg
import requests
from pycognito import Cognito
from tqdm import tqdm

CREDS = Path(os.environ.get('SPAN_CLOUD_CREDS', '/etc/span-viewer/credentials.json'))

USER_POOL_ID = 'us-west-2_xqz9y67ID'
COGNITO_CLIENT_ID = '21vd907gimk5ctc0pop94l2lip'
GRAPHQL_URL = 'https://app-api.prod.span-csp.com/graphql'

# Tier sizing — measured against actual SPAN retention boundaries
# (probed 2026-05-11; see project_span_cloud_api.md memory)
TIER_A_DAYS    = 30                                        # 10-min window  (PHONE_PORTRAIT + DAY)
TIER_B_WEEKS   = 12                                        # hourly window  (PHONE_PORTRAIT + WEEK)
TIER_C_MONTHS  = 24                                        # daily window   (CALENDAR + MONTH)
TIER_D_YEARS   = (datetime.datetime.now(datetime.timezone.utc).year - 2024) + 1   # monthly window (CALENDAR + YEAR)

# Polite throttle between cloud calls
SLEEP_S = 0.15


# ---------------------------------------------------------------------------
# SPAN cloud client
# ---------------------------------------------------------------------------

class SpanCloud:
    def __init__(self) -> None:
        cfg = json.loads(CREDS.read_text())
        self._email = cfg['email']
        self._password = cfg['password']
        self._auth()
        self.panel_id = self._discover_panel()

    def _auth(self) -> None:
        u = Cognito(USER_POOL_ID, COGNITO_CLIENT_ID, username=self._email)
        u.authenticate(password=self._password)
        self.token = u.access_token
        self._auth_at = time.time()

    def _maybe_refresh(self) -> None:
        if time.time() - self._auth_at > 50 * 60:
            self._auth()

    def gql(self, query: str, variables: dict | None = None,
            op: str | None = None, retries: int = 2) -> dict:
        for attempt in range(retries + 1):
            self._maybe_refresh()
            try:
                r = requests.post(
                    GRAPHQL_URL,
                    headers={'Authorization': f'Bearer {self.token}',
                             'Content-Type': 'application/json'},
                    json={'query': query,
                          'variables': variables or {},
                          'operationName': op},
                    timeout=30,
                )
                body = r.json()
                if 'errors' in body and attempt < retries:
                    msg = body['errors'][0].get('message', '')
                    if 'Internal server error' in msg:
                        time.sleep(1.5 ** attempt)
                        continue
                return body
            except (requests.RequestException, ValueError):
                if attempt < retries:
                    time.sleep(1.5 ** attempt)
                    continue
                raise

    def _discover_panel(self) -> str:
        r = self.gql('query CU { data: currentUser { buildings '
                     '{ multiPanels { panelId type } } } }')
        panels = r['data']['data']['buildings'][0]['multiPanels']
        live = next((p for p in panels if p.get('type') == 'LIVE'), panels[0])
        return live['panelId']


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PT_RE = re.compile(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$')


def iso_duration_to_seconds(s: str) -> int:
    if s.startswith('PT'):
        m = _PT_RE.match(s)
        if m:
            h, mn, sec = (int(g or 0) for g in m.groups())
            return h * 3600 + mn * 60 + sec
    return {'P1D': 86400, 'P1W': 7*86400, 'P1M': 30*86400, 'P1Y': 365*86400}[s]


def is_2_bucket_probe(ms: list) -> bool:
    if len(ms) != 2:
        return False
    if ms[0].get('duration') != 'PT30M':
        return False
    # UTC, like SPAN's startTime, whatever the process TZ is.
    today_iso = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    return all((m.get('startTime') or '').startswith(today_iso) for m in ms)


def all_zero(ms: list) -> bool:
    """The OTHER silent-failure mode: response has the right shape but every
    value is zero. Real days of consumption never have ALL buckets at zero,
    so we treat that as the API saying 'no data at this resolution.'"""
    if not ms:
        return True
    return all((m.get('value') or 0) == 0 for m in ms)


# Filter SPAN's future-month placeholders. CALENDAR/YEAR/wb=0 returns a full
# 12-month response even for the current year — every month after "now" is
# zero-valued. ISO-8601 UTC timestamps compare correctly as strings.
_NOW_ISO = datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z')


def is_future(start_time: str) -> bool:
    return bool(start_time) and start_time > _NOW_ISO


# ---------------------------------------------------------------------------
# Panel-level fetcher — solar + grid + battery + bc all in one query
# ---------------------------------------------------------------------------

# Template uses `%s` for the resolution literal because SPAN's GraphQL doesn't
# accept WindowResolution as a variable. The window-duration enum IS variable-able.
PANEL_QUERY_TPL = '''query PA($panelId: ID!, $duration: DurationWindow!, $wb: Int!) {
  data: getPanel(panelId: $panelId) {
    solar {
      mh: measurementHistory(
        type: ENERGY_OVER_TIME, subtype: ALL,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
      toGrid: measurementHistory(
        type: ENERGY_OVER_TIME, subtype: GRID,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
    }
    grid {
      mh: measurementHistory(
        type: ENERGY_OVER_TIME, subtype: CONSUMING,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
    }
    battery {
      ch: measurementHistory(
        type: ENERGY_OVER_TIME, subtype: CHARGING,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
      dc: measurementHistory(
        type: ENERGY_OVER_TIME, subtype: DISCHARGING,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
    }
    buildingConnection {
      bcTotal:    measurementHistory(
        type: ENERGY_OVER_TIME, subtype: ALL,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
      bcSolar:    measurementHistory(
        type: ENERGY_OVER_TIME, subtype: SOLAR,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
      bcBattery:  measurementHistory(
        type: ENERGY_OVER_TIME, subtype: BATTERY,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
      bcGrid:     measurementHistory(
        type: ENERGY_OVER_TIME, subtype: GRID,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
    }
  }
}'''

# Maps a (top-level-key, sub-alias) to the canonical metric name.
PANEL_FIELD_TO_METRIC = {
    ('solar',              'mh'):        'solar',
    ('solar',              'toGrid'):    'solar_to_grid',
    ('grid',               'mh'):        'grid_consuming',
    ('battery',            'ch'):        'battery_charging',
    ('battery',            'dc'):        'battery_discharging',
    ('buildingConnection', 'bcTotal'):   'building_total',
    ('buildingConnection', 'bcSolar'):   'building_from_solar',
    ('buildingConnection', 'bcBattery'): 'building_from_battery',
    ('buildingConnection', 'bcGrid'):    'building_from_grid',
}


def _panel_query(resolution: str) -> str:
    return PANEL_QUERY_TPL % ((resolution,) * 9)


def fetch_panel_window(cloud: SpanCloud, duration: str, resolution: str,
                       wb: int) -> list[tuple]:
    r = cloud.gql(
        _panel_query(resolution),
        {'panelId': cloud.panel_id, 'duration': duration, 'wb': wb},
        op='PA',
    )
    if 'errors' in r:
        msg = r['errors'][0].get('message', '')[:120]
        print(f'   ! panel query error at {resolution}/{duration}/wb={wb}: {msg}',
              file=sys.stderr)
        return []
    panel = ((r.get('data') or {}).get('data')) or {}
    rows = []
    for (top, alias), metric in PANEL_FIELD_TO_METRIC.items():
        ms = (((panel.get(top) or {}).get(alias)) or {}).get('measurements') or []
        if is_2_bucket_probe(ms):
            continue
        if all_zero(ms):
            # Real consumption is never all-zero across a whole day/month.
            # Treat as silent-fail and skip.
            continue
        for m in ms:
            v = m.get('value')
            if v is None:
                continue
            ts = m.get('startTime') or ''
            if is_future(ts):
                continue
            rows.append((ts, metric, abs(float(v)),
                         iso_duration_to_seconds(m['duration'])))
    return rows


def fetch_panel_all(cloud: SpanCloud) -> list[tuple]:
    rows: list[tuple] = []
    for wb in tqdm(range(0, TIER_A_DAYS + 1), desc='panel 10-min', unit='d'):
        rows.extend(fetch_panel_window(cloud, 'DAY', 'PHONE_PORTRAIT', wb))
        time.sleep(SLEEP_S)
    for wb in tqdm(range(0, TIER_B_WEEKS + 1), desc='panel hourly', unit='w'):
        rows.extend(fetch_panel_window(cloud, 'WEEK', 'PHONE_PORTRAIT', wb))
        time.sleep(SLEEP_S)
    for wb in tqdm(range(1, TIER_C_MONTHS + 1), desc='panel daily', unit='mo'):
        rows.extend(fetch_panel_window(cloud, 'MONTH', 'CALENDAR', wb))
        time.sleep(SLEEP_S)
    for wb in tqdm(range(0, TIER_D_YEARS + 1), desc='panel monthly', unit='y'):
        rows.extend(fetch_panel_window(cloud, 'YEAR', 'CALENDAR', wb))
        time.sleep(SLEEP_S)
    return rows


# ---------------------------------------------------------------------------
# Per-space fetcher — all 22 spaces in one query per window
# ---------------------------------------------------------------------------

# getSpace.room isn't a directly-queryable field on the Space type in
# SPAN's GraphQL — passing it returns 500. The SUMMARY.md's "room" values
# probably come from a nested type (`getSpace.room.name`?) — investigate
# later. For now just leave `room` NULL in the dim table.
SPACES_QUERY_TPL = '''query SP($panelId: ID!, $duration: DurationWindow!, $wb: Int!) {
  data: getPanel(panelId: $panelId) {
    spaces {
      spaceId
      name
      measurementHistory(
        type: ENERGY_OVER_TIME,
        window: { duration: $duration, windowsBack: $wb, resolution: %s, calendarWindows: true }
      ) { measurements { value startTime duration } }
    }
  }
}'''


def fetch_spaces_window(cloud: SpanCloud, duration: str, resolution: str,
                        wb: int) -> tuple[list, list]:
    """Returns (readings, meta).
    readings: (ts, space_id, energy_kwh, duration_s)
    meta:     (space_id, name, room)"""
    r = cloud.gql(
        SPACES_QUERY_TPL % resolution,
        {'panelId': cloud.panel_id, 'duration': duration, 'wb': wb},
        op='SP',
    )
    if 'errors' in r:
        msg = r['errors'][0].get('message', '')[:120]
        print(f'   ! spaces query error at {resolution}/{duration}/wb={wb}: {msg}',
              file=sys.stderr)
        return [], []
    spaces = (((r.get('data') or {}).get('data') or {}).get('spaces') or [])
    rows: list[tuple] = []
    meta: list[tuple] = []
    # Aggregate across all spaces to detect "all-zero window" silent fail
    all_ms = []
    for s in spaces:
        sid = s.get('spaceId')
        if not sid:
            continue
        meta.append((sid, s.get('name'), s.get('room')))
        ms = ((s.get('measurementHistory') or {}).get('measurements')) or []
        if is_2_bucket_probe(ms):
            continue
        all_ms.extend(ms)
        for m in ms:
            v = m.get('value')
            if v is None:
                continue
            ts = m.get('startTime') or ''
            if is_future(ts):
                continue
            rows.append((ts, sid, abs(float(v)),
                         iso_duration_to_seconds(m['duration'])))
    # If literally zero non-zero values across all 22 spaces for the window,
    # treat as silent fail. Real days always have at least the fridge running.
    if rows and all_zero(all_ms):
        return [], meta
    return rows, meta


def fetch_spaces_all(cloud: SpanCloud) -> tuple[list, list]:
    all_rows: list[tuple] = []
    meta_by_sid: dict[str, tuple] = {}

    def absorb_meta(meta):
        for sid, name, room in meta:
            if sid not in meta_by_sid:
                meta_by_sid[sid] = (name, room)

    for wb in tqdm(range(0, TIER_A_DAYS + 1), desc='spaces 10-min', unit='d'):
        rows, meta = fetch_spaces_window(cloud, 'DAY', 'PHONE_PORTRAIT', wb)
        all_rows.extend(rows); absorb_meta(meta)
        time.sleep(SLEEP_S)
    for wb in tqdm(range(0, TIER_B_WEEKS + 1), desc='spaces hourly', unit='w'):
        rows, meta = fetch_spaces_window(cloud, 'WEEK', 'PHONE_PORTRAIT', wb)
        all_rows.extend(rows); absorb_meta(meta)
        time.sleep(SLEEP_S)
    for wb in tqdm(range(1, TIER_C_MONTHS + 1), desc='spaces daily', unit='mo'):
        rows, meta = fetch_spaces_window(cloud, 'MONTH', 'CALENDAR', wb)
        all_rows.extend(rows); absorb_meta(meta)
        time.sleep(SLEEP_S)
    for wb in tqdm(range(0, TIER_D_YEARS + 1), desc='spaces monthly', unit='y'):
        rows, meta = fetch_spaces_window(cloud, 'YEAR', 'CALENDAR', wb)
        all_rows.extend(rows); absorb_meta(meta)
        time.sleep(SLEEP_S)

    return all_rows, [(sid, n, r) for sid, (n, r) in meta_by_sid.items()]


# ---------------------------------------------------------------------------
# DB writers
# ---------------------------------------------------------------------------

def db_connect():
    # DATABASE_URL, else span_app@localhost with the legacy password file.
    from app import config
    return psycopg.connect(config.database_url(), autocommit=False)


def truncate_facts(conn) -> None:
    # span_app doesn't own the tables, so TRUNCATE is denied. DELETE FROM
    # works with our DML grants, and the dataset is small enough (<2M rows)
    # that the perf difference is negligible.
    with conn.cursor() as cur:
        cur.execute('DELETE FROM space_readings;')
        cur.execute('DELETE FROM panel_readings;')


def bulk_upsert_panel(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute('CREATE TEMP TABLE _stg_panel ('
                    ' ts TIMESTAMPTZ NOT NULL, metric TEXT NOT NULL,'
                    ' energy_kwh DOUBLE PRECISION NOT NULL,'
                    ' duration_s INTEGER NOT NULL) ON COMMIT DROP;')
        with cur.copy('COPY _stg_panel (ts, metric, energy_kwh, duration_s) '
                       'FROM STDIN') as cp:
            for r in rows:
                cp.write_row(r)
        # Multi-tier fetches produce overlapping rows: a daily bucket's
        # midnight ts collides with the first 10-min bucket of the same day.
        # Prefer the finest granularity (smallest duration_s) — the daily
        # aggregate is recoverable by summing 10-min rows, but not vice versa.
        cur.execute('''
            INSERT INTO panel_readings (ts, metric, energy_kwh, duration_s)
            SELECT DISTINCT ON (ts, metric) ts, metric, energy_kwh, duration_s
            FROM _stg_panel
            ORDER BY ts, metric, duration_s ASC
            ON CONFLICT (ts, metric) DO UPDATE SET
                energy_kwh = EXCLUDED.energy_kwh,
                duration_s = EXCLUDED.duration_s
            WHERE EXCLUDED.duration_s <= panel_readings.duration_s;
        ''')
        cur.execute('SELECT COUNT(*) FROM _stg_panel;')
        return cur.fetchone()[0]


def bulk_upsert_spaces_dim(conn, meta: list[tuple]) -> int:
    if not meta:
        return 0
    with conn.cursor() as cur:
        cur.executemany('''
            INSERT INTO spaces (space_id, name, room) VALUES (%s, %s, %s)
            ON CONFLICT (space_id) DO UPDATE SET
                name = EXCLUDED.name, room = EXCLUDED.room, updated_at = NOW();
        ''', meta)
        return len(meta)


def bulk_upsert_space_readings(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.execute('CREATE TEMP TABLE _stg_space ('
                    ' ts TIMESTAMPTZ NOT NULL, space_id TEXT NOT NULL,'
                    ' energy_kwh DOUBLE PRECISION NOT NULL,'
                    ' duration_s INTEGER NOT NULL) ON COMMIT DROP;')
        with cur.copy('COPY _stg_space (ts, space_id, energy_kwh, duration_s) '
                       'FROM STDIN') as cp:
            for r in rows:
                cp.write_row(r)
        cur.execute('''
            INSERT INTO space_readings (ts, space_id, energy_kwh, duration_s)
            SELECT DISTINCT ON (ts, space_id) ts, space_id, energy_kwh, duration_s
            FROM _stg_space
            ORDER BY ts, space_id, duration_s ASC
            ON CONFLICT (ts, space_id) DO UPDATE SET
                energy_kwh = EXCLUDED.energy_kwh,
                duration_s = EXCLUDED.duration_s
            WHERE EXCLUDED.duration_s <= space_readings.duration_s;
        ''')
        cur.execute('SELECT COUNT(*) FROM _stg_space;')
        return cur.fetchone()[0]


def open_run(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ingest_runs (status) VALUES ('running') "
                    "RETURNING id;")
        return cur.fetchone()[0]


def close_run(conn, run_id: int, status: str, **kwargs) -> None:
    parts = ['finished_at = NOW()', f"status = '{status}'"]
    params = []
    for k, v in kwargs.items():
        parts.append(f'{k} = %s')
        params.append(v)
    params.append(run_id)
    with conn.cursor() as cur:
        cur.execute(f"UPDATE ingest_runs SET {', '.join(parts)} WHERE id = %s",
                    params)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--truncate', action='store_true',
                    help='Wipe panel_readings + space_readings before loading. '
                         'Use when an earlier run polluted the tables with '
                         'silent-failure zero rows from the JSON archive.')
    ap.add_argument('--dry-run', action='store_true',
                    help='Hit SPAN cloud but skip DB writes (for shape testing).')
    args = ap.parse_args()

    started = time.time()
    print(f'== span-viewer backfill — {datetime.datetime.now().isoformat(timespec="seconds")} ==')
    print(f'   tier sizing: A(10-min)={TIER_A_DAYS}d  B(hourly)={TIER_B_WEEKS}w  '
          f'C(daily)={TIER_C_MONTHS}mo  D(monthly)={TIER_D_YEARS}y')

    conn = None if args.dry_run else db_connect()
    run_id = None

    try:
        if conn:
            run_id = open_run(conn)
            conn.commit()
            print(f'   ingest_runs.id = {run_id}')
            if args.truncate:
                print('   TRUNCATE panel_readings + space_readings')
                truncate_facts(conn)
                conn.commit()

        print('\n-- authenticating to SPAN cloud --')
        cloud = SpanCloud()
        print(f'   panel_id: {cloud.panel_id}')

        print('\n-- fetching panel-level (solar/grid/battery/buildingConnection) --')
        panel_rows = fetch_panel_all(cloud)
        print(f'   {len(panel_rows):,} panel rows after silent-fail filtering')
        if conn and panel_rows:
            n = bulk_upsert_panel(conn, panel_rows)
            conn.commit()
            print(f'   upserted {n:,} panel rows')

        print('\n-- fetching per-space (22 circuits batched per window) --')
        space_rows, space_meta = fetch_spaces_all(cloud)
        print(f'   {len(space_rows):,} space rows, {len(space_meta)} unique spaces')
        if conn:
            if space_meta:
                bulk_upsert_spaces_dim(conn, space_meta)
                print(f'   upserted {len(space_meta)} space dim rows')
            if space_rows:
                n = bulk_upsert_space_readings(conn, space_rows)
                conn.commit()
                print(f'   upserted {n:,} space readings')

        if conn:
            panel_ts = [r[0] for r in panel_rows] if panel_rows else []
            close_run(conn, run_id, 'success',
                      panel_rows_in=len(panel_rows),
                      space_rows_in=len(space_rows),
                      panel_period_start=min(panel_ts) if panel_ts else None,
                      panel_period_end=max(panel_ts) if panel_ts else None)
            conn.commit()
    except Exception as e:
        if conn and run_id:
            try:
                close_run(conn, run_id, 'failed', error_text=str(e)[:1000])
                conn.commit()
            except Exception:
                pass
        raise
    finally:
        if conn:
            conn.close()

    print(f'\n== done in {time.time() - started:.1f}s ==')
    return 0


if __name__ == '__main__':
    sys.exit(main())
