"""SQL queries the UI needs. Kept as plain functions so they're easy to
test independently of the FastAPI routes."""

import datetime
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from . import config
from .db import pool

# Local time zone for "what month did this energy land in" rollups: config
# TZ (an IANA name, checked by config.tz(), so it's safe inside SQL).
TZ = config.tz()


def today_local() -> datetime.date:
    """Today's date in the configured local time zone.

    The server may run on UTC while the panel's owner lives elsewhere;
    `date.today()` would return tomorrow's date for hours after midnight UTC,
    so anything that asks "what is today" must go through this helper.
    """
    return datetime.datetime.now(ZoneInfo(TZ)).date()


_SOLAR_START: dict = {}


def solar_start() -> dict:
    """When solar production starts in this panel's history, from the data:
    `first_day`, the first local day with at least 0.5 kWh of solar, and
    `first_full_month`, the 1st of the month after it (or that day when it's
    a 1st). Both None with no solar. Cached once found."""
    if _SOLAR_START:
        return _SOLAR_START
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT MIN(d) FROM (
                SELECT (ts AT TIME ZONE '{TZ}')::date AS d
                FROM panel_readings
                WHERE metric = 'solar' AND duration_s <= 86400
                GROUP BY 1 HAVING SUM(energy_kwh) >= 0.5
            ) s;
            """
        )
        first = (cur.fetchone() or [None])[0]
    if first is None:
        return {'first_day': None, 'first_full_month': None}
    full = first if first.day == 1 else (
        first.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    _SOLAR_START.update(first_day=first, first_full_month=full)
    return _SOLAR_START


def _solar_full_month_sql() -> str:
    """A TIMESTAMPTZ literal for solar_start()'s first full month (the far
    future with no solar, so the filter matches nothing)."""
    d = solar_start()['first_full_month'] or datetime.date(9999, 1, 1)
    return f"TIMESTAMPTZ '{d.isoformat()} 00:00 {TZ}'"

# Sentinel for the whole-house selection in the UI.
WHOLE_HOUSE = 'whole_house'


# Per-window bucket sizing for /api/live/history. Tuples are
# (period, bucket_width); period drives the WHERE clause, bucket_width
# is the time_bucket() argument that downsamples the response so a 24-h
# zoom doesn't return ~28k rows.
_LIVE_WINDOWS: dict[str, tuple[str, str]] = {
    '5m':  ('5 minutes',  '3 seconds'),   # ~100 raw points
    '1h':  ('1 hour',     '1 minute'),    # 60 points
    '6h':  ('6 hours',    '5 minutes'),   # 72 points
    '24h': ('24 hours',   '15 minutes'),  # 96 points
}


def live_history(window: str = '5m') -> dict:
    """Downsampled panel_live time series for the /live strip chart.

    `window` selects one of the predefined zoom levels. Unknown values
    fall back to '5m'. The downsample bucket scales with the window so
    the chart receives ~60-100 points regardless of zoom — keeps ECharts
    happy on phones."""
    if window not in _LIVE_WINDOWS:
        window = '5m'
    period, bucket = _LIVE_WINDOWS[window]
    # period / bucket come from a hardcoded dict so it's safe to f-string
    # them into the INTERVAL literals. No user-controlled SQL.
    sql = f"""
        SELECT
            time_bucket(INTERVAL '{bucket}', ts) AS ts,
            AVG(grid_power_w)::float        AS grid_w,
            AVG(solar_power_w)::float       AS solar_w,
            AVG(load_power_w)::float        AS load_w,
            AVG(feedthrough_power_w)::float AS feedthrough_w
        FROM panel_live
        WHERE ts >= NOW() - INTERVAL '{period}'
        GROUP BY 1
        ORDER BY 1;
    """
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    return {
        'window':  window,
        'period':  period,
        'bucket':  bucket,
        'samples': [{
            'ts':      r['ts'].isoformat(),
            'grid_w':  r['grid_w'],
            'solar_w': r['solar_w'],
            'load_w':  r['load_w'],
            'feedthrough_w': r['feedthrough_w'],
        } for r in rows],
    }


def load_24h_ago_w() -> dict:
    """Average house load (W) for the 10-min bucket nearest ~24 h ago.

    Used by /live's same-time-yesterday tile. Returns
        {ts: ISO timestamp, load_w: float}
    or
        {ts: None, load_w: None}
    if no bucket lands in the ±5 min lookup window (e.g. ingest paused
    or we're inside the first 24 h of the panel's lifetime). Reads from
    panel_readings 'building_total' — the same gross-consumption metric
    /live derives client-side from circuits + feedthrough."""
    sql = """
        SELECT ts, energy_kwh::float AS energy_kwh, duration_s
        FROM panel_readings
        WHERE metric = 'building_total'
          AND duration_s <= 600
          AND ts >= NOW() - INTERVAL '24 hours 5 minutes'
          AND ts <= NOW() - INTERVAL '23 hours 55 minutes'
        ORDER BY ts ASC
        LIMIT 1;
    """
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql)
        row = cur.fetchone()
    if not row:
        return {'ts': None, 'load_w': None}
    return {
        'ts': row['ts'].isoformat(),
        # energy_kwh over duration_s seconds → average W:
        # kWh × 3.6e6 J/kWh ÷ duration_s = J/s = W
        'load_w': row['energy_kwh'] * 3_600_000.0 / float(row['duration_s']),
    }


def list_circuits() -> list[dict]:
    """Return circuits for the dropdown, sorted by total consumption desc.
    The first row is always 'Whole house' (uses panel_readings.building_total)."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            WITH totals AS (
              SELECT space_id, SUM(energy_kwh) AS total_kwh
              FROM space_readings GROUP BY space_id
            )
            SELECT s.space_id, s.name, COALESCE(t.total_kwh, 0) AS total_kwh
            FROM spaces s LEFT JOIN totals t USING (space_id)
            ORDER BY t.total_kwh DESC NULLS LAST, s.name;
            """
        )
        circuits = cur.fetchall()

        cur.execute(
            "SELECT COALESCE(SUM(energy_kwh), 0) AS total_kwh "
            "FROM panel_readings WHERE metric = 'building_total';"
        )
        whole_house_total = cur.fetchone()['total_kwh']

    return [
        {'id': WHOLE_HOUSE, 'name': 'Whole house', 'total_kwh': whole_house_total},
        *circuits,
    ]


def yoy_monthly(circuit_id: str) -> dict:
    """Return year-over-year monthly kWh for the given circuit.

    Output shape:
      {
        'circuit_id': str,
        'circuit_name': str,
        'years':  [2024, 2025, 2026],
        'months': [{'year': 2024, 'month': 1, 'kwh': 0.0}, ...]
      }
    The shape is designed for direct rendering by ECharts."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if circuit_id == WHOLE_HOUSE:
            circuit_name = 'Whole house'
            cur.execute(
                f"""
                SELECT
                    EXTRACT(YEAR  FROM ts AT TIME ZONE '{TZ}')::int  AS year,
                    EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}')::int  AS month,
                    SUM(energy_kwh)::float                           AS kwh
                FROM panel_readings
                WHERE metric = 'building_total'
                GROUP BY 1, 2
                HAVING SUM(energy_kwh) > 0  -- ignore all-zero backfill stub months
                ORDER BY 1, 2;
                """
            )
        else:
            cur.execute("SELECT name FROM spaces WHERE space_id = %s;", (circuit_id,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f'unknown circuit_id: {circuit_id}')
            circuit_name = row['name']
            cur.execute(
                f"""
                SELECT
                  EXTRACT(YEAR  FROM ts AT TIME ZONE '{TZ}')::int  AS year,
                  EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}')::int  AS month,
                  SUM(energy_kwh)                                  AS kwh
                FROM space_readings
                WHERE space_id = %s
                GROUP BY 1, 2 ORDER BY 1, 2;
                """,
                (circuit_id,),
            )
        months = [dict(r) for r in cur.fetchall()]

    years = sorted({m['year'] for m in months})

    return {
        'circuit_id': circuit_id,
        'circuit_name': circuit_name,
        'years': years,
        'months': months,
    }


def top_consumers(window: str = '30d') -> list[dict]:
    """Per-circuit kWh totals for a chosen window, sorted desc.

    `window` is one of '30d', '90d', '12mo', 'all'. Returns [{name, kwh, share}]
    where `share` is the circuit's fraction of total LAN consumption in the window.
    """
    interval_map = {
        '30d':  "INTERVAL '30 days'",
        '90d':  "INTERVAL '90 days'",
        '12mo': "INTERVAL '12 months'",
    }
    where = ''
    if window in interval_map:
        where = f"WHERE ts >= NOW() - {interval_map[window]}"
    elif window != 'all':
        raise ValueError(f'unknown window: {window}')

    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT s.name AS name, SUM(sr.energy_kwh)::float AS kwh
            FROM space_readings sr JOIN spaces s USING (space_id)
            {where}
            GROUP BY s.name ORDER BY kwh DESC;
            """
        )
        rows = cur.fetchall()

    total = sum(r['kwh'] for r in rows) or 1.0
    return [
        {'name': r['name'], 'kwh': r['kwh'], 'share': r['kwh'] / total}
        for r in rows
    ]


def daily_kwh_for_year(year: int, circuit_id: str = WHOLE_HOUSE) -> list[dict]:
    """Daily kWh totals for a calendar year — drives the heatmap.

    Returns [{date: 'YYYY-MM-DD', kwh: float}] sorted ascending."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if circuit_id == WHOLE_HOUSE:
            cur.execute(
                f"""
                SELECT (ts AT TIME ZONE '{TZ}')::date::text AS date,
                       SUM(energy_kwh)::float                AS kwh
                FROM panel_readings
                WHERE metric = 'building_total'
                  AND ts >= make_timestamptz(%s, 1, 1, 0, 0, 0, %s)
                  AND ts <  make_timestamptz(%s, 1, 1, 0, 0, 0, %s)
                GROUP BY 1 ORDER BY 1;
                """,
                (year, TZ, year + 1, TZ),
            )
        else:
            cur.execute(
                f"""
                SELECT (ts AT TIME ZONE '{TZ}')::date::text AS date,
                       SUM(energy_kwh)::float                AS kwh
                FROM space_readings
                WHERE space_id = %s
                  AND ts >= make_timestamptz(%s, 1, 1, 0, 0, 0, %s)
                  AND ts <  make_timestamptz(%s, 1, 1, 0, 0, 0, %s)
                GROUP BY 1 ORDER BY 1;
                """,
                (circuit_id, year, TZ, year + 1, TZ),
            )
        return [dict(r) for r in cur.fetchall()]


def years_with_data() -> list[int]:
    """Calendar years that have at least one panel reading. For the heatmap
    year-picker."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT EXTRACT(YEAR FROM ts AT TIME ZONE '{TZ}')::int AS y
            FROM panel_readings
            WHERE metric = 'building_total'
            ORDER BY y;
            """
        )
        return [r[0] for r in cur.fetchall()]


