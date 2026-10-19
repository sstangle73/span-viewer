"""app/api.py — read-only JSON API at /api/v1/*.

This is the surface for other services (Home Assistant, dashboards,
scripts, notebooks, anything LAN-only) to consume our data without
screen-scraping the HTML pages. It is a stable, versioned contract (README,
"API v1"): within v1, add endpoints and fields but never rename, remove or
change the meaning of one; breaking changes go under /api/v2. The shapes here mirror what the templates already get, so
adding a new endpoint = a thin wrapper around an existing queries.py
function plus a serializer pass for datetimes.

Posture: same as the rest of the app — LAN-only, no auth,
read-only. If we ever expose this outside the LAN we'd want a token check
on this router specifically.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from .live_store import latest_snapshot, latest_snapshot_age_s, source_snapshot
from . import queries as q

router = APIRouter(prefix='/api/v1', tags=['api-v1'])


# ---- Helpers ---------------------------------------------------------------

def _jsonable(v: Any) -> Any:
    """Make psycopg / stdlib values JSON-safe. Datetimes → ISO strings,
    Decimals → float, dicts/lists recursed."""
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if hasattr(v, '__float__') and not isinstance(v, (int, float, bool)):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    return v


# ---- Index / discovery -----------------------------------------------------

@router.get('')
async def api_index():
    """Self-describing index of v1 endpoints — useful when poking the API
    by hand. FastAPI's auto /docs and /openapi.json are still the
    canonical surfaces."""
    return {
        'version': 'v1',
        'docs': '/docs',
        'openapi': '/openapi.json',
        'endpoints': [
            # Live / real-time
            {'path': '/api/v1/live',                  'desc': 'Cached panel + per-circuit snapshot (3 s freshness)'},
            {'path': '/api/v1/live/history',          'desc': 'Downsampled panel time series; window=5m|1h|6h|24h'},
            {'path': '/api/v1/live/yesterday',        'desc': 'House load at the same time 24 h ago (W)'},
            # Topology / config
            {'path': '/api/v1/circuits',              'desc': 'All circuits + whole-house, with lifetime kWh totals'},
            # Panel totals (stable contract, see README "API v1")
            {'path': '/api/v1/panel/daily',           'desc': 'Import, export, solar and load kWh per local day (config TZ), with coverage; ?start&end=YYYY-MM-DD'},
            # Per-circuit analytics
            {'path': '/api/v1/circuits/{id}/yoy',     'desc': 'Year-over-year monthly kWh for the given circuit'},
            {'path': '/api/v1/circuits/{id}/daily',   'desc': 'Daily kWh for a calendar year; ?year=YYYY'},
            {'path': '/api/v1/circuits/{id}/day',     'desc': 'Single-day curve; ?date=YYYY-MM-DD'},
            # Top-K
            {'path': '/api/v1/top',                   'desc': 'Top-consumer ranking; ?window=30d|90d|12mo|all'},
            {'path': '/api/v1/top-days',              'desc': 'Biggest consumption days; ?window&circuit&limit'},
            # Solar
            {'path': '/api/v1/solar/overview',        'desc': 'Headline solar stats: lifetime + month + best day'},
            {'path': '/api/v1/solar/monthly',         'desc': 'Monthly produced/consumed/exported/self-pct rows'},
            {'path': '/api/v1/solar/year-end',        'desc': 'Year-end production projection'},
            # Events
            {'path': '/api/v1/sessions',              'desc': 'EV charging session log; ?days=90'},
            {'path': '/api/v1/duty-cycle',            'desc': 'Configured duty-cycle circuits (circuits.yaml): kWh, minutes on, starts per day; ?days=30'},
            {'path': '/api/v1/anomalies',             'desc': 'Detected per-circuit anomalies; ?window=90&threshold=3.0'},
            {'path': '/api/v1/alerts',                'desc': 'Recent push-alert log; ?limit=100'},
            {'path': '/api/v1/house/dehumidifier',    'desc': 'The duty-cycle circuit keyed dehumidifier: kWh and duty by day and month ($ with a flat rate); ?days=60'},
            # Health
            {'path': '/api/v1/health',                'desc': 'Ingest freshness + row counts + recent runs'},
            {'path': '/api/v1/health/vm-check',       'desc': 'Daily SPAN-cloud kWh vs VictoriaMetrics (live buckets only)'},
        ],
    }


# ---- Live ------------------------------------------------------------------

@router.get('/live')
async def v1_live():
    """Same payload as the legacy /api/live (cached snapshot or live
    fall-through). Mirrored under /v1 so external consumers have one
    stable namespace to subscribe to."""
    snap = latest_snapshot()
    if snap is None:
        return JSONResponse(source_snapshot())
    return JSONResponse({**snap, 'cached_at_s': latest_snapshot_age_s()})


@router.get('/live/history')
async def v1_live_history(window: str = '5m'):
    return JSONResponse(q.live_history(window))


@router.get('/live/yesterday')
async def v1_live_yesterday():
    return JSONResponse(q.load_24h_ago_w())


# ---- Topology / config -----------------------------------------------------

@router.get('/circuits')
async def v1_circuits():
    return JSONResponse(_jsonable(q.list_circuits()))


# ---- Per-circuit analytics -------------------------------------------------

@router.get('/circuits/{circuit_id}/yoy')
async def v1_circuit_yoy(circuit_id: str):
    try:
        return JSONResponse(_jsonable(q.yoy_monthly(circuit_id)))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get('/circuits/{circuit_id}/daily')
async def v1_circuit_daily(circuit_id: str, year: int | None = None):
    if year is None:
        year = q.today_local().year
    return JSONResponse(_jsonable(
        q.daily_kwh_for_year(year, circuit_id)
    ))


@router.get('/circuits/{circuit_id}/day')
async def v1_circuit_day(circuit_id: str, date: str | None = None):
    if date is None:
        date = q.today_local().isoformat()
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail=f'bad date: {date}')
    return JSONResponse(_jsonable(q.day_curve(date, circuit_id)))


# ---- Panel totals ----------------------------------------------------------

PANEL_DAILY_MAX_DAYS = 3660


@router.get('/panel/daily')
def v1_panel_daily(start: str | None = None, end: str | None = None):
    """Panel kWh per local day (config TZ), start..end inclusive
    (YYYY-MM-DD). end defaults to today, start to 29 days before end; an
    end after today is clipped to today. Every day in the range gets a
    row, so gaps show as coverage < 1 rather than missing rows."""
    today = q.today_local()
    try:
        end_d = datetime.date.fromisoformat(end) if end else today
        start_d = (datetime.date.fromisoformat(start) if start
                   else end_d - datetime.timedelta(days=29))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f'bad date: {e}')
    end_d = min(end_d, today)
    if start_d > end_d:
        raise HTTPException(status_code=400, detail='start is after end')
    if (end_d - start_d).days + 1 > PANEL_DAILY_MAX_DAYS:
        raise HTTPException(status_code=400,
                            detail=f'range over {PANEL_DAILY_MAX_DAYS} days')
    return JSONResponse({
        'timezone': q.TZ,
        'start': start_d.isoformat(),
        'end': end_d.isoformat(),
        'days': _jsonable(q.panel_daily_local(start_d, end_d)),
    })


# ---- Top-K -----------------------------------------------------------------

@router.get('/top')
async def v1_top(window: str = '30d'):
    try:
        return JSONResponse(_jsonable(q.top_consumers(window)))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get('/top-days')
async def v1_top_days(window: str = '12mo',
                      circuit: str = q.WHOLE_HOUSE,
                      limit: int = 25):
    return JSONResponse(_jsonable(q.top_days(window, circuit, limit=limit)))


# ---- Solar -----------------------------------------------------------------

@router.get('/solar/overview')
async def v1_solar_overview():
    return JSONResponse(_jsonable(q.solar_overview()))


@router.get('/solar/monthly')
async def v1_solar_monthly():
    return JSONResponse(_jsonable(q.solar_monthly_full()))


@router.get('/solar/year-end')
async def v1_solar_year_end():
    return JSONResponse(_jsonable(q.year_end_production_forecast()))


# ---- Events ----------------------------------------------------------------

@router.get('/sessions')
async def v1_sessions(days: int = 90):
    return JSONResponse(_jsonable(q.ev_charging_sessions(days=days)))


@router.get('/anomalies')
async def v1_anomalies(window: int = 90, threshold: float = 3.0):
    return JSONResponse(_jsonable(
        q.anomaly_history(window_days=window, threshold=threshold)
    ))


@router.get('/alerts')
async def v1_alerts(limit: int = 100):
    return JSONResponse(_jsonable(q.recent_alerts(limit=limit)))


# ---- Duty-cycle circuits ----------------------------------------------------

@router.get('/duty-cycle')
async def v1_duty_cycle(days: int = 30):
    days = max(1, min(days, 3660))
    return JSONResponse(_jsonable(q.duty_cycle_overview(days=days)))


# ---- The dehumidifier (duty_cycle key `dehumidifier`) ----------------------

@router.get('/house/dehumidifier')
async def v1_house_dehumidifier(days: int = 60):
    return JSONResponse(_jsonable(q.dehumidifier_overview(days=days)))


# ---- Health ----------------------------------------------------------------

@router.get('/health')
async def v1_health():
    return JSONResponse(_jsonable(q.health_snapshot()))


@router.get('/health/vm-check')
def v1_health_vm_check(days: int = Query(7, ge=1, le=30)):
    return JSONResponse(_jsonable(q.db_vs_vm_check(days)))
