#!/usr/bin/env python3
"""sample_data.py — fill an empty span-viewer database with a synthetic house.

So the app can be tried, tested and screenshotted with no SPAN panel. Every
number is made up: a 10 kW solar array and 14 circuits whose loads follow the
season, the hour and a seeded random schedule (dryer loads, EV charges, a
fridge's compressor, a dehumidifier and a furnace burner that cycle). The
same seed gives the same house.

Written like the real ingest writes it, at the resolutions SPAN's cloud keeps:
10-minute buckets for the last 45 days, hourly for the 75 days before, daily
back two years, monthly for the three months before that. Plus 24 h of 3-s
`panel_live` / `space_live` rows (the /live strip chart and the NILM pages)
and one ingest run. The circuits match circuits.example.yaml.

Usage (DATABASE_URL or the config file names the database):
    python scripts/sample_data.py              # refuses a database with readings
    python scripts/sample_data.py --if-empty   # exit 0 quietly when it has some
    python scripts/sample_data.py --days 120   # a shorter history
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import math
import sys
from pathlib import Path

import numpy as np
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

UTC = dt.timezone.utc
STEP = 600                      # seconds per base bucket
LIVE_STEP = 3                   # seconds per live sample

# (space_id, name, room). Ids are fixed so URLs stay stable across reruns.
CIRCUITS = [
    ('sample0000000000000000000000dryer', 'Dryer', 'Laundry'),
    ('sample00000000000000000000000oven', 'Oven & Range', 'Kitchen'),
    ('sample000000000000000000000water', 'Water Heater', 'Basement'),
    ('sample0000000000000000000000fridge', 'Refrigerator', 'Kitchen'),
    ('sample0000000000000000000kitchen', 'Kitchen Outlets & Microwave', 'Kitchen'),
    ('sample00000000000000000000000ev', 'EV Charger', 'Garage'),
    ('sample000000000000000000dehumid', 'Dehumidifier', 'Basement'),
    ('sample0000000000000000000furnace', 'Furnace', 'Basement'),
    ('sample000000000000000000heatpump', 'Heat Pump', 'Attic'),
    ('sample00000000000000000000lights', 'Lights', None),
    ('sample00000000000000000000office', 'Office', 'Office'),
    ('sample0000000000000000000living', 'Living Room Outlets', 'Living Room'),
    ('sample000000000000000000network', 'Network & Always-on', 'Basement'),
    ('sample000000000000000000subpanel', 'Sub-panel', None),
]
SOLAR_KW = 10.0


def _local_hours(ts: np.ndarray, tz: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Local hour-of-day (float), day index, and day-of-year for epoch seconds."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    # One UTC offset per local day is plenty for a synthetic house.
    days = np.floor(ts / 86400).astype(np.int64)
    uniq = np.unique(days)
    off = {d: z.utcoffset(dt.datetime.fromtimestamp(int(d) * 86400 + 43200)).total_seconds()
           for d in uniq}
    offs = np.vectorize(off.get)(days)
    local = ts + offs
    hour = (local % 86400) / 3600.0
    day = np.floor(local / 86400).astype(np.int64)
    doy = np.array([dt.date.fromordinal(719163 + int(d)).timetuple().tm_yday for d in day])
    return hour, day, doy


def weather(days: np.ndarray, doy: np.ndarray, rng) -> dict:
    """Per local day: mean/min/max °F, cloud %, radiation, snow."""
    uniq, idx = np.unique(days, return_index=True)
    d = doy[idx]
    season = np.cos(2 * math.pi * (d - 200) / 365.25)          # +1 mid-July
    t_avg = 50 + 24 * season + rng.normal(0, 6, len(uniq))
    spread = 14 + rng.normal(0, 3, len(uniq))
    cloud = np.clip(rng.beta(1.6, 1.8, len(uniq)) * 100, 0, 100)
    clear_rad = 4.6 + 3.0 * season                             # kWh/m² on a clear day
    rad = np.clip(clear_rad * (1 - 0.72 * cloud / 100), 0.2, None)
    snow = np.where((t_avg < 30) & (cloud > 70), rng.uniform(0, 8, len(uniq)), 0.0)
    return {
        'day': uniq, 't_avg': t_avg, 't_min': t_avg - spread / 2, 't_max': t_avg + spread / 2,
        'cloud': cloud, 'rad': rad, 'precip': np.where(cloud > 75, rng.uniform(0, 12, len(uniq)), 0.0),
        'snow_cm': snow, 'snow_depth_m': snow / 100,
    }


def model(ts: np.ndarray, tz: str, seed: int) -> tuple[dict, np.ndarray, dict]:
    """kW per circuit per 10-min bucket, solar kW, and the made-up daily weather
    that drives them."""
    rng = np.random.default_rng(seed)
    hour, day, doy = _local_hours(ts, tz)
    w = weather(day, doy, rng)
    di = np.searchsorted(w['day'], day)            # bucket -> weather row
    t = w['t_avg'][di]
    hdd = np.clip(65 - t, 0, None)
    cdd = np.clip(t - 70, 0, None)
    n = len(ts)
    kw = {}

    def bumps(prob_per_day, start_h, spread_h, length, power, jitter=0.0):
        """Random sessions: per day with prob, start ~N(start_h, spread_h)."""
        out = np.zeros(n)
        first = {}
        for i, d in enumerate(day):
            first.setdefault(d, i)
        for d, i0 in first.items():
            if rng.random() >= prob_per_day:
                continue
            start = i0 + int(max(0.0, rng.normal(start_h, spread_h)) * 6) - int(hour[i0] * 6)
            ln = max(1, int(length + rng.integers(-1, 2)))
            out[max(start, 0):max(start + ln, 0)] += power * (1 + rng.normal(0, jitter))
        return out

    def cycling(on_kw, duty, period_buckets=6):
        """A compressor-style on/off square wave with a given duty (array)."""
        phase = (np.arange(n) + rng.integers(0, period_buckets)) % period_buckets
        return np.where(phase < np.round(duty * period_buckets), on_kw, 0.0)

    kw['Dryer'] = bumps(0.35, 18, 3, 6, 2.6, 0.05)
    kw['Oven & Range'] = bumps(0.7, 17.5, 1, 4, 1.8, 0.2)
    wh = 0.01 + bumps(0.95, 7, 0.7, 3, 0.35) + bumps(0.95, 19, 1.2, 3, 0.35)
    wh += bumps(0.06, 8, 1, 1, 4.4)                      # resistive backup
    kw['Water Heater'] = wh
    kw['Refrigerator'] = 0.004 + cycling(0.09, np.full(n, 0.4))
    kw['Kitchen Outlets & Microwave'] = (0.03 + bumps(0.8, 7.5, 0.5, 1, 1.1)
                                         + bumps(0.6, 12.3, 0.5, 1, 1.1)
                                         + bumps(0.7, 18.5, 0.6, 1, 1.2))
    kw['EV Charger'] = bumps(0.4, 22.5, 1, 22, 7.2, 0.02)
    humid = np.clip((t - 45) / 30, 0.05, 0.9)
    kw['Dehumidifier'] = cycling(0.45, humid, 9) + 0.002
    # A burner fires for part of a bucket, so its on-time isn't whole buckets.
    kw['Furnace'] = 0.012 + cycling(0.108, np.clip(hdd / 45, 0, 0.85), 6) * rng.uniform(0.3, 1.0, n)
    kw['Heat Pump'] = np.where(cdd > 0, cycling(1.4, np.clip(cdd / 15, 0, 0.9), 6), 0.0) + 0.005
    lit = ((hour > 6) & (hour < 7.5)) | ((hour > 17.5) & (hour < 23.5))
    kw['Lights'] = np.where(lit, 0.22, 0.02)
    workday = (((day + 4) % 7) < 5)                      # 1970-01-01 was a Thursday
    kw['Office'] = 0.06 + np.where(workday & (hour > 8.5) & (hour < 17.5), 0.18, 0.0)
    kw['Living Room Outlets'] = 0.05 + np.where((hour > 19) & (hour < 23), 0.16, 0.0)
    kw['Network & Always-on'] = np.full(n, 0.045)
    kw['Sub-panel'] = 0.09 + np.where((hour > 6.5) & (hour < 22.5), 0.11, 0.0)
    for k in kw:
        kw[k] = np.clip(kw[k] * (1 + rng.normal(0, 0.03, n)), 0, None)

    # Solar: a sine over daylight, scaled by the day's radiation.
    daylen = 12 + 3.2 * np.cos(2 * math.pi * (doy - 172) / 365.25)
    sunrise = 12.8 - daylen / 2
    x = (hour - sunrise) / daylen
    shape = np.where((x > 0) & (x < 1), np.sin(math.pi * np.clip(x, 0, 1)) ** 1.3, 0.0)
    rad = w['rad'][di]
    solar = SOLAR_KW * shape * (rad / 7.6) * (1 + rng.normal(0, 0.08, n))
    return kw, np.clip(solar, 0, SOLAR_KW), w, day


def layout(now: dt.datetime, days: int, tz: str) -> tuple[dt.datetime, dict]:
    """The first bucket (a local midnight at the start of a month) and the
    start of each finer resolution: {'day': .., 'hour': .., 'ten': ..}."""
    from zoneinfo import ZoneInfo
    z = ZoneInfo(tz)
    local_now = now.astimezone(z)

    def midnight(d: dt.date) -> dt.datetime:
        return dt.datetime(d.year, d.month, d.day, tzinfo=z).astimezone(UTC)

    day_start = (local_now - dt.timedelta(days=days)).date()
    hour_start = (local_now - dt.timedelta(days=min(120, days))).date()
    ten = now - dt.timedelta(days=min(45, days))
    ten = ten.replace(minute=0)
    first = day_start.replace(day=1)
    if days > 200:
        # Three monthly buckets before the daily ones, like SPAN's oldest tier.
        y, m = first.year, first.month - 3
        if m < 1:
            y, m = y - 1, m + 12
        first = dt.date(y, m, 1)
        day_start = day_start.replace(day=1)
    else:
        first = day_start
    return midnight(first), {'day': midnight(day_start), 'hour': midnight(hour_start), 'ten': ten}


def _copy(cur, table: str, cols: str, rows) -> int:
    buf = io.StringIO()
    n = 0
    for r in rows:
        buf.write('\t'.join('\\N' if v is None else str(v) for v in r) + '\n')
        n += 1
    buf.seek(0)
    with cur.copy(f'COPY {table} ({cols}) FROM STDIN') as cp:
        while chunk := buf.read(1 << 20):
            cp.write(chunk)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--days', type=int, default=730, help='history length (default 730)')
    ap.add_argument('--seed', type=int, default=7)
    ap.add_argument('--if-empty', action='store_true',
                    help='exit 0 without writing when the database already has readings')
    ap.add_argument('--no-live', action='store_true', help='skip the 24 h of 3-s rows')
    args = ap.parse_args()
    tz = config.tz()

    conn = psycopg.connect(config.database_url(), autocommit=False)
    with conn.cursor() as cur:
        cur.execute('SELECT EXISTS (SELECT 1 FROM panel_readings LIMIT 1)')
        if cur.fetchone()[0]:
            if args.if_empty:
                print('sample_data: the database already has readings; nothing to do')
                return 0
            print('sample_data: refusing: panel_readings is not empty', file=sys.stderr)
            return 1

    now = dt.datetime.now(UTC).replace(second=0, microsecond=0)
    now = now.replace(minute=now.minute - now.minute % 10)
    start, edges = layout(now, args.days, tz)
    ts = np.arange(int(start.timestamp()), int(now.timestamp()) + 1, STEP, dtype=np.int64)
    print(f'sample_data: {len(ts)} buckets of 10 min from {start:%Y-%m-%d} ({tz})')
    kw, solar, w, lday = model(ts, tz, args.seed)

    names = [c[1] for c in CIRCUITS]
    load = sum(kw[nm] for nm in names)
    from_solar = np.minimum(load, solar)
    panel = {
        'solar': solar, 'building_total': load,
        'building_from_solar': from_solar, 'building_from_grid': load - from_solar,
        'grid_consuming': load - from_solar, 'solar_to_grid': solar - from_solar,
    }
    to_kwh = STEP / 3600.0

    # Group key per bucket: local month, local day, UTC hour, or itself.
    e_day, e_hour, e_ten = (int(edges[k].timestamp()) for k in ('day', 'hour', 'ten'))
    months = np.array([(d.year * 12 + d.month) for d in
                       (dt.date.fromordinal(719163 + int(x)) for x in lday)])
    key = np.where(ts >= e_ten, ts,
          np.where(ts >= e_hour, (ts // 3600) * 3600,
          np.where(ts >= e_day, -lday, -10_000_000 - months)))
    dur = np.where(ts >= e_ten, STEP,
          np.where(ts >= e_hour, 3600,
          np.where(ts >= e_day, 86400, 30 * 86400)))
    # Group in order of time; each group's ts is its first bucket's.
    change = np.r_[True, key[1:] != key[:-1]]
    gid = np.cumsum(change) - 1
    g_ts = ts[change]
    g_dur = dur[change]

    def bucketed(series_kw: np.ndarray):
        sums = np.bincount(gid, weights=series_kw) * to_kwh
        for t, kwh, d in zip(g_ts, sums, g_dur):
            yield dt.datetime.fromtimestamp(int(t), UTC), float(kwh), int(d)

    with conn.cursor() as cur:
        cur.executemany(
            'INSERT INTO spaces (space_id, name, room) VALUES (%s, %s, %s) '
            'ON CONFLICT (space_id) DO UPDATE SET name = EXCLUDED.name, room = EXCLUDED.room',
            CIRCUITS)
        n_panel = _copy(cur, 'panel_readings', 'ts, metric, energy_kwh, duration_s',
                        ((t.isoformat(), metric, f'{kwh:.6f}', dur)
                         for metric, series in panel.items() for t, kwh, dur in bucketed(series)))
        n_space = _copy(cur, 'space_readings', 'ts, space_id, energy_kwh, duration_s',
                        ((t.isoformat(), sid, f'{kwh:.6f}', dur)
                         for sid, nm, _ in CIRCUITS for t, kwh, dur in bucketed(kw[nm])))
        print(f'sample_data: {n_panel} panel_readings, {n_space} space_readings')

        if not args.no_live:
            n_live = live_rows(cur, kw, solar, ts, now, args.seed)
            print(f'sample_data: {n_live} space_live rows (24 h at 3 s)')

        cur.execute(
            "INSERT INTO ingest_runs (started_at, finished_at, status, panel_rows_in, "
            "space_rows_in, panel_period_start, panel_period_end) "
            "VALUES (NOW() - INTERVAL '9 seconds', NOW(), 'success', %s, %s, %s, %s)",
            (n_panel, n_space, start, now))
    conn.commit()

    # Continuous aggregates refresh outside a transaction, finest first
    # (the monthly views are built on the daily ones).
    conn.autocommit = True
    with conn.cursor() as cur:
        for view in ('panel_hourly', 'panel_daily', 'panel_monthly',
                     'space_hourly', 'space_daily', 'space_monthly'):
            cur.execute(f"CALL refresh_continuous_aggregate('{view}', NULL, NULL)")
    conn.close()
    print('sample_data: done')
    return 0


def live_rows(cur, kw: dict, solar: np.ndarray, ts: np.ndarray, now: dt.datetime, seed: int) -> int:
    """24 h of 3-s rows: each 10-min bucket's average, with on/off loads as
    square waves (so the edge detector has edges to find)."""
    rng = np.random.default_rng(seed + 1)
    end = int(now.timestamp())
    t_live = np.arange(end - 86400, end, LIVE_STEP, dtype=np.int64)
    bucket = np.searchsorted(ts, t_live, side='right') - 1
    in_bucket = (t_live - ts[bucket]) / STEP                # 0..1 through the bucket
    on_w = {'Dryer': 2600, 'Oven & Range': 1800, 'Refrigerator': 90, 'Dehumidifier': 450,
            'Furnace': 108, 'Heat Pump': 1400, 'EV Charger': 7200, 'Water Heater': 350,
            'Kitchen Outlets & Microwave': 1100}
    series = {}
    for sid, nm, _ in CIRCUITS:
        avg_w = kw[nm][bucket] * 1000
        if nm in on_w:
            base = np.minimum(avg_w, 15.0)
            duty = np.clip((avg_w - base) / on_w[nm], 0, 1)
            series[sid] = base + np.where(in_bucket < duty, on_w[nm], 0.0)
        else:
            series[sid] = avg_w * (1 + rng.normal(0, 0.02, len(t_live)))
    sub = series['sample000000000000000000subpanel']
    rows = []
    for sid, s in series.items():
        if sid == 'sample000000000000000000subpanel':
            continue        # the feedthrough lugs: live data names it __feedthrough__
        rows.append((sid, s))
    rows.append(('__feedthrough__', sub))
    n = _copy(cur, 'space_live', 'ts, space_id, power_w',
              ((dt.datetime.fromtimestamp(int(t), UTC).isoformat(), sid, f'{p:.1f}')
               for sid, s in rows for t, p in zip(t_live, s)))
    load = sum(s for s in series.values())
    sol = solar[bucket] * 1000 * (1 + rng.normal(0, 0.01, len(t_live)))
    sol = np.clip(sol, 0, None)
    grid = load - sol
    _copy(cur, 'panel_live',
          'ts, grid_power_w, feedthrough_power_w, solar_power_w, load_power_w, phase_a_w, phase_b_w',
          ((dt.datetime.fromtimestamp(int(t), UTC).isoformat(), f'{g:.1f}', f'{f:.1f}',
            f'{s:.1f}', f'{ld:.1f}', f'{ld * 0.52:.1f}', f'{ld * 0.48:.1f}')
           for t, g, f, s, ld in zip(t_live, grid, sub, sol, load)))
    return n


if __name__ == '__main__':
    sys.exit(main())