def monthly_kwh_lifetime(circuit_id: str = WHOLE_HOUSE) -> list[dict]:
    """Monthly kWh over the panel's whole history, for the year x month
    "lifetime" heatmap. Whole house is the panel's building_total; a circuit
    is its own readings.

    Returns [{year, month, kwh}] sorted ascending."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if circuit_id == WHOLE_HOUSE:
            cur.execute(
                f"""
                SELECT
                    EXTRACT(YEAR  FROM ts AT TIME ZONE '{TZ}')::int  AS year,
                    EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}')::int  AS month,
                    SUM(energy_kwh)::float                           AS kwh
                FROM panel_readings
                WHERE metric = 'building_total'
                GROUP BY 1, 2
                HAVING SUM(energy_kwh) > 0
                ORDER BY 1, 2;
                """
            )
        else:
            cur.execute(
                f"""
                SELECT
                    EXTRACT(YEAR  FROM ts AT TIME ZONE '{TZ}')::int  AS year,
                    EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}')::int  AS month,
                    SUM(energy_kwh)::float                           AS kwh
                FROM space_readings
                WHERE space_id = %s
                GROUP BY 1, 2 ORDER BY 1, 2;
                """,
                (circuit_id,),
            )
        return [dict(r) for r in cur.fetchall()]


def solar_self_consumption_monthly() -> list[dict]:
    """Monthly breakdown of where each kWh of consumption came from.

    Returns [{month: 'YYYY-MM-DD', total, from_solar, from_grid, solar_pct}].
    Math is `building_total = building_from_solar + building_from_grid`
    (already validated; no battery on this panel)."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                date_trunc('month', ts AT TIME ZONE '{TZ}')::date::text AS month,
                SUM(energy_kwh) FILTER (WHERE metric='building_total')::float        AS total,
                SUM(energy_kwh) FILTER (WHERE metric='building_from_solar')::float   AS from_solar,
                SUM(energy_kwh) FILTER (WHERE metric='building_from_grid')::float    AS from_grid
            FROM panel_readings
            WHERE metric IN ('building_total', 'building_from_solar', 'building_from_grid')
            GROUP BY 1 ORDER BY 1;
            """
        )
        out = []
        for r in cur.fetchall():
            total = r['total'] or 0.0
            from_solar = r['from_solar'] or 0.0
            from_grid = r['from_grid'] or 0.0
            out.append({
                'month':      r['month'],
                'total':      total,
                'from_solar': from_solar,
                'from_grid':  from_grid,
                'solar_pct':  (from_solar / total * 100) if total else 0.0,
            })
        return out


def panel_daily_local(start: datetime.date, end: datetime.date) -> list[dict]:
    """Panel totals per local day (config TZ), start..end inclusive.

    One row per day in the range, including days with no readings
    (coverage 0, kWh null). Drives the stable /api/v1/panel/daily contract,
    so the shape and meaning of these fields must not change in v1.

    - import_kwh  = grid_consuming (from the utility)
    - export_kwh  = solar_to_grid  (to the utility)
    - solar_kwh   = solar          (produced)
    - load_kwh    = building_total (consumed by the house)
      load = import - export + solar holds to float rounding (no battery).
    - coverage    = seconds of the local day covered by load readings / the
      day's length (23 or 25 h on DST changes). Under 1 means a partial
      day: today, or a gap in SPAN's cloud.
    - resolution_s = widest bucket that day: 600, 3600 or 86400, after
      SPAN's retention tiers. Days only covered by SPAN's monthly buckets
      (before 2024-05) have no daily figure, so coverage 0.

    Built from panel_readings, not the panel_daily rollup: panel_daily is
    bucketed by UTC day, which only matches a local day where readings
    are daily buckets stamped at local midnight. solar/export are null
    on days SPAN reported no solar series (before 2024-06); missing zero
    buckets inside a covered day count as 0."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH days AS (
                SELECT d::date AS date,
                       EXTRACT(EPOCH FROM
                           ((d + INTERVAL '1 day')::timestamp AT TIME ZONE '{TZ}')
                         - (d::timestamp AT TIME ZONE '{TZ}'))                  AS day_s
                FROM generate_series(%(start)s::date, %(end)s::date,
                                     INTERVAL '1 day') AS d
            ),
            agg AS (
                SELECT (ts AT TIME ZONE '{TZ}')::date                           AS date,
                       SUM(energy_kwh) FILTER (WHERE metric = 'grid_consuming') AS import_kwh,
                       SUM(energy_kwh) FILTER (WHERE metric = 'solar_to_grid')  AS export_kwh,
                       SUM(energy_kwh) FILTER (WHERE metric = 'solar')          AS solar_kwh,
                       SUM(energy_kwh) FILTER (WHERE metric = 'building_total') AS load_kwh,
                       SUM(duration_s) FILTER (WHERE metric = 'building_total'
                                                 AND duration_s < 86400)        AS sub_s,
                       BOOL_OR(metric = 'building_total' AND duration_s = 86400) AS whole_day,
                       MAX(duration_s)                                          AS resolution_s
                FROM panel_readings
                WHERE ts >= (%(start)s::date::timestamp AT TIME ZONE '{TZ}')
                  AND ts <  ((%(end)s::date + 1)::timestamp AT TIME ZONE '{TZ}')
                  AND duration_s <= 86400
                  AND metric IN ('grid_consuming', 'solar_to_grid', 'solar', 'building_total')
                GROUP BY 1
            )
            SELECT days.date::text                                              AS date,
                   agg.import_kwh::float, agg.export_kwh::float,
                   agg.solar_kwh::float,  agg.load_kwh::float,
                   -- A daily bucket covers its whole local day, 23 or 25 h too.
                   (CASE WHEN agg.whole_day THEN 1.0
                         ELSE COALESCE(agg.sub_s, 0) / days.day_s END)::float   AS coverage,
                   agg.resolution_s
            FROM days LEFT JOIN agg USING (date)
            ORDER BY days.date;
            """,
            {'start': start, 'end': end},
        )
        out = []
        for r in cur.fetchall():
            r = dict(r)
            if r['load_kwh'] is not None and r['solar_kwh'] is not None:
                r['export_kwh'] = r['export_kwh'] or 0.0
            r['coverage'] = round(min(r['coverage'], 1.0), 4)
            out.append(r)
        return out


def solar_overview() -> dict:
    """Headline solar stats: lifetime + this-month + last-month + best day."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                SUM(energy_kwh) FILTER (WHERE metric='solar')::float                AS produced,
                SUM(energy_kwh) FILTER (WHERE metric='building_from_solar')::float  AS consumed,
                SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float        AS exported
            FROM panel_readings
            WHERE metric IN ('solar', 'building_from_solar', 'solar_to_grid');
            """
        )
        lifetime = cur.fetchone() or {}

        # This calendar month + previous calendar month (LOCAL TZ).
        for label, expr in (('this_month', "date_trunc('month', now() AT TIME ZONE %s)"),
                            ('last_month', "date_trunc('month', now() AT TIME ZONE %s) - INTERVAL '1 month'")):
            cur.execute(
                f"""
                SELECT
                    SUM(energy_kwh) FILTER (WHERE metric='solar')::float               AS produced,
                    SUM(energy_kwh) FILTER (WHERE metric='building_from_solar')::float AS consumed,
                    SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float       AS exported
                FROM panel_readings
                WHERE metric IN ('solar', 'building_from_solar', 'solar_to_grid')
                  AND (ts AT TIME ZONE '{TZ}') >= {expr}
                  AND (ts AT TIME ZONE '{TZ}') <  {expr} + INTERVAL '1 month';
                """,
                (TZ, TZ),
            )
            row = cur.fetchone() or {}
            lifetime[label] = {
                'produced': row.get('produced') or 0.0,
                'consumed': row.get('consumed') or 0.0,
                'exported': row.get('exported') or 0.0,
            }

        # Best solar production day across all data.
        cur.execute(
            f"""
            SELECT (ts AT TIME ZONE '{TZ}')::date::text AS date,
                   SUM(energy_kwh)::float                AS produced
            FROM panel_readings
            WHERE metric = 'solar' AND duration_s <= 86400
            GROUP BY 1 ORDER BY produced DESC LIMIT 1;
            """
        )
        best = cur.fetchone() or {}

    produced = lifetime.get('produced') or 0.0
    consumed = lifetime.get('consumed') or 0.0
    exported = lifetime.get('exported') or 0.0
    return {
        'lifetime': {
            'produced': produced,
            'consumed': consumed,
            'exported': exported,
            'self_pct': (consumed / produced * 100) if produced else 0.0,
        },
        'this_month': lifetime.get('this_month', {}),
        'last_month': lifetime.get('last_month', {}),
        'best_day': {'date': best.get('date'), 'produced': best.get('produced') or 0.0},
    }


def solar_monthly_full() -> list[dict]:
    """Monthly: production / consumed by house / exported / from-grid /
    self-consumption %. Combines solar_self_consumption_monthly with the new
    solar_to_grid metric."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                date_trunc('month', ts AT TIME ZONE '{TZ}')::date::text AS month,
                SUM(energy_kwh) FILTER (WHERE metric='solar')::float                  AS produced,
                SUM(energy_kwh) FILTER (WHERE metric='building_from_solar')::float    AS consumed,
                SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float          AS exported,
                SUM(energy_kwh) FILTER (WHERE metric='building_total')::float         AS house_total,
                SUM(energy_kwh) FILTER (WHERE metric='building_from_grid')::float     AS from_grid
            FROM panel_readings
            WHERE metric IN ('solar', 'building_from_solar', 'solar_to_grid',
                             'building_total', 'building_from_grid')
            GROUP BY 1 ORDER BY 1;
            """
        )
        out = []
        for r in cur.fetchall():
            produced = r['produced'] or 0.0
            consumed = r['consumed'] or 0.0
            exported = r['exported'] or 0.0
            house    = r['house_total'] or 0.0
            from_g   = r['from_grid'] or 0.0
            out.append({
                'month':       r['month'],
                'produced':    produced,
                'consumed':    consumed,
                'exported':    exported,
                'house_total': house,
                'from_grid':   from_g,
                'self_pct':    (consumed / produced * 100) if produced else 0.0,
                'offset_pct':  (consumed / house * 100) if house else 0.0,  # how much of house consumption solar covered
            })
        return out


def solar_yoy_monthly() -> list[dict]:
    """Solar production rolled to (year, month) for the multi-year overlay
    chart on the solar dashboard."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                EXTRACT(YEAR  FROM ts AT TIME ZONE '{TZ}')::int AS year,
                EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}')::int AS month,
                SUM(energy_kwh)::float                            AS produced
            FROM panel_readings
            WHERE metric = 'solar'
            GROUP BY 1, 2 ORDER BY 1, 2;
            """
        )
        return [dict(r) for r in cur.fetchall()]


def solar_daily_for_year(year: int) -> list[dict]:
    """Daily solar production for a calendar year — for the production heatmap."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT (ts AT TIME ZONE '{TZ}')::date::text AS date,
                   SUM(energy_kwh)::float                AS kwh
            FROM panel_readings
            WHERE metric = 'solar'
              AND ts >= make_timestamptz(%s, 1, 1, 0, 0, 0, %s)
              AND ts <  make_timestamptz(%s, 1, 1, 0, 0, 0, %s)
            GROUP BY 1 ORDER BY 1;
            """,
            (year, TZ, year + 1, TZ),
        )
        return [dict(r) for r in cur.fetchall()]


def best_solar_days(window: str = '90d', limit: int = 15) -> list[dict]:
    interval_map = {
        '30d':  "INTERVAL '30 days'",
        '90d':  "INTERVAL '90 days'",
        '12mo': "INTERVAL '12 months'",
    }
    where_window = ''
    if window in interval_map:
        where_window = f"AND ts >= NOW() - {interval_map[window]}"
    elif window != 'all':
        raise ValueError(f'unknown window: {window}')

    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT (ts AT TIME ZONE '{TZ}')::date::text   AS date,
                   SUM(energy_kwh) FILTER (WHERE metric='solar')::float          AS produced,
                   SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float  AS exported,
                   SUM(energy_kwh) FILTER (WHERE metric='building_from_solar')::float AS consumed
            FROM panel_readings
            WHERE metric IN ('solar', 'solar_to_grid', 'building_from_solar')
              AND duration_s <= 86400
              {where_window}
            GROUP BY 1
            ORDER BY produced DESC NULLS LAST
            LIMIT %s;
            """,
            (limit,),
        )
        return [
            {
                'date': r['date'],
                'produced': r['produced'] or 0.0,
                'exported': r['exported'] or 0.0,
                'consumed': r['consumed'] or 0.0,
            }
            for r in cur.fetchall()
        ]


def solar_hour_of_day(weeks: int = 12) -> list[dict]:
    """Average solar production per hour-of-day. The classic 'duck curve' but
    for one panel — should bell-curve around solar noon."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH hourly AS (
                SELECT
                    EXTRACT(HOUR FROM ts AT TIME ZONE '{TZ}')::int AS hour,
                    (ts AT TIME ZONE '{TZ}')::date                 AS local_date,
                    SUM(energy_kwh)::float                          AS kwh
                FROM panel_readings
                WHERE metric = 'solar'
                  AND duration_s <= 3600
                  AND ts >= NOW() - %s::interval
                GROUP BY 1, 2
            )
            SELECT hour, AVG(kwh)::float AS avg_kwh
            FROM hourly GROUP BY hour ORDER BY hour;
            """,
            (f'{weeks} weeks',),
        )
        return [{'hour': r['hour'], 'avg_kwh': r['avg_kwh'] or 0.0}
                for r in cur.fetchall()]


def anomaly_history(window_days: int = 90, threshold: float = 3.0) -> list[dict]:
    """Find (space, date) pairs where the day's kWh was at least `threshold` ×
    the prior 7-day rolling average for that same space. Surfaces the
    historical version of a 'circuit ran weirdly hot' alarm.

    Day-aligned window: filter on NY-local date boundaries so partial days
    (today, and a partial slice at the start of the window) can't pollute the
    rolling baseline (the retired anomaly alerter used the same
    full-day alignment). Also requires kwh - baseline >= 2 kWh so
    sub-watt circuits ("cooked dinner today") don't trip on ratio alone.

    Returns sorted by ratio descending, limited to the most striking ~30."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            WITH daily AS (
                SELECT space_id,
                       (ts AT TIME ZONE '{TZ}')::date AS date,
                       SUM(energy_kwh)::float          AS kwh
                FROM space_readings
                WHERE duration_s <= 86400
                  AND (ts AT TIME ZONE '{TZ}')::date
                        >= (NOW() AT TIME ZONE '{TZ}')::date - %s::int
                  AND (ts AT TIME ZONE '{TZ}')::date
                        <  (NOW() AT TIME ZONE '{TZ}')::date
                GROUP BY 1, 2
            ),
            with_baseline AS (
                SELECT space_id, date, kwh,
                       AVG(kwh) OVER (PARTITION BY space_id ORDER BY date
                                       RANGE BETWEEN INTERVAL '7 days' PRECEDING
                                                 AND INTERVAL '1 day'  PRECEDING) AS baseline
                FROM daily
            )
            SELECT s.name             AS space_name,
                   wb.space_id        AS space_id,
                   wb.date::text      AS date,
                   wb.kwh             AS kwh,
                   wb.baseline        AS baseline,
                   (wb.kwh / NULLIF(wb.baseline, 0))::float AS ratio
            FROM with_baseline wb
            JOIN spaces s USING (space_id)
            WHERE wb.baseline IS NOT NULL
              AND wb.baseline > 0.5            -- skip noise circuits whose baseline is sub-watt
              AND wb.kwh >= wb.baseline * %s
              AND (wb.kwh - wb.baseline) >= 2.0
            ORDER BY ratio DESC
            LIMIT 30;
            """,
            (window_days, threshold),
        )
        return [dict(r) for r in cur.fetchall()]


def daily_produced_vs_consumed() -> list[dict]:
    """Per-day pairs of (solar produced, total house consumption) — for the
    scatter chart showing where the user lands relative to net-zero."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                (ts AT TIME ZONE '{TZ}')::date::text AS date,
                EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}')::int AS month,
                SUM(energy_kwh) FILTER (WHERE metric='solar')::float           AS produced,
                SUM(energy_kwh) FILTER (WHERE metric='building_total')::float  AS consumed
            FROM panel_readings
            WHERE metric IN ('solar', 'building_total')
              AND duration_s <= 86400
            GROUP BY 1, 2
            HAVING SUM(energy_kwh) FILTER (WHERE metric='solar') > 0
                OR SUM(energy_kwh) FILTER (WHERE metric='building_total') > 0
            ORDER BY 1;
            """
        )
        return [{'date': r['date'], 'month': r['month'],
                 'produced': r['produced'] or 0.0,
                 'consumed': r['consumed'] or 0.0}
                for r in cur.fetchall()]


def capacity_factor() -> dict:
    """Estimate the panel's capacity factor from observed peak power.

    Without a nameplate kW input we infer peak from the highest observed
    instantaneous power (max energy_kwh per 10-min bucket × 6 = kW), then
    compute CF = average production / (peak × hours).
    """
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT MAX(energy_kwh * 3600.0 / duration_s)::float AS peak_kw
            FROM panel_readings
            WHERE metric = 'solar' AND duration_s = 600;
            """
        )
        peak_kw = (cur.fetchone() or {}).get('peak_kw') or 0.0

        cur.execute(
            """
            SELECT SUM(energy_kwh)::float                            AS total_kwh,
                   MIN(ts)                                            AS first_ts,
                   MAX(ts)                                            AS last_ts
            FROM panel_readings
            WHERE metric = 'solar' AND energy_kwh > 0;
            """
        )
        r = cur.fetchone() or {}
        total = r.get('total_kwh') or 0.0
        first_ts = r.get('first_ts')
        last_ts  = r.get('last_ts')

    cf = None
    hours = None
    if peak_kw and first_ts and last_ts:
        hours = (last_ts - first_ts).total_seconds() / 3600
        if hours > 0:
            cf = (total / (peak_kw * hours)) * 100  # percent
    return {
        'peak_kw': peak_kw,
        'total_kwh': total,
        'observation_hours': hours,
        'capacity_factor_pct': cf,
        'first_ts': first_ts.isoformat() if first_ts else None,
        'last_ts': last_ts.isoformat() if last_ts else None,
    }


def same_month_prior_years(date: str) -> list[dict]:
    """For the given date's year+month, return month-to-date totals (whole-house)
    for the same calendar month in each year of available data. Caps prior
    years at the same day-of-month so the comparison is apples-to-apples
    (e.g. May 11 2026 compares to May 1-11 of 2024/2025).
    """
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        return []

    target_month = d.month
    target_day   = d.day

    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT EXTRACT(YEAR FROM ts AT TIME ZONE '{TZ}')::int AS year,
                   SUM(energy_kwh) FILTER (WHERE metric='solar')::float            AS produced,
                   SUM(energy_kwh) FILTER (WHERE metric='building_total')::float   AS consumed,
                   SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float    AS exported,
                   SUM(energy_kwh) FILTER (WHERE metric='building_from_grid')::float AS imported
            FROM panel_readings
            WHERE metric IN ('solar','building_total','solar_to_grid','building_from_grid')
              AND duration_s <= 86400
              AND EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}') = %s
              AND EXTRACT(DAY   FROM ts AT TIME ZONE '{TZ}') <= %s
            GROUP BY 1 ORDER BY 1;
            """,
            (target_month, target_day),
        )
        return [dict(r) for r in cur.fetchall()]


def same_day_prior_years(date: str, circuit_id: str = WHOLE_HOUSE) -> list[dict]:
    """For a given local-time date, return the same calendar day in each
    prior year that has data. Used by the /day view's YoY drill."""
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        return []

    target_month, target_day = d.month, d.day
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if circuit_id == WHOLE_HOUSE:
            cur.execute(
                f"""
                SELECT (ts AT TIME ZONE '{TZ}')::date::text AS date,
                       EXTRACT(YEAR FROM ts AT TIME ZONE '{TZ}')::int AS year,
                       SUM(energy_kwh) FILTER (WHERE metric='solar')::float          AS produced,
                       SUM(energy_kwh) FILTER (WHERE metric='building_total')::float AS consumed,
                       SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float  AS exported
                FROM panel_readings
                WHERE metric IN ('solar', 'building_total', 'solar_to_grid')
                  AND duration_s <= 86400
                  AND EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}') = %s
                  AND EXTRACT(DAY   FROM ts AT TIME ZONE '{TZ}') = %s
                GROUP BY 1, 2 ORDER BY 2;
                """,
                (target_month, target_day),
            )
        else:
            cur.execute(
                f"""
                SELECT (ts AT TIME ZONE '{TZ}')::date::text AS date,
                       EXTRACT(YEAR FROM ts AT TIME ZONE '{TZ}')::int AS year,
                       SUM(energy_kwh)::float AS consumed
                FROM space_readings
                WHERE space_id = %s AND duration_s <= 86400
                  AND EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}') = %s
                  AND EXTRACT(DAY   FROM ts AT TIME ZONE '{TZ}') = %s
                GROUP BY 1, 2 ORDER BY 2;
                """,
                (circuit_id, target_month, target_day),
            )
        return [dict(r) for r in cur.fetchall()]


# Top-N "biggest day" metrics. Per-circuit selection only supports 'consumption'
# since the others are panel-level concepts.
TOP_DAYS_METRICS = {
    'consumption': 'building_total',     # household load
    'production':  'solar',              # gross solar production
    'export':      'solar_to_grid',      # solar pushed back to grid
    'import':      'building_from_grid', # grid kWh actually used by the building
}


def top_days(window: str = '90d', circuit_id: str = WHOLE_HOUSE, limit: int = 25,
             metric: str = 'consumption') -> list[dict]:
    """Top-N days by a chosen metric in a chosen window.

    Returns rows with {date, kwh, dow, day_type}. When metric is 'consumption'
    AND the selection is whole-house, also includes {from_solar, from_grid,
    produced, exported} so the UI can show the day's energy mix alongside
    the headline kWh. Per-circuit selections only support metric='consumption'.
    """
    interval_map = {
        '30d':  "INTERVAL '30 days'",
        '90d':  "INTERVAL '90 days'",
        '12mo': "INTERVAL '12 months'",
    }
    where_window = ''
    if window in interval_map:
        where_window = f"AND ts >= NOW() - {interval_map[window]}"
    elif window != 'all':
        raise ValueError(f'unknown window: {window}')

    if metric not in TOP_DAYS_METRICS:
        raise ValueError(f'unknown metric: {metric}')

    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if circuit_id == WHOLE_HOUSE:
            if metric == 'consumption':
                # Rank by consumption AND surface the day's source mix so the user
                # can see at a glance whether a big day was bill-painful or
                # bill-cheap.
                cur.execute(
                    f"""
                    SELECT (ts AT TIME ZONE '{TZ}')::date::text   AS date,
                           SUM(energy_kwh) FILTER (WHERE metric='building_total')::float      AS kwh,
                           SUM(energy_kwh) FILTER (WHERE metric='building_from_solar')::float AS from_solar,
                           SUM(energy_kwh) FILTER (WHERE metric='building_from_grid')::float  AS from_grid,
                           SUM(energy_kwh) FILTER (WHERE metric='solar')::float               AS produced,
                           SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float       AS exported,
                           EXTRACT(DOW FROM ts AT TIME ZONE '{TZ}')::int AS dow
                    FROM panel_readings
                    WHERE metric IN ('building_total','building_from_solar','building_from_grid',
                                     'solar','solar_to_grid')
                      AND duration_s <= 86400
                      {where_window}
                    GROUP BY 1, 7
                    ORDER BY kwh DESC NULLS LAST
                    LIMIT %s;
                    """,
                    (limit,),
                )
            else:
                target = TOP_DAYS_METRICS[metric]
                cur.execute(
                    f"""
                    SELECT (ts AT TIME ZONE '{TZ}')::date::text   AS date,
                           SUM(energy_kwh)::float                  AS kwh,
                           EXTRACT(DOW FROM ts AT TIME ZONE '{TZ}')::int AS dow
                    FROM panel_readings
                    WHERE metric = %s
                      AND duration_s <= 86400
                      {where_window}
                    GROUP BY 1, 3
                    ORDER BY kwh DESC NULLS LAST
                    LIMIT %s;
                    """,
                    (target, limit),
                )
        else:
            # Per-circuit only supports consumption; production/export/import
            # are panel-level, not circuit-level.
            if metric != 'consumption':
                return []
            cur.execute(
                f"""
                SELECT (ts AT TIME ZONE '{TZ}')::date::text   AS date,
                       SUM(energy_kwh)::float                  AS kwh,
                       EXTRACT(DOW FROM ts AT TIME ZONE '{TZ}')::int AS dow
                FROM space_readings
                WHERE space_id = %s
                  AND duration_s <= 86400
                  {where_window}
                GROUP BY 1, 3
                ORDER BY kwh DESC
                LIMIT %s;
                """,
                (circuit_id, limit),
            )
        rows = cur.fetchall()

    out = []
    for r in rows:
        item = {
            'date': r['date'],
            'kwh': r['kwh'],
            'dow': r['dow'],
            'day_type': 'weekend' if r['dow'] in (0, 6) else 'weekday',
        }
        if 'from_solar' in r:
            item['from_solar'] = r['from_solar']
            item['from_grid']  = r['from_grid']
            item['produced']   = r['produced']
            item['exported']   = r['exported']
        out.append(item)
    return out


def day_curve(date: str, circuit_id: str = WHOLE_HOUSE) -> dict:
    """All readings for one date, ordered by time. Used by the day-drill view.

    Returns {date, circuit_name, granularity, series} where series is a
    dict of metric -> [{ts, kwh, power_kw}]. The `power_kw` is derived from
    duration_s so it's comparable across the 10-min/hourly tiers.
    Granularity is the finest-grain duration_s present that day, useful for
    the chart to label its axis."""
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        raise ValueError(f'invalid date: {date!r}')

    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if circuit_id == WHOLE_HOUSE:
            circuit_name = 'Whole house'
            # Two stories on one date:
            #   Load:  building_total = building_from_solar + building_from_grid
            #   Solar: solar          = building_from_solar + solar_to_grid
            # grid_consuming is omitted — with no battery it equals
            # building_from_grid, so showing both is just chart noise.
            cur.execute(
                f"""
                SELECT ts, metric, energy_kwh::float AS kwh, duration_s
                FROM panel_readings
                WHERE metric IN ('building_total', 'building_from_solar',
                                 'building_from_grid', 'solar', 'solar_to_grid')
                  AND (ts AT TIME ZONE '{TZ}')::date = %s
                ORDER BY ts;
                """,
                (d,),
            )
        else:
            cur.execute("SELECT name FROM spaces WHERE space_id = %s;", (circuit_id,))
            row = cur.fetchone()
            circuit_name = (row or {}).get('name') or circuit_id
            cur.execute(
                f"""
                SELECT ts, 'circuit' AS metric, energy_kwh::float AS kwh, duration_s
                FROM space_readings
                WHERE space_id = %s
                  AND (ts AT TIME ZONE '{TZ}')::date = %s
                ORDER BY ts;
                """,
                (circuit_id, d),
            )
        raw = cur.fetchall()

    series: dict[str, list[dict]] = {}
    granularity = None
    for r in raw:
        m = r['metric']
        if granularity is None or r['duration_s'] < granularity:
            granularity = r['duration_s']
        # ISO-format timestamp for JS, plus computed avg power for the bucket.
        ts_iso = r['ts'].isoformat()
        power_kw = (r['kwh'] * 3600.0 / r['duration_s']) if r['duration_s'] else 0.0
        series.setdefault(m, []).append({
            'ts': ts_iso,
            'kwh': r['kwh'],
            'power_kw': power_kw,
            'duration_s': r['duration_s'],
        })

    return {
        'date': date,
        'circuit_id': circuit_id,
        'circuit_name': circuit_name,
        'granularity': granularity,   # smallest duration_s observed
        'series': series,
    }


def day_per_circuit(date: str) -> dict:
    """For a given local-time date, return per-circuit daily total + bucket-level
    curve. Powers the per-circuit breakdown section on /day's whole-house view."""
    try:
        d = datetime.date.fromisoformat(date)
    except ValueError:
        return {'totals': [], 'rows': [], 'granularity': None}

    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # Daily totals, ranked desc.
        cur.execute(
            f"""
            SELECT s.space_id, s.name,
                   SUM(sr.energy_kwh)::float AS kwh
            FROM space_readings sr JOIN spaces s USING (space_id)
            WHERE (sr.ts AT TIME ZONE '{TZ}')::date = %s
            GROUP BY s.space_id, s.name
            HAVING SUM(sr.energy_kwh) > 0
            ORDER BY kwh DESC;
            """,
            (d,),
        )
        totals = [{'space_id': r['space_id'], 'name': r['name'], 'kwh': r['kwh']}
                  for r in cur.fetchall()]

        # Bucket-level rows for the stacked-area chart.
        cur.execute(
            f"""
            SELECT sr.ts, sr.space_id, s.name,
                   sr.energy_kwh::float, sr.duration_s
            FROM space_readings sr JOIN spaces s USING (space_id)
            WHERE (sr.ts AT TIME ZONE '{TZ}')::date = %s
            ORDER BY sr.ts;
            """,
            (d,),
        )
        rows = []
        granularity = None
        for r in cur.fetchall():
            if granularity is None or r['duration_s'] < granularity:
                granularity = r['duration_s']
            rows.append({
                'ts': r['ts'].isoformat(),
                'space_id': r['space_id'],
                'name': r['name'],
                'kwh': r['energy_kwh'],
                'duration_s': r['duration_s'],
            })

    return {'totals': totals, 'rows': rows, 'granularity': granularity}


def hourly_pattern(circuit_id: str = WHOLE_HOUSE, window_weeks: int = 12) -> list[dict]:
    """Average kWh per hour-of-day, split by weekday vs weekend.

    Only uses rows at hourly resolution or finer (duration_s <= 3600) so the
    answer reflects actual sub-day load shape, not daily-aggregate smearing.

    Returns [{hour: 0..23, weekday_kwh: float, weekend_kwh: float}]."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        if circuit_id == WHOLE_HOUSE:
            cur.execute(
                f"""
                WITH hourly AS (
                    SELECT
                        EXTRACT(HOUR FROM ts AT TIME ZONE '{TZ}')::int       AS hour,
                        EXTRACT(DOW  FROM ts AT TIME ZONE '{TZ}')::int       AS dow,
                        (ts AT TIME ZONE '{TZ}')::date                       AS local_date,
                        SUM(energy_kwh)::float                               AS kwh
                    FROM panel_readings
                    WHERE metric = 'building_total'
                      AND duration_s <= 3600
                      AND ts >= NOW() - %s::interval
                    GROUP BY 1, 2, 3
                )
                SELECT hour,
                       AVG(kwh) FILTER (WHERE dow BETWEEN 1 AND 5)::float AS weekday_kwh,
                       AVG(kwh) FILTER (WHERE dow IN (0, 6))::float       AS weekend_kwh
                FROM hourly
                GROUP BY hour
                ORDER BY hour;
                """,
                (f'{window_weeks} weeks',),
            )
        else:
            cur.execute(
                f"""
                WITH hourly AS (
                    SELECT
                        EXTRACT(HOUR FROM ts AT TIME ZONE '{TZ}')::int       AS hour,
                        EXTRACT(DOW  FROM ts AT TIME ZONE '{TZ}')::int       AS dow,
                        (ts AT TIME ZONE '{TZ}')::date                       AS local_date,
                        SUM(energy_kwh)::float                               AS kwh
                    FROM space_readings
                    WHERE space_id = %s
                      AND duration_s <= 3600
                      AND ts >= NOW() - %s::interval
                    GROUP BY 1, 2, 3
                )
                SELECT hour,
                       AVG(kwh) FILTER (WHERE dow BETWEEN 1 AND 5)::float AS weekday_kwh,
                       AVG(kwh) FILTER (WHERE dow IN (0, 6))::float       AS weekend_kwh
                FROM hourly
                GROUP BY hour
                ORDER BY hour;
                """,
                (circuit_id, f'{window_weeks} weeks'),
            )
        return [
            {
                'hour': r['hour'],
                'weekday_kwh': r['weekday_kwh'] or 0.0,
                'weekend_kwh': r['weekend_kwh'] or 0.0,
            }
            for r in cur.fetchall()
        ]


def energy_flows(window: str = '12mo') -> dict:
    """Aggregate energy flows for the Sankey diagram in the chosen window."""
    interval = {'30d': "30 days", '90d': "90 days", '12mo': "365 days"}.get(window)
    where = f"AND ts >= NOW() - INTERVAL '{interval}'" if interval else ''
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            f"""
            SELECT
                SUM(energy_kwh) FILTER (WHERE metric='solar')::float                AS produced,
                SUM(energy_kwh) FILTER (WHERE metric='building_from_solar')::float  AS from_solar,
                SUM(energy_kwh) FILTER (WHERE metric='solar_to_grid')::float        AS to_grid,
                SUM(energy_kwh) FILTER (WHERE metric='building_from_grid')::float   AS from_grid,
                SUM(energy_kwh) FILTER (WHERE metric='building_total')::float       AS consumed
            FROM panel_readings
            WHERE metric IN ('solar','building_from_solar','solar_to_grid','building_from_grid','building_total')
              AND duration_s <= 86400 {where};
            """
        )
        r = dict(cur.fetchone() or {})
    return {
        'window':    window,
        'produced':  r.get('produced')  or 0.0,
        'from_solar': r.get('from_solar') or 0.0,
        'to_grid':   r.get('to_grid')   or 0.0,
        'from_grid': r.get('from_grid') or 0.0,
        'consumed':  r.get('consumed')  or 0.0,
    }


def solar_vs_consumption_hourly(weeks: int = 12) -> list[dict]:
    """Average kWh per hour-of-day for solar production AND whole-house
    consumption. Drives the time-of-day mismatch overlay."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            WITH hourly AS (
                SELECT
                    EXTRACT(HOUR FROM ts AT TIME ZONE '{TZ}')::int AS hour,
                    (ts AT TIME ZONE '{TZ}')::date                  AS date,
                    SUM(energy_kwh) FILTER (WHERE metric='solar')::float           AS solar,
                    SUM(energy_kwh) FILTER (WHERE metric='building_total')::float  AS consumed
                FROM panel_readings
                WHERE metric IN ('solar', 'building_total')
                  AND duration_s <= 3600
                  AND ts >= NOW() - %s::interval
                GROUP BY 1, 2
            )
            SELECT hour,
                   AVG(solar)::float    AS solar_avg,
                   AVG(consumed)::float AS consumed_avg
            FROM hourly GROUP BY hour ORDER BY hour;
            """,
            (f'{weeks} weeks',),
        )
        return [{'hour': r[0], 'solar': r[1] or 0.0, 'consumed': r[2] or 0.0}
                for r in cur.fetchall()]


def phantom_load_daily(days: int = 90) -> list[dict]:
    """Per-day minimum power draw — the 10-min bucket low-water-mark, converted
    to kW. Tracks the 'what's on when nothing's on' baseline over time."""
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT (ts AT TIME ZONE '{TZ}')::date::text AS date,
                   MIN(energy_kwh * 3600.0 / duration_s)::float AS min_power_kw
            FROM panel_readings
            WHERE metric = 'building_total'
              AND duration_s = 600
              AND ts >= NOW() - %s::interval
            GROUP BY 1 ORDER BY 1;
            """,
            (f'{days} days',),
        )
        return [{'date': r[0], 'min_power_kw': r[1]} for r in cur.fetchall()]


def year_end_production_forecast() -> dict:
    """Project full-year solar kWh from YTD + the prior-year monthly profile."""
    import calendar
    today = today_local()

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT SUM(energy_kwh)::float
            FROM panel_readings
            WHERE metric = 'solar'
              AND duration_s <= 86400
              AND EXTRACT(YEAR FROM ts AT TIME ZONE '{TZ}') = %s;
            """,
            (today.year,),
        )
        ytd = (cur.fetchone() or [0])[0] or 0.0

        # Average monthly production from prior years, used to project
        # what each remaining month will produce.
        cur.execute(
            f"""
            SELECT m.month,
                   AVG(m.monthly_kwh)::float AS avg_kwh
            FROM (
                SELECT EXTRACT(YEAR  FROM ts AT TIME ZONE '{TZ}')::int AS year,
                       EXTRACT(MONTH FROM ts AT TIME ZONE '{TZ}')::int AS month,
                       SUM(energy_kwh)::float AS monthly_kwh
                FROM panel_readings
                WHERE metric='solar' AND duration_s <= 86400
                  AND EXTRACT(YEAR FROM ts AT TIME ZONE '{TZ}') < %s
                GROUP BY 1, 2
                HAVING SUM(energy_kwh) > 50
            ) m
            GROUP BY m.month ORDER BY m.month;
            """,
            (today.year,),
        )
        monthly_avg = {int(r[0]): r[1] for r in cur.fetchall()}

        cur.execute(
            f"""
            SELECT EXTRACT(YEAR FROM ts AT TIME ZONE '{TZ}')::int AS year,
                   SUM(energy_kwh)::float AS total
            FROM panel_readings WHERE metric='solar' AND duration_s <= 86400
            GROUP BY 1 ORDER BY 1;
            """
        )
        prior_totals = {int(r[0]): r[1] for r in cur.fetchall()}

    # Project: YTD + remainder of this month at its historical pace + remaining months at their averages
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    progress = today.day / days_in_month
    this_month_avg = monthly_avg.get(today.month, 0.0)
    # Assume the month average is reached linearly across the month.
    cur_month_remaining = this_month_avg * (1 - progress)
    later_months_kwh = sum(monthly_avg.get(m, 0.0) for m in range(today.month + 1, 13))
    projected_total = ytd + cur_month_remaining + later_months_kwh

    return {
        'year':             today.year,
        'today':            today.isoformat(),
        'ytd_kwh':          ytd,
        'projected_total':  projected_total,
        'prior_totals':     prior_totals,
        'monthly_avg':      monthly_avg,
    }


# Appliance signatures for /appliances, from circuits.yaml's `appliances`
# (see circuits.example.yaml for every field). Keys are stable identifiers
# used in URLs and CSV exports. Each profile names its circuit by `space_id`
# (stable across renames in the SPAN app); `name_match` is the exact,
# case-insensitive fallback for a circuit whose id ever changes. Glyphs pair
# with colours so the page is readable in grayscale / for colour-blind users.
APPLIANCE_PROFILES: list[dict] = config.circuits()['appliances']


def _appliance_space(cur, profile: dict) -> dict | None:
    """The profile's circuit: by space_id, else by exact name."""
    cur.execute(
        """
        SELECT space_id, name FROM spaces
        WHERE space_id = %s OR name ILIKE %s
        ORDER BY (space_id = %s) DESC, name
        LIMIT 1;
        """,
        (profile.get('space_id'), profile['name_match'], profile.get('space_id')),
    )
    return cur.fetchone()


def _appliance_profile(key: str) -> dict | None:
    for p in APPLIANCE_PROFILES:
        if p['key'] == key:
            return p
    return None


def appliance_sessions(profile_key: str, days: int = 90) -> list[dict]:
    """Detect on/off sessions for an appliance defined in APPLIANCE_PROFILES.
    Generalises ev_charging_sessions to any circuit + threshold pair.

    Profiles with `secondary_kw` (e.g. the heat-pump water heater's resistive
    backup) get an extra per-session breakdown: how many buckets and how many
    kWh inside the session were at or above that secondary threshold."""
    profile = _appliance_profile(profile_key)
    if profile is None:
        return []
    threshold_kw = profile['threshold_kw']
    min_kwh      = profile['min_kwh']
    secondary_kw = profile.get('secondary_kw')
    since_date   = profile.get('since_date')
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cand = _appliance_space(cur, profile)
        if not cand:
            return []
        space_id   = cand['space_id']
        space_name = cand['name']

        # Same sessionizer shape as ev_charging_sessions: contiguous active
        # buckets become one session. Sub-threshold buckets break the run.
        # `secondary_kw` is a within-session classifier — buckets >= it are
        # tallied separately so the UI can show "this session used X kWh of
        # resistive backup" without breaking the session apart.
        sec_kw = float(secondary_kw) if secondary_kw is not None else None
        cur.execute(
            """
            WITH buckets AS (
                SELECT ts, energy_kwh::float AS kwh,
                       (energy_kwh * 3600.0 / duration_s)::float AS power_kw
                FROM space_readings
                WHERE space_id = %s
                  AND duration_s = 600
                  AND ts >= GREATEST(
                      NOW() - %s::interval,
                      COALESCE(%s::timestamptz, '-infinity'::timestamptz)
                  )
                ORDER BY ts
            ),
            flagged AS (
                SELECT ts, kwh, power_kw,
                       CASE WHEN power_kw >= %s THEN 1 ELSE 0 END AS active,
                       CASE WHEN %s::float8 IS NOT NULL AND power_kw >= %s::float8
                            THEN kwh ELSE 0 END AS secondary_kwh,
                       CASE WHEN %s::float8 IS NOT NULL AND power_kw >= %s::float8
                            THEN 1 ELSE 0 END AS secondary_active
                FROM buckets
            ),
            grouped AS (
                SELECT ts, kwh, power_kw, active, secondary_kwh, secondary_active,
                       SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END)
                         OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING) AS group_id
                FROM flagged
            )
            SELECT MIN(ts) AS start_ts,
                   MAX(ts) AS end_ts,
                   SUM(kwh)::float AS kwh,
                   AVG(power_kw)::float AS avg_kw,
                   MAX(power_kw)::float AS peak_kw,
                   COUNT(*) AS buckets,
                   SUM(secondary_kwh)::float AS secondary_kwh,
                   SUM(secondary_active) AS secondary_buckets
            FROM grouped
            WHERE active = 1
            GROUP BY group_id
            HAVING SUM(kwh) >= %s
            ORDER BY start_ts DESC;
            """,
            (space_id, f'{days} days', since_date, threshold_kw,
             sec_kw, sec_kw, sec_kw, sec_kw, min_kwh),
        )
        rows = cur.fetchall()
    sessions = []
    for r in rows:
        # +10 min because end_ts is the last bucket's start, not its end
        duration_min = ((r['end_ts'] - r['start_ts']).total_seconds() / 60) + 10
        session = {
            'appliance':    profile['key'],
            'label':        profile['label'],
            'glyph':        profile['glyph'],
            'color':        profile['color'],
            'start':        r['start_ts'].isoformat(),
            'end':          r['end_ts'].isoformat(),
            'date':         r['start_ts'].date().isoformat(),
            'duration_min': round(duration_min, 1),
            'kwh':          r['kwh'],
            'avg_kw':       r['avg_kw'],
            'peak_kw':      r['peak_kw'],
            'space_id':     space_id,
            'space_name':   space_name,
        }
        if secondary_kw is not None:
            sec_kwh = r['secondary_kwh'] or 0.0
            sec_min = (r['secondary_buckets'] or 0) * 10  # one 10-min bucket each
            session['secondary_label'] = profile.get('secondary_label', 'high power')
            session['secondary_kwh']   = sec_kwh
            session['secondary_min']   = sec_min
            session['secondary_share'] = (sec_kwh / r['kwh']) if r['kwh'] else 0.0
        sessions.append(session)
    return sessions


def appliance_daily_stats(profile_key: str, days: int = 30) -> dict | None:
    """Daily aggregates for cyclical appliances (fridge, mini split) where a
    per-cycle session list is too noisy to read. Returns daily kWh, cycle
    counts (buckets above threshold), duty-cycle %, and rolling 7-day kWh."""
    profile = _appliance_profile(profile_key)
    if profile is None:
        return None
    threshold_kw = profile['threshold_kw']
    since_date = profile.get('since_date')
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cand = _appliance_space(cur, profile)
        if not cand:
            return None
        space_id = cand['space_id']

        cur.execute(
            f"""
            WITH buckets AS (
                SELECT
                    (ts AT TIME ZONE '{TZ}')::date AS day,
                    energy_kwh::float AS kwh,
                    (energy_kwh * 3600.0 / duration_s)::float AS power_kw
                FROM space_readings
                WHERE space_id = %s
                  AND duration_s = 600
                  AND ts >= GREATEST(
                      NOW() - %s::interval,
                      COALESCE(%s::timestamptz, '-infinity'::timestamptz)
                  )
            )
            SELECT day,
                   SUM(kwh)::float                                  AS day_kwh,
                   COUNT(*) FILTER (WHERE power_kw >= %s)::int      AS active_buckets,
                   COUNT(*)::int                                    AS total_buckets,
                   MAX(power_kw)::float                              AS peak_kw,
                   AVG(power_kw)::float                              AS avg_kw
            FROM buckets
            GROUP BY day
            ORDER BY day;
            """,
            (space_id, f'{days} days', since_date, threshold_kw),
        )
        rows = cur.fetchall()
    if not rows:
        return None
    daily = []
    for r in rows:
        active = r['active_buckets'] or 0
        total  = r['total_buckets'] or 1
        daily.append({
            'date':         r['day'].isoformat(),
            'kwh':          r['day_kwh'] or 0.0,
            'active_min':   active * 10,  # one 10-min bucket each
            'duty_pct':     (active / total * 100.0) if total else 0.0,
            'peak_kw':      r['peak_kw'] or 0.0,
            'avg_kw':       r['avg_kw'] or 0.0,
        })
    total_kwh   = sum(d['kwh'] for d in daily)
    n           = len(daily)
    avg_kwh_day = total_kwh / n if n else 0.0
    avg_duty    = sum(d['duty_pct'] for d in daily) / n if n else 0.0
    avg_active  = sum(d['active_min'] for d in daily) / n if n else 0.0
    return {
        'days':        n,
        'total_kwh':   total_kwh,
        'avg_kwh_day': avg_kwh_day,
        'avg_duty_pct': avg_duty,
        'avg_active_min_day': avg_active,
        'since_date':  since_date,
        'daily':       daily,
    }


def list_live_circuits() -> list[dict]:
    """Circuits that actually have data in `space_live` — the 3-s NILM stream.

    Different from list_circuits() in two ways:
      1. Excludes spaces that the SPAN local API doesn't expose at the
         circuit level (the Sub-panel real circuit is downstream of the
         feedthrough lugs; SPAN only meters the lug aggregate, not the
         individual sub-panel circuits).
      2. Adds a synthetic '__feedthrough__' entry — the sub-panel
         aggregate, which IS in space_live but not in the spaces table.

    Used by the /attribute and /discover/cluster pickers so users can't
    pick a circuit that will return zero samples."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # Only spaces with at least one row in space_live in the last day —
        # both confirms the writer is healthy and excludes Sub-panel.
        cur.execute(
            """
            SELECT s.space_id, s.name
            FROM spaces s
            WHERE EXISTS (
                SELECT 1 FROM space_live sl
                WHERE sl.space_id = s.space_id
                  AND sl.ts >= NOW() - INTERVAL '1 day'
            )
            ORDER BY s.name;
            """
        )
        rows = [dict(r) for r in cur.fetchall()]
        # Add the synthetic feedthrough row if it has data.
        cur.execute(
            "SELECT 1 FROM space_live WHERE space_id = '__feedthrough__' "
            "AND ts >= NOW() - INTERVAL '1 day' LIMIT 1;"
        )
        if cur.fetchone():
            rows.append({
                'space_id': '__feedthrough__',
                'name':     config.circuits()['feedthrough_name'],
            })
    return rows


# ---- NILM: load discovery from 3-s per-circuit data ------------------------
#
# space_live (db/schema/012-space-live.sql) captures per-circuit power every
# 3 s. With that resolution, on/off edges of distinctive loads (window ACs,
# microwaves, kettles, sump pumps, etc.) are recoverable even on mixed
# outlet circuits.
#
# The first-pass detector here is intentionally simple:
#   1. For each circuit, find its 24-h power baseline (10th percentile).
#   2. Treat samples > baseline + min_step_w as "active".
#   3. Group contiguous active samples into "events" — each event is one
#      load turning on, staying on, and turning off.
#   4. Cluster events by amplitude (50 W bins) to surface repeating
#      signatures — e.g. "23 events at ~850 W, ~12 min each" → almost
#      certainly a window AC.
#
# We deliberately stop short of full NILM (HMMs, NN-based disaggregators).
# Step 1-3 already finds 80-90% of distinctive loads on mixed circuits;
# step 4's clustering is the human-readable layer. Going further (e.g.
# co-occurring multi-appliance disaggregation) is research-grade and not
# needed until the simple detector hits a wall.

def _discovered_load_query_for(space_id: str, days: int,
                                min_step_w: float = 200.0) -> str:
    """Build the edge-detection SQL for one circuit. Returned as a string
    because we substitute the interval inline (no user input)."""
    # SQL note: we compute the baseline as the 10th percentile of all
    # samples in the window. For a circuit with a window AC, that's the
    # "AC off" baseline (mostly always-on outlet loads). The detection
    # threshold = baseline + min_step_w bumps us cleanly above noise.
    return f"""
        WITH samples AS (
            SELECT ts, power_w
            FROM space_live
            WHERE space_id = %s
              AND ts >= NOW() - INTERVAL '{days} days'
              AND power_w IS NOT NULL
        ),
        baseline AS (
            SELECT percentile_cont(0.10) WITHIN GROUP (ORDER BY power_w) AS base_w
            FROM samples
        ),
        flagged AS (
            SELECT s.ts, s.power_w,
                   (SELECT base_w FROM baseline) AS base_w,
                   CASE WHEN s.power_w >=
                        (SELECT base_w FROM baseline) + %s
                        THEN 1 ELSE 0 END AS active
            FROM samples s
        ),
        grouped AS (
            SELECT ts, power_w, base_w, active,
                   SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END)
                     OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING) AS run_id
            FROM flagged
        )
        SELECT MIN(ts) AS start_ts,
               MAX(ts) AS end_ts,
               AVG(power_w)::float AS avg_w,
               MAX(power_w)::float AS peak_w,
               MIN(power_w)::float AS min_w,
               (AVG(power_w) - MAX(base_w))::float AS step_w,
               COUNT(*) AS samples,
               EXTRACT(EPOCH FROM (MAX(ts) - MIN(ts) + INTERVAL '3 seconds'))::int AS duration_s
        FROM grouped
        WHERE active = 1
        GROUP BY run_id
        HAVING COUNT(*) >= 5      -- at least 15 s of sustained draw
        ORDER BY start_ts DESC;
    """


def discovered_loads_for_circuit(space_id: str, days: int = 7,
                                  min_step_w: float = 200.0) -> dict:
    """Find load events + cluster them by amplitude for a single circuit.

    Returns {space_name, space_id, baseline_w, sample_count, events: [...],
             clusters: [{amplitude_bin_w, count, ...}], window_days}.
    Each event = one load turning on, staying on, turning off."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT name FROM spaces WHERE space_id = %s;",
            (space_id,),
        )
        sp = cur.fetchone()
        if sp:
            space_name = sp['name']
        elif space_id == '__feedthrough__':
            space_name = config.circuits()['feedthrough_name']
        else:
            space_name = space_id

        cur.execute(
            "SELECT COUNT(*) AS n, "
            "       percentile_cont(0.10) WITHIN GROUP (ORDER BY power_w)::float AS base_w, "
            "       percentile_cont(0.50) WITHIN GROUP (ORDER BY power_w)::float AS median_w, "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY power_w)::float AS p95_w, "
            "       MAX(power_w)::float AS peak_w "
            "FROM space_live WHERE space_id = %s AND ts >= NOW() - %s::interval;",
            (space_id, f'{days} days'),
        )
        stats = cur.fetchone() or {}

        events: list[dict] = []
        if stats.get('n', 0) > 0:
            cur.execute(_discovered_load_query_for(space_id, days, min_step_w),
                        (space_id, min_step_w))
            for r in cur.fetchall():
                duration_s = r['duration_s'] or 0
                avg_w = r['avg_w'] or 0.0
                step_w = r['step_w'] or 0.0
                events.append({
                    'start':      r['start_ts'].isoformat(),
                    'end':        r['end_ts'].isoformat(),
                    'date':       r['start_ts'].date().isoformat(),
                    'duration_s': duration_s,
                    'duration_min': round(duration_s / 60.0, 1),
                    'avg_w':      avg_w,
                    'peak_w':     r['peak_w'] or 0.0,
                    'step_w':     step_w,
                    'samples':    r['samples'],
                    'kwh':        (avg_w * duration_s) / 3_600_000.0,
                })

    # Cluster events by amplitude (50 W bins) so repeating loads surface.
    # Each cluster is further split by duration if it's clearly bimodal
    # (microwave at 2 min vs air fryer at 12 min, both ~1200 W).
    raw_clusters: dict[int, list[dict]] = {}
    for e in events:
        bin_w = int(round(e['avg_w'] / 50.0)) * 50
        raw_clusters.setdefault(bin_w, []).append(e)
    cluster_rows = []
    for bin_w, evs in raw_clusters.items():
        # Phase 1.5.2: split by duration if clearly bimodal.
        sub_groups = _split_by_duration(evs)
        for sub_evs, sub_label in sub_groups:
            cluster_rows.append(_summarize_cluster(
                bin_w, sub_evs, sub_label, days))
    cluster_rows.sort(key=lambda c: c['total_kwh'], reverse=True)

    return {
        'space_id':     space_id,
        'space_name':   space_name,
        'window_days':  days,
        'min_step_w':   min_step_w,
        'sample_count': stats.get('n', 0) or 0,
        'baseline_w':   stats.get('base_w'),
        'median_w':     stats.get('median_w'),
        'p95_w':        stats.get('p95_w'),
        'peak_w':       stats.get('peak_w'),
        'events':       events,
        'clusters':     cluster_rows,
    }


def _split_by_duration(events: list[dict]) -> list[tuple[list[dict], str]]:
    """Phase 1.5.2 — split a single amplitude cluster on duration if bimodal.

    Sorts durations, finds the largest gap, splits there if:
      - both sub-clusters have ≥ 3 events
      - max gap is ≥ 2× the median gap
      - max gap is ≥ 30 % of the duration range
    Otherwise returns the single cluster unchanged. Returns a list of
    (events, label) tuples; label is '' for the unsplit case and 'short'/
    'long' for split clusters."""
    n = len(events)
    if n < 6:
        return [(events, '')]
    sorted_by_dur = sorted(events, key=lambda e: e['duration_min'])
    durs = [e['duration_min'] for e in sorted_by_dur]
    gaps = [durs[i+1] - durs[i] for i in range(n - 1)]
    max_gap = max(gaps)
    max_idx = gaps.index(max_gap)
    sorted_gaps = sorted(gaps)
    median_gap = sorted_gaps[len(sorted_gaps) // 2] or 0.001
    dur_range = durs[-1] - durs[0]
    if dur_range <= 0:
        return [(events, '')]
    if (max_gap < 2 * median_gap) or (max_gap < 0.30 * dur_range):
        return [(events, '')]
    low = sorted_by_dur[:max_idx + 1]
    high = sorted_by_dur[max_idx + 1:]
    if len(low) < 3 or len(high) < 3:
        return [(events, '')]
    return [(low, 'short'), (high, 'long')]


def _summarize_cluster(bin_w: int, evs: list[dict], dur_label: str,
                        days: int) -> dict:
    """Build the cluster summary row including the Phase 1.5.1
    time-of-day distribution."""
    total_duration_s = sum(e['duration_s'] for e in evs)
    total_kwh = sum(e['kwh'] for e in evs)
    durations_min = sorted(e['duration_min'] for e in evs)
    med_dur = durations_min[len(durations_min) // 2] if durations_min else 0
    # Hour-of-day distribution (local time). Stored as 24-int list so the
    # UI can sparkline it without further computation.
    hour_dist = [0] * 24
    for e in evs:
        # e['start'] is a UTC ISO string like '2026-05-18T03:42:11+00:00'.
        # Convert to local hour by parsing the time then applying the same
        # zoneinfo TZ the rest of the app uses.
        try:
            ts = datetime.datetime.fromisoformat(e['start'])
            local_h = ts.astimezone(ZoneInfo(TZ)).hour
            hour_dist[local_h] += 1
        except (ValueError, TypeError):
            pass
    peak_hour = hour_dist.index(max(hour_dist)) if any(hour_dist) else None
    return {
        'amplitude_bin_w':   bin_w,
        'duration_label':    dur_label,                # 'short' / 'long' / ''
        'count':             len(evs),
        'total_min':         total_duration_s / 60.0,
        'total_kwh':         total_kwh,
        'median_duration_min': med_dur,
        'avg_amplitude_w':   sum(e['avg_w'] for e in evs) / len(evs),
        'peak_amplitude_w':  max(e['peak_w'] for e in evs),
        'first_seen':        min(e['start'] for e in evs),
        'last_seen':         max(e['start'] for e in evs),
        'hour_distribution': hour_dist,
        'peak_hour':         peak_hour,
        'hypothesis':        _classify_load(
                                  bin_w, durations_min, len(evs), days,
                                  hour_dist=hour_dist, peak_hour=peak_hour,
                                  duration_label=dur_label),
    }


def _classify_load(amplitude_w: int, durations_min: list[float],
                    count: int, days: int,
                    hour_dist: list[int] | None = None,
                    peak_hour: int | None = None,
                    duration_label: str = '') -> str:
    """Heuristic best-guess at what kind of load a cluster represents.
    Combines amplitude + median duration + (Phase 1.5.1) time-of-day +
    (Phase 1.5.2) bimodal-split label. Pure folklore — typical power
    signatures of household appliances. A starting hypothesis, not truth."""
    if not durations_min:
        return 'unknown'
    n = len(durations_min)
    med_dur = durations_min[n // 2]
    per_day = count / days if days else 0
    # Time-of-day hints help disambiguate similar-amplitude loads.
    morning = sum(hour_dist[6:10])  if hour_dist else 0
    evening = sum(hour_dist[17:22]) if hour_dist else 0
    overnight = sum(hour_dist[0:6]) if hour_dist else 0
    daytime_share = (sum(hour_dist[10:18]) / max(sum(hour_dist), 1)) if hour_dist else 0.0

    if amplitude_w < 100:
        return 'phantom load (always-on)'
    # Window AC: thermostat-cycling, mid amplitude, sustained duration.
    if 600 <= amplitude_w <= 1400 and 5 <= med_dur <= 25 and per_day >= 1:
        return 'window AC (thermostat cycling)'
    # 1000-1500 W band: microwave (short), kettle (short, often AM),
    # toaster (very short), air fryer (long).
    if 800 <= amplitude_w <= 1600:
        if duration_label == 'long' or med_dur >= 6:
            return 'air fryer / slow cooker / electric pan'
        if med_dur < 2:
            if peak_hour is not None and 6 <= peak_hour <= 9 and morning > evening:
                return 'kettle / coffee maker (morning burst)'
            if peak_hour is not None and 7 <= peak_hour <= 9:
                return 'toaster / kettle (breakfast)'
            return 'microwave / kettle / toaster (short burst)'
        if med_dur < 5:
            return 'microwave (medium reheat)'
    if 1200 <= amplitude_w <= 1800 and 30 <= med_dur <= 60:
        return 'dishwasher heating element'
    if 100 <= amplitude_w <= 300 and med_dur < 5:
        return 'fan / small motor (kicked on briefly)'
    if 300 <= amplitude_w <= 600 and med_dur >= 15:
        # Could be fridge OR small mini-split. Time-of-day helps:
        # fridge cycles all day; mini-split usually daytime cooling.
        if daytime_share > 0.8:
            return 'small heat pump / mini-split cycle'
        return 'fridge compressor or similar cycle'
    if amplitude_w >= 3000:
        return 'high-draw appliance (oven / dryer / EVSE)'
    if overnight > (morning + evening) * 0.6:
        return f'overnight-heavy load (~{amplitude_w} W) — check pumps / chargers'
    return f'unclassified ({amplitude_w} W, {med_dur:.1f} min typical)'


def detect_thermostat_cycling(space_id: str, days: int = 7,
                                min_step_w: float = 200.0) -> list[dict]:
    """Phase 1.5.3 — find thermostat-cycling patterns on a circuit.

    Window ACs (and refrigerators, mini-splits, well pumps) exhibit a
    very specific signature: a similar-amplitude load that switches on
    and off at regular intervals for hours at a stretch. This detector
    walks the event list for a circuit and groups consecutive events
    into "cycling runs" where:

      - ≥ 4 events in a row
      - amplitude within 20 % of run mean
      - on/off period coefficient of variation < 0.4 (regular spacing)
      - run lasts at least 30 minutes

    A run that passes these tests is almost certainly thermostat
    cycling. The classifier separately reports duty cycle and period
    so the user can confirm by eye (e.g. "10 min on / 5 min off for 4 hr").
    """
    detail = discovered_loads_for_circuit(space_id, days=days,
                                           min_step_w=min_step_w)
    events = sorted(detail['events'], key=lambda e: e['start'])
    if len(events) < 4:
        return []

    # Convert ISO strings to datetimes once.
    parsed = []
    for e in events:
        try:
            start = datetime.datetime.fromisoformat(e['start'])
            end   = datetime.datetime.fromisoformat(e['end'])
            parsed.append({**e, '_start_dt': start, '_end_dt': end})
        except (ValueError, TypeError):
            continue
    if len(parsed) < 4:
        return []

    runs: list[list[dict]] = []
    current = [parsed[0]]
    for i in range(1, len(parsed)):
        prev = current[-1]
        curr = parsed[i]
        amp_ratio = abs(curr['avg_w'] - prev['avg_w']) / max(prev['avg_w'], 1.0)
        gap_min = (curr['_start_dt'] - prev['_end_dt']).total_seconds() / 60.0
        # Same load? Same amplitude (±20%), reasonable gap (30 s to 60 min)
        if amp_ratio <= 0.20 and 0.5 <= gap_min <= 60.0:
            current.append(curr)
        else:
            if len(current) >= 4:
                runs.append(current)
            current = [curr]
    if len(current) >= 4:
        runs.append(current)

    cycling_runs = []
    for run in runs:
        # Period CoV check — regular spacing means low CoV
        gaps = []
        for i in range(1, len(run)):
            g = (run[i]['_start_dt'] - run[i-1]['_start_dt']).total_seconds() / 60.0
            gaps.append(g)
        if not gaps:
            continue
        mean_period = sum(gaps) / len(gaps)
        if mean_period <= 0:
            continue
        variance = sum((g - mean_period) ** 2 for g in gaps) / len(gaps)
        cov = (variance ** 0.5) / mean_period
        run_start = run[0]['_start_dt']
        run_end   = run[-1]['_end_dt']
        run_min   = (run_end - run_start).total_seconds() / 60.0
        if cov > 0.40 or run_min < 30:
            continue
        on_durations  = [e['duration_min'] for e in run]
        avg_on  = sum(on_durations) / len(on_durations)
        avg_off = mean_period - avg_on
        cycling_runs.append({
            'start':          run_start.isoformat(),
            'end':            run_end.isoformat(),
            'cycles':         len(run),
            'period_min':     mean_period,
            'on_duration_min':  avg_on,
            'off_duration_min': max(0.0, avg_off),
            'duty_pct':       (avg_on / mean_period * 100.0) if mean_period else 0.0,
            'amplitude_w':    sum(e['avg_w'] for e in run) / len(run),
            'peak_w':         max(e['peak_w'] for e in run),
            'total_kwh':      sum(e['kwh'] for e in run),
            'cov':            cov,
            'space_id':       space_id,
            'space_name':     detail['space_name'],
        })
    return cycling_runs


def detect_thermostat_cycling_all(days: int = 7,
                                    min_step_w: float = 200.0) -> list[dict]:
    """Run detect_thermostat_cycling against every circuit with space_live
    data; flatten + sort runs by total kWh so the biggest items surface first.
    Sub-panel real space is excluded since it has no space_live samples."""
    spaces = list_live_circuits()
    all_runs = []
    for sp in spaces:
        if sp['name'].startswith('Unused'):
            continue
        runs = detect_thermostat_cycling(
            sp['space_id'], days=days, min_step_w=min_step_w)
        all_runs.extend(runs)
    all_runs.sort(key=lambda r: r['total_kwh'], reverse=True)
    return all_runs


def cluster_cooccurrence(days: int = 7, min_events: int = 5,
                          bucket: str = '1 minute') -> list[dict]:
    """Phase 1.5.4 — pairwise co-occurrence between (circuit, cluster) pairs.

    Mines pairs where both clusters are "on" in the same minute bucket
    significantly more often than chance. Computes:
      - support_a: fraction of buckets cluster A is active
      - support_b: same for B
      - support_both: fraction with both active
      - lift: support_both / (support_a * support_b) — 1.0 means independent,
                                                       > 1 means correlated

    Used to confirm or refute Phase 1 cluster hypotheses (e.g. "this 850 W
    cluster on the kitchen circuit is co-active with the 200 W cluster on
    the AC outlet circuit → confirms window AC hypothesis").

    Practical filter: only clusters with ≥ `min_events` events qualify so we
    don't return noise.
    """
    # First pass: compute per-circuit clusters in Python and gather their
    # amplitude threshold. Then SQL counts co-occurrence per minute.
    # Use the live-circuit list so we don't waste a query on Sub-panel.
    spaces = [sp for sp in list_live_circuits()
              if not sp['name'].startswith('Unused')]

    # candidates: list of (space_id, space_name, amp_bin_w, threshold_w,
    #                     event_count, hypothesis)
    candidates = []
    for sp in spaces:
        d = discovered_loads_for_circuit(sp['space_id'], days=days)
        for c in d['clusters']:
            if c['count'] < min_events:
                continue
            # Detection threshold for this cluster's footprint: 70 % of the
            # cluster's mean amplitude. Avoids over-counting partial events.
            threshold = max(c['avg_amplitude_w'] * 0.70, 50.0)
            candidates.append({
                'space_id':    sp['space_id'],
                'space_name':  sp['name'],
                'amp_bin_w':   c['amplitude_bin_w'],
                'threshold_w': threshold,
                'count':       c['count'],
                'hypothesis':  c['hypothesis'],
                'duration_label': c.get('duration_label', ''),
            })

    if len(candidates) < 2:
        return []

    # Count co-occurrence per minute via SQL. Build a per-candidate
    # "active minutes" set, then intersect pair-wise in Python. For
    # ~50 candidates and ~7 days = 10080 minutes, this stays fast.
    active_minutes: dict[int, set] = {}
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        for i, c in enumerate(candidates):
            cur.execute(
                f"""
                SELECT DISTINCT time_bucket(%s::interval, ts) AS mb
                FROM space_live
                WHERE space_id = %s
                  AND ts >= NOW() - %s::interval
                  AND power_w >= %s;
                """,
                (bucket, c['space_id'], f'{days} days', c['threshold_w']),
            )
            active_minutes[i] = {r['mb'] for r in cur.fetchall()}

    # Estimate total buckets in window for support fractions.
    if bucket == '1 minute':
        total_buckets = days * 24 * 60
    elif bucket == '5 minutes':
        total_buckets = days * 24 * 12
    else:
        total_buckets = days * 24 * 60  # default to 1-min for unknown

    rows = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            # Skip pairs on the same circuit — that's intra-circuit overlap,
            # which the FHMM module handles instead.
            if candidates[i]['space_id'] == candidates[j]['space_id']:
                continue
            a = active_minutes[i]
            b = active_minutes[j]
            both = a & b
            if not both:
                continue
            support_a    = len(a) / total_buckets
            support_b    = len(b) / total_buckets
            support_both = len(both) / total_buckets
            expected     = support_a * support_b
            lift         = (support_both / expected) if expected > 0 else 0.0
            # Filter to interesting pairs only.
            if lift < 1.5 or support_both < 0.001 or len(both) < 5:
                continue
            rows.append({
                'a':            candidates[i],
                'b':            candidates[j],
                'support_a':    support_a,
                'support_b':    support_b,
                'support_both': support_both,
                'lift':         lift,
                'minutes_both': len(both),
            })
    rows.sort(key=lambda r: r['lift'], reverse=True)
    return rows


def cluster_event_history(space_id: str, amplitude_bin_w: int,
                           duration_label: str = '',
                           days: int = 30,
                           min_step_w: float = 200.0) -> dict:
    """Phase 1.5.5 — return the full event timeline for one specific cluster.

    Takes a cluster key (circuit + amplitude bin + optional short/long
    duration tag) and returns every event in it across `days`, plus
    aggregates: hour-of-day, day-of-week, daily kWh trend, first/last seen.
    Drives the cluster-detail page so the user can scroll an appliance's
    full operational history."""
    detail = discovered_loads_for_circuit(space_id, days=days,
                                           min_step_w=min_step_w)
    # Find the matching cluster's events.
    matching = [
        c for c in detail['clusters']
        if c['amplitude_bin_w'] == amplitude_bin_w and
           c.get('duration_label', '') == duration_label
    ]
    if not matching:
        return {
            'space_id':     space_id,
            'space_name':   detail['space_name'],
            'amplitude_bin_w': amplitude_bin_w,
            'duration_label': duration_label,
            'cluster':      None,
            'events':       [],
            'hour_dist':    [0] * 24,
            'dow_dist':     [0] * 7,
            'daily_kwh':    [],
        }
    cluster = matching[0]
    # Re-derive events for this bin from detail['events']:
    cluster_events = []
    for e in detail['events']:
        e_bin = int(round(e['avg_w'] / 50.0)) * 50
        if e_bin != amplitude_bin_w:
            continue
        cluster_events.append(e)
    # If we split by duration originally, re-apply the same split here.
    if duration_label in ('short', 'long'):
        groups = _split_by_duration(cluster_events)
        for evs, label in groups:
            if label == duration_label:
                cluster_events = evs
                break
    # Hour, day-of-week, and daily kWh aggregates
    hour_dist = [0] * 24
    dow_dist  = [0] * 7
    daily_kwh: dict[str, float] = {}
    for e in cluster_events:
        try:
            ts = datetime.datetime.fromisoformat(e['start']).astimezone(ZoneInfo(TZ))
            hour_dist[ts.hour] += 1
            dow_dist[ts.weekday()] += 1
            daily_kwh[ts.date().isoformat()] = daily_kwh.get(
                ts.date().isoformat(), 0.0) + e['kwh']
        except (ValueError, TypeError):
            pass
    daily_kwh_list = sorted(
        [{'date': k, 'kwh': v} for k, v in daily_kwh.items()],
        key=lambda x: x['date'])
    return {
        'space_id':         space_id,
        'space_name':       detail['space_name'],
        'amplitude_bin_w':  amplitude_bin_w,
        'duration_label':   duration_label,
        'cluster':          cluster,
        'events':           cluster_events,
        'hour_dist':        hour_dist,
        'dow_dist':         dow_dist,
        'daily_kwh':        daily_kwh_list,
    }


def discovered_loads_overview(days: int = 7,
                               min_step_w: float = 200.0) -> list[dict]:
    """One-row-per-circuit summary so /discover can show every circuit's
    headline numbers without 22 round trips. Each row has sample_count,
    baseline, event_count, top_cluster_amplitude. The detail view does
    the full discovery per circuit. Uses the live-circuit list so the
    Sub-panel real space (no SPAN local-API exposure) is excluded and
    the synthetic feedthrough aggregate is included."""
    rows: list[dict] = []
    spaces = [sp for sp in list_live_circuits()
              if not sp['name'].startswith('Unused')]
    for sp in spaces:
        d = discovered_loads_for_circuit(sp['space_id'], days=days,
                                          min_step_w=min_step_w)
        rows.append({
            'space_id':       sp['space_id'],
            'space_name':     sp['name'],
            'sample_count':   d['sample_count'],
            'baseline_w':     d['baseline_w'],
            'peak_w':         d['peak_w'],
            'event_count':    len(d['events']),
            'cluster_count':  len(d['clusters']),
            'top_cluster_w':  d['clusters'][0]['amplitude_bin_w'] if d['clusters'] else None,
            'top_cluster_hypothesis': d['clusters'][0]['hypothesis'] if d['clusters'] else None,
            'total_event_kwh': sum(e['kwh'] for e in d['events']),
        })
    rows.sort(key=lambda r: (r['event_count'], r['total_event_kwh']), reverse=True)
    return rows


def all_appliance_sessions(days: int = 30) -> dict:
    """Detect sessions for every profile in APPLIANCE_PROFILES.
    Returns {summary: [{key,label,...,count,kwh,hours,peak_kw,
                        secondary_count,secondary_kwh,secondary_label}],
             sessions: [merged list, sorted by start desc]}."""
    summary  = []
    combined = []
    for profile in APPLIANCE_PROFILES:
        sessions = appliance_sessions(profile['key'], days=days)
        # high_cycle appliances (fridge) produce 30+ sessions/day. Their
        # rollup tile still gets full counts but the combined session list
        # skips them so other appliances stay visible in the "All" view.
        if not profile.get('high_cycle'):
            combined.extend(sessions)
        if sessions:
            total_kwh = sum(s['kwh'] for s in sessions)
            total_min = sum(s['duration_min'] for s in sessions)
            peak_kw   = max(s['peak_kw'] for s in sessions)
        else:
            total_kwh = 0.0
            total_min = 0.0
            peak_kw   = 0.0
        row = {
            'key':          profile['key'],
            'label':        profile['label'],
            'glyph':        profile['glyph'],
            'color':        profile['color'],
            'note':         profile['note'],
            'threshold_kw': profile['threshold_kw'],
            'since_date':   profile.get('since_date'),
            'high_cycle':   profile.get('high_cycle', False),
            'count':        len(sessions),
            'kwh':          total_kwh,
            'hours':        total_min / 60.0,
            'peak_kw':      peak_kw,
            'avg_kwh':      (total_kwh / len(sessions)) if sessions else 0.0,
            'avg_min':      (total_min / len(sessions)) if sessions else 0.0,
        }
        if profile.get('secondary_kw') is not None:
            sec_kwh   = sum(s.get('secondary_kwh', 0.0) for s in sessions)
            sec_count = sum(1 for s in sessions if s.get('secondary_kwh', 0.0) > 0)
            sec_min   = sum(s.get('secondary_min', 0) for s in sessions)
            row['secondary_label'] = profile.get('secondary_label', 'high power')
            row['secondary_kwh']   = sec_kwh
            row['secondary_count'] = sec_count
            row['secondary_min']   = sec_min
            row['secondary_share'] = (sec_kwh / total_kwh) if total_kwh else 0.0
        summary.append(row)
    combined.sort(key=lambda s: s['start'], reverse=True)
    return {'summary': summary, 'sessions': combined}


def ev_circuit(cur) -> dict | None:
    """The EV charger's circuit: circuits.yaml `ev.space_ids` (the first one
    that exists), else the first circuit whose name matches one of
    `ev.name_match` (ILIKE patterns; default '%charge%' or '%EV%')."""
    ev = config.circuits()['ev']
    if ev['space_ids']:
        cur.execute(
            "SELECT space_id, name FROM spaces WHERE space_id = ANY(%s) "
            "ORDER BY array_position(%s, space_id) LIMIT 1;",
            (ev['space_ids'], ev['space_ids']),
        )
        cand = cur.fetchone()
        if cand:
            return cand
    if not ev['name_match']:
        return None
    cur.execute(
        "SELECT space_id, name FROM spaces WHERE "
        + " OR ".join(["name ILIKE %s"] * len(ev['name_match']))
        + " ORDER BY name LIMIT 1;",
        ev['name_match'],
    )
    return cur.fetchone()


def ev_charging_sessions(days: int = 90, threshold_kw: float | None = None) -> list[dict]:
    """Detect EV charging sessions on the EV charger's circuit (ev_circuit()).
    A session is a contiguous run of 10-min buckets averaging at least
    `threshold_kw` (default circuits.yaml `ev.threshold_kw`, 1.5 kW)."""
    if threshold_kw is None:
        threshold_kw = config.circuits()['ev']['threshold_kw']
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cand = ev_circuit(cur)
        if not cand:
            return []
        space_id = cand['space_id']
        space_name = cand['name']

        # Sessionize by gap: any bucket below threshold or > 20-min gap from
        # the previous bucket starts a new session.
        cur.execute(
            f"""
            WITH buckets AS (
                SELECT ts, energy_kwh::float AS kwh,
                       (energy_kwh * 3600.0 / duration_s)::float AS power_kw
                FROM space_readings
                WHERE space_id = %s
                  AND duration_s = 600
                  AND ts >= NOW() - %s::interval
                ORDER BY ts
            ),
            flagged AS (
                SELECT ts, kwh, power_kw,
                       CASE WHEN power_kw >= %s THEN 1 ELSE 0 END AS active
                FROM buckets
            ),
            grouped AS (
                SELECT ts, kwh, power_kw, active,
                       SUM(CASE WHEN active = 0 THEN 1 ELSE 0 END)
                         OVER (ORDER BY ts ROWS UNBOUNDED PRECEDING) AS group_id
                FROM flagged
            )
            SELECT MIN(ts) AS start_ts,
                   MAX(ts) AS end_ts,
                   SUM(kwh)::float AS kwh,
                   AVG(power_kw)::float AS avg_kw,
                   MAX(power_kw)::float AS peak_kw,
                   COUNT(*) AS buckets
            FROM grouped
            WHERE active = 1
            GROUP BY group_id
            HAVING SUM(kwh) > 0.5
            ORDER BY start_ts DESC;
            """,
            (space_id, f'{days} days', threshold_kw),
        )
        rows = cur.fetchall()
    sessions = []
    for r in rows:
        # +10 minutes because the last bucket's ts is the start, not the end
        duration_min = ((r['end_ts'] - r['start_ts']).total_seconds() / 60) + 10
        sessions.append({
            'start':         r['start_ts'].isoformat(),
            'end':           r['end_ts'].isoformat(),
            'date':          r['start_ts'].date().isoformat(),
            'duration_min':  round(duration_min, 1),
            'kwh':           r['kwh'],
            'avg_kw':        r['avg_kw'],
            'peak_kw':       r['peak_kw'],
            'space_id':      space_id,
            'space_name':    space_name,
        })
    return sessions


def recent_alerts(limit: int = 50) -> list[dict]:
    """Recent ntfy pushes from the anomaly_alerts table — backs the /alerts page."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT a.space_id, a.date::text AS date, a.ratio, a.kwh, a.baseline,
                   a.alerted_at, s.name AS space_name
            FROM anomaly_alerts a
            JOIN spaces s USING (space_id)
            ORDER BY a.alerted_at DESC
            LIMIT %s;
            """,
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]


def health_snapshot() -> dict:
    """Operational snapshot: ingest freshness per metric, last alerter run,
    DB row counts, weekly anomaly count. Drives /health."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        # Freshness per metric (most recent ts per metric in panel_readings)
        cur.execute(
            """
            SELECT metric, MAX(ts) AS last_ts, COUNT(*) AS rows
            FROM panel_readings GROUP BY metric ORDER BY metric;
            """
        )
        panel_metrics = [dict(r) for r in cur.fetchall()]

        # Per-space totals + last ts (one row per circuit)
        cur.execute(
            """
            SELECT s.name AS name, COUNT(*) AS rows, MAX(sr.ts) AS last_ts,
                   SUM(sr.energy_kwh)::float AS total_kwh
            FROM space_readings sr JOIN spaces s USING (space_id)
            GROUP BY s.name ORDER BY total_kwh DESC NULLS LAST;
            """
        )
        spaces_meta = [dict(r) for r in cur.fetchall()]

        # Latest ingest run + recent successes/failures
        cur.execute(
            """
            SELECT id, started_at, finished_at, status, panel_rows_in, space_rows_in
            FROM ingest_runs ORDER BY id DESC LIMIT 10;
            """
        )
        recent_runs = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT COUNT(*) FROM ingest_runs WHERE status = 'success' AND started_at > NOW() - INTERVAL '24 hours';"
        )
        runs_24h = (cur.fetchone() or {'count': 0}).get('count') or 0

        # Anomaly stats
        cur.execute(
            "SELECT COUNT(*) FROM anomaly_alerts WHERE alerted_at > NOW() - INTERVAL '7 days';"
        )
        anomalies_week = (cur.fetchone() or {'count': 0}).get('count') or 0
        cur.execute(
            "SELECT MAX(alerted_at) AS last_alert FROM anomaly_alerts;"
        )
        last_alert = (cur.fetchone() or {}).get('last_alert')

        # Row counts
        cur.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM panel_readings)::bigint  AS panel_rows,
              (SELECT COUNT(*) FROM space_readings)::bigint  AS space_rows,
              (SELECT COUNT(*) FROM spaces)::bigint          AS spaces,
              (SELECT COUNT(*) FROM ingest_runs)::bigint     AS ingest_runs;
            """
        )
        counts = dict(cur.fetchone() or {})

    return {
        'panel_metrics':   panel_metrics,
        'spaces_meta':     spaces_meta,
        'recent_runs':     recent_runs,
        'runs_24h':        runs_24h,
        'anomalies_week':  anomalies_week,
        'last_alert':      last_alert,
        'counts':          counts,
    }


def current_rates() -> dict | None:
    """The optional flat tariff from config (RATE_IMPORT_PER_KWH, and
    RATE_EXPORT_CREDIT_PER_KWH for exports, default the same): $ per kWh
    for the pages that price energy. None when it's unset, and then every
    page shows kWh only. Bill-accurate cost is out of scope: a utility's
    tiers, riders and fixed charges don't come from the panel."""
    flat = config.flat_rate()
    if flat is None:
        return None
    return {
        'all_in_per_kwh':         flat['import_per_kwh'],
        'credit_dollars_per_kwh': flat['export_credit_per_kwh'],
        'source':                 'flat',
    }


def ingest_health() -> dict:
    """Latest ingest run + freshness of the data, for a footer status badge."""
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT id, started_at, finished_at, status, panel_rows_in, "
            "space_rows_in FROM ingest_runs "
            "ORDER BY id DESC LIMIT 1;"
        )
        last_run = cur.fetchone()
        cur.execute(
            "SELECT MAX(ts) AS latest_ts FROM panel_readings "
            "WHERE metric = 'building_total';"
        )
        latest_ts = cur.fetchone()['latest_ts']
    return {'last_run': last_run, 'latest_ts': latest_ts}


# ---- Duty-cycle circuits (circuits.yaml `duty_cycle`) ----------------------
#
# Each entry names a circuit (space_id, else name_match) that switches on and
# off by itself. `on_kw`: a 10-min bucket averaging this is "on". `standby_w`
# and `on_w`: the circuit's draw idle and running, for on-time from energy.
# `since` starts its history (e.g. after a rewiring or a new appliance).


def _duty_space_id(cur, dc: dict) -> str | None:
    """A duty-cycle circuit's space_id: its own, else by exact name."""
    cur.execute(
        """
        SELECT space_id FROM spaces
        WHERE space_id = %s OR name ILIKE %s
        ORDER BY (space_id = %s) DESC, name LIMIT 1;
        """,
        (dc.get('space_id'), dc.get('name_match') or '', dc.get('space_id')),
    )
    r = cur.fetchone()
    if r is None:
        return None
    return r['space_id'] if isinstance(r, dict) else r[0]


def _duty_since(dc: dict) -> str:
    return f"{dc.get('since') or '1970-01-01'} 00:00 {TZ}"


def dehumidifier_overview(days: int = 60) -> dict:
    """The dehumidifier (duty_cycle key `dehumidifier`): daily kWh, hours
    running, duty %, and month-by-month kWh since `since`. $ fields are
    priced at the optional flat rate, and None without one."""
    rates = current_rates()
    rate = rates['all_in_per_kwh'] if rates else None
    dc = config.duty_circuit('dehumidifier')

    def cost(kwh):
        return kwh * rate if rate is not None else None

    empty = {'daily': [], 'monthly': [], 'rate': rate, 'last30_days': 0,
             'last30_kwh': 0.0, 'last30_cost': cost(0.0), 'last30_duty_pct': None,
             'on_kw': dc['on_kw'] if dc else 0.0, 'configured': dc is not None}
    if dc is None:
        return empty
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        space_id = _duty_space_id(cur, dc)
        if space_id is None:
            return empty
        cur.execute(
            f"""
            SELECT (ts AT TIME ZONE '{TZ}')::date AS day,
                   SUM(energy_kwh)::float AS kwh,
                   COUNT(*) FILTER (WHERE energy_kwh * 3600.0 / duration_s >= %s) AS on_buckets,
                   COUNT(*) AS buckets
            FROM space_readings
            WHERE space_id = %s AND duration_s = 600
              AND ts >= GREATEST(%s::timestamptz, NOW() - %s::interval)
            GROUP BY 1 ORDER BY 1;
            """,
            (dc['on_kw'], space_id, _duty_since(dc), f'{days} days'),
        )
        daily = []
        for r in cur.fetchall():
            daily.append({
                'date': r['day'].isoformat(),
                'kwh': r['kwh'],
                'cost': cost(r['kwh']),
                'hours_on': r['on_buckets'] / 6.0,
                'duty_pct': 100.0 * r['on_buckets'] / r['buckets'] if r['buckets'] else None,
            })
        cur.execute(
            f"""
            SELECT date_trunc('month', ts AT TIME ZONE '{TZ}')::date AS month,
                   SUM(energy_kwh)::float AS kwh,
                   COUNT(DISTINCT (ts AT TIME ZONE '{TZ}')::date) AS days
            FROM space_readings
            WHERE space_id = %s AND duration_s <= 86400
              AND ts >= %s::timestamptz
            GROUP BY 1 ORDER BY 1;
            """,
            (space_id, _duty_since(dc)),
        )
        monthly = [{
            'month': r['month'].isoformat()[:7],
            'kwh': r['kwh'],
            'days': r['days'],
            'cost': cost(r['kwh']),
            'cost_per_30d': cost(r['kwh'] * 30.0 / r['days']) if r['days'] else None,
        } for r in cur.fetchall()]

    # The last 30 full days, today excluded.
    full = daily[:-1][-30:]
    last30_kwh = sum(d['kwh'] for d in full)
    return {
        'daily': daily,
        'monthly': monthly,
        'rate': rate,
        'last30_days': len(full),
        'last30_kwh': last30_kwh,
        'last30_cost': cost(last30_kwh),
        'last30_duty_pct': (sum(d['hours_on'] for d in full) / (24.0 * len(full)) * 100) if full else None,
        'on_kw': dc['on_kw'],
        'label': dc['label'],
        'since': dc.get('since'),
        'configured': True,
    }


# ---- Generic duty-cycle circuits (circuits.yaml `duty_cycle`) ---------------

def duty_cycle_overview(days: int = 30) -> list[dict]:
    """Every configured duty-cycle circuit, from SPAN data alone: per local
    day its kWh, the minutes it ran and how many times it started.

    On-time comes from the 10-minute buckets (SPAN keeps about a month of
    them). With `standby_w` and `on_w` set (a burner, a pump: a known draw
    idle and running), each bucket's on-time is its energy above standby
    over the running draw, so a 3-minute firing counts 3 minutes; under a
    minute is standby noise and counts as off. Without them, a
    bucket averaging `on_kw` or more is on for its 10 minutes. A start is a
    running bucket after an idle one, so two cycles in adjacent buckets
    count once. Older days have daily totals only: with `standby_w` and
    `on_w` their on-time is (kWh - standby) / (on - standby), else unknown."""
    out = []
    circuits = config.circuits()['duty_cycle']
    if not circuits:
        return out
    with pool().connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        for dc in circuits:
            space_id = _duty_space_id(cur, dc)
            item = {'key': dc['key'], 'label': dc['label'], 'space_id': space_id,
                    'on_kw': dc['on_kw'], 'standby_w': dc.get('standby_w'),
                    'on_w': dc.get('on_w'), 'since': dc.get('since'),
                    'glyph': dc.get('glyph', '●'), 'days': []}
            out.append(item)
            if space_id is None:
                continue
            args = {'sid': space_id, 'since': _duty_since(dc), 'span': f'{int(days)} days'}
            cur.execute(
                f"""
                SELECT (ts AT TIME ZONE '{TZ}')::date AS day, energy_kwh::float AS kwh
                FROM space_readings
                WHERE space_id = %(sid)s AND duration_s = 600
                  AND ts >= GREATEST(%(since)s::timestamptz, NOW() - %(span)s::interval)
                ORDER BY ts;
                """,
                args,
            )
            buckets = cur.fetchall()
            cur.execute(
                f"""
                SELECT (ts AT TIME ZONE '{TZ}')::date AS day, SUM(energy_kwh)::float AS kwh
                FROM space_readings
                WHERE space_id = %(sid)s AND duration_s <= 86400
                  AND ts >= GREATEST(%(since)s::timestamptz, NOW() - %(span)s::interval)
                GROUP BY 1 ORDER BY 1 DESC;
                """,
                args,
            )
            totals = cur.fetchall()

            standby = (dc.get('standby_w') or 0.0) / 1000.0
            net = ((dc.get('on_w') or 0.0) / 1000.0) - standby
            fine: dict = {}
            prev_on = False
            for b in buckets:
                if net > 0:
                    minutes = min(max(b['kwh'] - standby / 6.0, 0.0) / net * 60.0, 10.0)
                    is_on = minutes >= 1.0
                    if not is_on:
                        minutes = 0.0   # standby noise, not a firing
                else:
                    is_on = b['kwh'] * 6.0 >= dc['on_kw']
                    minutes = 10.0 if is_on else 0.0
                f = fine.setdefault(b['day'], {'minutes': 0.0, 'starts': 0, 'buckets': 0})
                f['buckets'] += 1
                f['minutes'] += minutes
                if is_on and not prev_on:
                    f['starts'] += 1
                prev_on = is_on

            for r in totals:
                f = fine.get(r['day'])
                if f:
                    minutes, starts, method = f['minutes'], f['starts'], '10-min buckets'
                elif net > 0:
                    minutes = min(max(r['kwh'] - standby * 24, 0.0) / net, 24.0) * 60.0
                    starts, method = None, 'daily energy'
                else:
                    minutes, starts, method = None, None, None
                item['days'].append({
                    'date': r['day'].isoformat(), 'kwh': r['kwh'],
                    'minutes_on': minutes,
                    'hours_on': minutes / 60.0 if minutes is not None else None,
                    'starts': starts, 'method': method,
                    'coverage': (f['buckets'] / 144.0) if f else None,
                })
            full = item['days'][1:]   # today is partial
            item['total_kwh'] = sum(d['kwh'] for d in full)
            hrs = [d['hours_on'] for d in full if d['hours_on'] is not None]
            item['mean_hours_on'] = (sum(hrs) / len(hrs)) if hrs else None
            item['mean_kwh'] = (item['total_kwh'] / len(full)) if full else None
    return out


# ---- DB vs VictoriaMetrics -------------------------------------------------

# An optional plugin, off unless config VM_URL is set: a VictoriaMetrics that
# scrapes this app's MQTT feed (or anything else exporting the panel's solar
# and load power in W, named by VM_SOLAR_SERIES / VM_LOAD_SERIES).


def _vm_energy() -> dict:
    s = config.vm_series()
    return {
        'solar':          f"sum(integrate({s['solar']}[600s]))/3.6e6",
        'building_total': f"sum(integrate({s['load']}[600s]))/3.6e6",
    }


def db_vs_vm_check(days: int = 7, min_changes: int = 18,
                   flag_pct: float = 3.0) -> dict:
    """Day by day (NY-local), SPAN's cloud history in panel_readings against
    VictoriaMetrics' 30-s SPAN data, counting only the 10-minute buckets where
    VM was live (at least `min_changes` of its 20 samples changed). While the
    panel is unreachable VM repeats its last value, so frozen buckets prove
    nothing either way. A day is flagged when solar or load differs by more
    than `flag_pct` %. On demand only: no schedule, nothing stored. Returns
    {'enabled': False, ...} when VM_URL isn't set."""
    import requests

    base = config.vm_url()
    if not base:
        return {'enabled': False, 'error': None, 'days': [],
                'flag_pct': flag_pct, 'min_changes': min_changes}
    query_range = f'{base}/api/v1/query_range'

    now = datetime.datetime.now(ZoneInfo(TZ))
    end = now.replace(minute=now.minute - now.minute % 10, second=0, microsecond=0)
    start = (end - datetime.timedelta(days=days)).replace(hour=0, minute=0)

    def vm(q):
        r = requests.get(query_range, timeout=30, params={
            'query': q, 'start': int(start.timestamp()) + 600,
            'end': int(end.timestamp()), 'step': 600})
        r.raise_for_status()
        out = {}
        for series in r.json()['data']['result']:
            for ts, v in series['values']:
                out[int(ts) - 600] = float(v)   # value at T covers (T-600, T]
        return out

    try:
        live = vm(f"changes({config.vm_series()['load']}[600s])")
        vm_kwh = {m: vm(q) for m, q in _vm_energy().items()}
    except (requests.RequestException, KeyError, ValueError) as e:
        return {'enabled': True, 'error': f'VictoriaMetrics: {e}', 'days': []}

    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT extract(epoch FROM ts)::bigint, metric, energy_kwh::float
            FROM panel_readings
            WHERE ts >= %s AND ts < %s AND duration_s = 600
              AND metric IN ('solar', 'building_total');
            """, (start, end))
        db = {}
        for ts, metric, kwh in cur.fetchall():
            db.setdefault(int(ts), {})[metric] = kwh

    by_day = {}
    for ts, n in live.items():
        day = datetime.datetime.fromtimestamp(ts, ZoneInfo(TZ)).date()
        d = by_day.setdefault(day, {'date': day, 'buckets': 0, 'live_buckets': 0,
                                    'missing_in_db': 0, 'db_solar': 0.0,
                                    'vm_solar': 0.0, 'db_load': 0.0, 'vm_load': 0.0})
        d['buckets'] += 1
        if n < min_changes:
            continue
        d['live_buckets'] += 1
        row = db.get(ts)
        if row is None:
            d['missing_in_db'] += 1
            row = {}
        d['db_solar'] += row.get('solar', 0.0)
        d['db_load'] += row.get('building_total', 0.0)
        d['vm_solar'] += vm_kwh['solar'].get(ts, 0.0)
        d['vm_load'] += vm_kwh['building_total'].get(ts, 0.0)

    def pct(a, b, floor):
        return None if b < floor else 100.0 * (a - b) / b

    out = []
    for day in sorted(by_day, reverse=True):
        d = by_day[day]
        d['solar_pct'] = pct(d['db_solar'], d['vm_solar'], 1.0)
        d['load_pct'] = pct(d['db_load'], d['vm_load'], 1.0)
        d['flagged'] = any(p is not None and abs(p) > flag_pct
                           for p in (d['solar_pct'], d['load_pct']))
        out.append(d)
    return {'enabled': True, 'error': None, 'days': out, 'flag_pct': flag_pct,
            'min_changes': min_changes}
