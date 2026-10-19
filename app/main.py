"""span-viewer FastAPI app: the pages, the /api/live endpoints the pages
poll, the CSV/JSON exports, and the /api/v1 router (app/api.py)."""

import datetime
import hashlib
import json
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config
from .api import router as api_v1_router
from .db import lifespan
from .live_store import (latest_snapshot, latest_snapshot_age_s,
                         source_report, source_snapshot)
from .queries import (
    current_rates,
    list_circuits,
    live_history,
    load_24h_ago_w,
    yoy_monthly,
    ingest_health,
    top_consumers,
    daily_kwh_for_year,
    monthly_kwh_lifetime,
    years_with_data,
    top_days,
    day_curve,
    hourly_pattern,
    solar_overview,
    solar_monthly_full,
    solar_yoy_monthly,
    solar_daily_for_year,
    best_solar_days,
    solar_hour_of_day,
    daily_produced_vs_consumed,
    capacity_factor,
    same_day_prior_years,
    anomaly_history,
    day_per_circuit,
    same_month_prior_years,
    recent_alerts,
    health_snapshot,
    db_vs_vm_check,
    energy_flows,
    solar_vs_consumption_hourly,
    phantom_load_daily,
    year_end_production_forecast,
    ev_charging_sessions,
    APPLIANCE_PROFILES,
    appliance_sessions,
    all_appliance_sessions,
    appliance_daily_stats,
    discovered_loads_for_circuit,
    discovered_loads_overview,
    detect_thermostat_cycling_all,
    cluster_cooccurrence,
    cluster_event_history,
    list_live_circuits,
    dehumidifier_overview,
    duty_cycle_overview,
    today_local,
    WHOLE_HOUSE,
)
import csv
import io
from fastapi.responses import StreamingResponse, JSONResponse

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('span_viewer')

APP_DIR = Path(__file__).parent

app = FastAPI(lifespan=lifespan)
app.mount('/static', StaticFiles(directory=str(APP_DIR / 'static')), name='static')
# Read-only JSON API surface for outside consumers (Home Assistant, scripts,
# notebooks). HTML pages keep using the legacy /api/live/* endpoints; new
# external integrations should target /api/v1/*.
app.include_router(api_v1_router)

# Config REDIRECTS: paths answered with a 301 elsewhere, e.g. pages a
# deployment moved to another app. Empty by default.
_REDIRECTS = config.redirects()


@app.middleware('http')
async def configured_redirects(request: Request, call_next):
    target = _REDIRECTS.get(request.url.path)
    if target is None:
        return await call_next(request)
    if request.url.query:
        target += ('&' if '?' in target else '?') + request.url.query
    return RedirectResponse(target, status_code=301)

templates = Jinja2Templates(directory=str(APP_DIR / 'templates'))

# Nav items shared across all views — grouped into a handful of
# categories so the menu reads cleanly in a drawer on both phone and
# desktop. Adding a view = add a (path, label) tuple to the right group.
NAV = (
    ('Usage', (
        ('/live',    'Live'),
        ('/yoy',     'YoY'),
        ('/top',     'Top consumers'),
        ('/heatmap', 'Heatmap'),
        ('/hours',   'Hour pattern'),
        ('/day',     'Day view'),
        ('/biggest', 'Biggest days'),
    )),
    ('Solar', (
        ('/solar',   'Solar'),
        ('/flows',   'Flows'),
    )),
    ('Events', (
        ('/anomalies', 'Anomalies'),
        ('/alerts',    'Alerts log'),
        ('/sessions',    'EV sessions'),
        ('/appliances',  'Appliances'),
        ('/duty',        'Duty cycles'),
        ('/discover',    'Load discovery'),
        ('/discover/ac', 'AC cycling'),
        ('/discover/cooccur', 'Co-occurrence'),
        ('/attribute',   'Disaggregate'),
    )),
    ('Ops', (
        ('/health', 'Health'),
    )),
)

# Flat path→label map for fast lookup of the current page's title.
_NAV_LABEL = {path: label for _, items in NAV for path, label in items}


# Content-hash of style.css, computed once at import. Embedded as a
# ?v=<hash> query on the stylesheet <link> so deploys auto-invalidate the
# browser HTTP cache (and any service-worker cache) without manual bumps.
# 8 hex chars is plenty of entropy for a single-file cache key.
_STYLE_CSS = APP_DIR / 'static' / 'style.css'
STATIC_V = hashlib.md5(_STYLE_CSS.read_bytes()).hexdigest()[:8]


def _common_ctx(active: str) -> dict:
    return {
        'nav': NAV,
        'active': active,
        'active_label': _NAV_LABEL.get(active, ''),
        'health': ingest_health(),
        'rates': current_rates(),
        'static_v': STATIC_V,
    }


def _yoy_payload(circuit_id: str) -> dict:
    data = yoy_monthly(circuit_id)
    return {
        'circuit_id': data['circuit_id'],
        'circuit_name': data['circuit_name'],
        'years': data['years'],
        'months_json': json.dumps(data['months']),
    }


@app.get('/', response_class=HTMLResponse)
async def home():
    return RedirectResponse('/yoy', status_code=302)


# ---- PWA: manifest + service worker served from root --------------------
# The service worker must be served from the origin root for `scope: "/"`
# to register cleanly without the Service-Worker-Allowed dance. We serve the
# manifest from root for the same reason — keeps `start_url`/`scope` simple.

@app.get('/manifest.webmanifest', include_in_schema=False)
async def pwa_manifest():
    return FileResponse(
        APP_DIR / 'static' / 'manifest.webmanifest',
        media_type='application/manifest+json',
    )


@app.get('/sw.js', include_in_schema=False)
async def pwa_service_worker():
    # No-cache so a re-deploy of the SW takes effect on next page load.
    return FileResponse(
        APP_DIR / 'static' / 'sw.js',
        media_type='application/javascript',
        headers={'Cache-Control': 'no-cache'},
    )


# ---- Live (real-time from the panel's on-LAN API) --------------------------

@app.get('/live', response_class=HTMLResponse)
async def live_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name='live.html',
        context={**_common_ctx('/live')},
    )


@app.get('/api/live')
async def live_snapshot():
    """Latest snapshot the background poller stashed. Reads from a
    process-local cache (filled every 3 s by app.live_store) so the
    browser-facing endpoint never blocks on a SPAN round-trip and
    multiple sessions share a single upstream poll budget."""
    snap = latest_snapshot()
    if snap is None:
        # Cold start: poller hasn't run yet. Fall through to a direct
        # fetch so the page isn't stuck on "connecting…" for 3 s.
        return JSONResponse(source_snapshot())
    age = latest_snapshot_age_s()
    out = {**snap, 'cached_at_s': age}
    return JSONResponse(out)


@app.get('/api/live/source')
async def live_source():
    """Where /live's snapshots come from (v1 REST, the panel's eBus MQTT, or
    v1 with eBus shadowed alongside), the eBus connection's health, and in
    shadow mode the running v1-vs-eBus comparison. See app.live_store."""
    return JSONResponse(source_report())


@app.get('/api/live/history')
async def live_history_endpoint(window: str = '5m'):
    """Downsampled panel-level time series for the /live strip chart.
    `window` is one of 5m / 1h / 6h / 24h — unknown values fall back to
    5m. Reads from the panel_live hypertable that the live_store poller
    fills every 3 s."""
    return JSONResponse(live_history(window))


@app.get('/api/live/yesterday')
def live_yesterday():
    """House load at the same time 24 h ago (DB-backed). /live polls this
    once a minute for the vs-yesterday tile — separate from /api/live so
    the heavy-cadence local-API endpoint stays DB-free."""
    return JSONResponse(load_24h_ago_w())


# ---- YoY -------------------------------------------------------------------

@app.get('/yoy', response_class=HTMLResponse)
async def yoy_page(request: Request, circuit: str = WHOLE_HOUSE):
    return templates.TemplateResponse(
        request=request,
        name='yoy.html',
        context={
            **_common_ctx('/yoy'),
            'circuits': list_circuits(),
            'selected_circuit': circuit,
            **_yoy_payload(circuit),
        },
    )


@app.get('/yoy/chart', response_class=HTMLResponse)
async def yoy_chart_fragment(request: Request, circuit: str = WHOLE_HOUSE):
    return templates.TemplateResponse(
        request=request,
        name='yoy_chart.html',
        context=_yoy_payload(circuit),
    )


# ---- Top consumers ---------------------------------------------------------

@app.get('/top', response_class=HTMLResponse)
async def top_page(request: Request, window: str = '30d'):
    rows = top_consumers(window)
    return templates.TemplateResponse(
        request=request,
        name='top.html',
        context={
            **_common_ctx('/top'),
            'window': window,
            'rows': rows,
            'rows_json': json.dumps(rows),
            'total_kwh': sum(r['kwh'] for r in rows),
        },
    )


# ---- Calendar heatmap ------------------------------------------------------

@app.get('/heatmap', response_class=HTMLResponse)
async def heatmap_page(request: Request, year: int | None = None,
                       circuit: str = WHOLE_HOUSE, scale: str = 'daily'):
    years = years_with_data() or [today_local().year]
    if year is None:
        year = years[-1]
    if year not in years:
        year = years[-1]
    if scale not in ('daily', 'monthly'):
        scale = 'daily'
    days = daily_kwh_for_year(year, circuit) if scale == 'daily' else []
    months = monthly_kwh_lifetime(circuit) if scale == 'monthly' else []
    return templates.TemplateResponse(
        request=request,
        name='heatmap.html',
        context={
            **_common_ctx('/heatmap'),
            'circuits': list_circuits(),
            'selected_circuit': circuit,
            'years': years,
            'selected_year': year,
            'scale': scale,
            'days_json':   json.dumps(days),
            'months_json': json.dumps(months),
        },
    )


# ---- Solar self-consumption ------------------------------------------------

@app.get('/solar', response_class=HTMLResponse)
async def solar_page(request: Request, year: int | None = None,
                     window: str = '90d', weeks: int = 12):
    overview = solar_overview()

    # Monthly breakdown — restricts to months with real solar data
    months = [m for m in solar_monthly_full() if m['produced'] > 0]

    # Multi-year overlay (drop months with no production for clarity)
    yoy = [r for r in solar_yoy_monthly() if r['produced'] > 0]
    yoy_years = sorted({r['year'] for r in yoy})

    # Heatmap year picker
    years_with_solar = sorted({int(m['month'][:4]) for m in months})
    if year is None or year not in years_with_solar:
        year = years_with_solar[-1] if years_with_solar else today_local().year
    daily = solar_daily_for_year(year)

    # Best solar days table + duck curve
    best = best_solar_days(window, limit=15)
    duck = solar_hour_of_day(weeks)

    scatter   = daily_produced_vs_consumed()
    cf        = capacity_factor()
    year_end_forecast = year_end_production_forecast()

    return templates.TemplateResponse(
        request=request,
        name='solar.html',
        context={
            **_common_ctx('/solar'),
            'overview':       overview,
            'months':         months,
            'months_json':    json.dumps(months),
            'yoy_years':      yoy_years,
            'yoy_json':       json.dumps(yoy),
            'years_with_solar': years_with_solar,
            'selected_year':  year,
            'daily_json':     json.dumps(daily),
            'window':         window,
            'best_days':      best,
            'weeks':          weeks,
            'duck_json':      json.dumps(duck),
            'scatter_json':   json.dumps(scatter),
            'cf':             cf,
            'year_end':       year_end_forecast,
        },
    )


# ---- Anomaly history -------------------------------------------------------

@app.get('/anomalies', response_class=HTMLResponse)
async def anomalies_page(request: Request, window: int = 90, threshold: float = 3.0):
    rows = anomaly_history(window_days=window, threshold=threshold)
    return templates.TemplateResponse(
        request=request,
        name='anomalies.html',
        context={
            **_common_ctx('/anomalies'),
            'rows': rows,
            'window': window,
            'threshold': threshold,
        },
    )


# ---- Biggest days ----------------------------------------------------------

@app.get('/biggest', response_class=HTMLResponse)
async def biggest_page(request: Request, window: str = '90d',
                       circuit: str = WHOLE_HOUSE,
                       metric: str = 'consumption'):
    # Per-circuit selection only ranks by consumption — production/export/import
    # are panel-level concepts. Snap back silently if someone deep-links a
    # nonsense combo (e.g. /biggest?circuit=fridge&metric=production).
    if circuit != WHOLE_HOUSE and metric != 'consumption':
        metric = 'consumption'
    if metric not in ('consumption', 'production', 'export', 'import'):
        metric = 'consumption'
    rows = top_days(window, circuit, limit=25, metric=metric)
    return templates.TemplateResponse(
        request=request,
        name='biggest.html',
        context={
            **_common_ctx('/biggest'),
            'circuits': list_circuits(),
            'selected_circuit': circuit,
            'window': window,
            'metric': metric,
            'rows': rows,
        },
    )


# ---- Day view --------------------------------------------------------------

@app.get('/day', response_class=HTMLResponse)
async def day_page(request: Request, date: str | None = None,
                   circuit: str = WHOLE_HOUSE):
    if date is None:
        date = today_local().isoformat()
    try:
        current = datetime.date.fromisoformat(date)
    except ValueError:
        current = today_local()
        date = current.isoformat()

    today  = today_local()
    prev_d = (current - datetime.timedelta(days=1)).isoformat()
    next_d = (current + datetime.timedelta(days=1)).isoformat()
    prev_w = (current - datetime.timedelta(days=7)).isoformat()
    next_w = (current + datetime.timedelta(days=7)).isoformat()

    payload = day_curve(date, circuit)
    prior_years = same_day_prior_years(date, circuit)
    # When the whole house is selected, also break down by circuit so the user
    # can see what was actually running.
    per_circuit = day_per_circuit(date) if circuit == WHOLE_HOUSE else None
    # Month-to-date tiles for whole-house only (the math is panel-level).
    same_month = same_month_prior_years(date) if circuit == WHOLE_HOUSE else None
    return templates.TemplateResponse(
        request=request,
        name='day.html',
        context={
            **_common_ctx('/day'),
            'circuits': list_circuits(),
            'selected_circuit': circuit,
            'date': date,
            'today_date': today.isoformat(),
            'day': payload,
            'day_json': json.dumps(payload['series']),
            'granularity': payload['granularity'],
            'prior_years': prior_years,
            'same_month': same_month,
            'per_circuit': per_circuit,
            'per_circuit_rows_json': json.dumps(per_circuit['rows']) if per_circuit else 'null',
            'prev_date': prev_d,
            'next_date': next_d,
            'prev_week_date': prev_w,
            'next_week_date': next_w,
            'next_is_future': datetime.date.fromisoformat(next_d) > today,
            'next_week_is_future': datetime.date.fromisoformat(next_w) > today,
        },
    )


# ---- Alerts log ------------------------------------------------------------

@app.get('/alerts', response_class=HTMLResponse)
async def alerts_page(request: Request, limit: int = 100):
    rows = recent_alerts(limit=limit)
    return templates.TemplateResponse(
        request=request,
        name='alerts.html',
        context={**_common_ctx('/alerts'), 'rows': rows, 'limit': limit},
    )


# ---- Health dashboard ------------------------------------------------------

@app.get('/health', response_class=HTMLResponse)
async def health_page(request: Request):
    snap = health_snapshot()
    now = datetime.datetime.now(datetime.timezone.utc)
    return templates.TemplateResponse(
        request=request,
        name='health.html',
        context={**_common_ctx('/health'), 'snap': snap, 'now': now},
    )


@app.get('/health/vm-check', response_class=HTMLResponse)
def health_vm_check(request: Request, days: int = 7):
    return templates.TemplateResponse(
        request=request,
        name='_vm_check.html',
        context={'check': db_vs_vm_check(max(1, min(days, 30)))},
    )


# ---- Energy-flow Sankey ----------------------------------------------------

@app.get('/flows', response_class=HTMLResponse)
async def flows_page(request: Request, window: str = '12mo'):
    flows = energy_flows(window)
    mismatch = solar_vs_consumption_hourly(12)
    phantom  = phantom_load_daily(90)
    return templates.TemplateResponse(
        request=request,
        name='flows.html',
        context={
            **_common_ctx('/flows'),
            'window':         window,
            'flows':          flows,
            'flows_json':     json.dumps(flows),
            'mismatch_json':  json.dumps(mismatch),
            'phantom_json':   json.dumps(phantom),
        },
    )


# ---- EV sessions -----------------------------------------------------------

@app.get('/sessions', response_class=HTMLResponse)
async def sessions_page(request: Request, days: int = 90):
    sessions = ev_charging_sessions(days=days)
    return templates.TemplateResponse(
        request=request,
        name='sessions.html',
        context={
            **_common_ctx('/sessions'),
            'days': days,
            'sessions': sessions,
        },
    )


# ---- Appliance sessions ----------------------------------------------------

@app.get('/appliances', response_class=HTMLResponse)
async def appliances_page(request: Request, days: int = 30, only: str = ''):
    """Sessionized view of high-load appliances. `only=<key>` filters to one
    appliance; the summary tiles always show all configured appliances."""
    data = all_appliance_sessions(days=days)
    sessions = data['sessions']
    only_key = only.strip().lower()
    valid_keys = {p['key'] for p in APPLIANCE_PROFILES}
    if only_key and only_key in valid_keys:
        # When filtered to a single appliance, re-fetch the full session list
        # so high_cycle appliances (which are skipped from the combined view)
        # still get their own detail.
        sessions = appliance_sessions(only_key, days=days)
    else:
        only_key = ''

    # Daily-stats panel for cyclical appliances when filtered to them
    daily_stats = None
    if only_key:
        profile = next((p for p in APPLIANCE_PROFILES if p['key'] == only_key), None)
        if profile and profile.get('high_cycle'):
            daily_stats = appliance_daily_stats(only_key, days=days)

    return templates.TemplateResponse(
        request=request,
        name='appliances.html',
        context={
            **_common_ctx('/appliances'),
            'days':        days,
            'only':        only_key,
            'profiles':    APPLIANCE_PROFILES,
            'summary':     data['summary'],
            'sessions':    sessions,
            'daily_stats': daily_stats,
        },
    )


# ---- Load discovery (NILM) -------------------------------------------------

@app.get('/discover', response_class=HTMLResponse)
async def discover_page(request: Request, days: int = 7,
                         circuit: str = '',
                         min_step_w: float = 200.0):
    """NILM-style load discovery from the 3-s per-circuit space_live stream.

    Without `circuit`: overview row per circuit (event count, top cluster).
    With `circuit=<space_id>`: full event list + amplitude-clustered
    hypotheses for that one circuit."""
    detail = None
    overview = None
    if circuit:
        detail = discovered_loads_for_circuit(circuit, days=days,
                                              min_step_w=min_step_w)
    else:
        overview = discovered_loads_overview(days=days, min_step_w=min_step_w)
    return templates.TemplateResponse(
        request=request,
        name='discover.html',
        context={
            **_common_ctx('/discover'),
            'days':        days,
            'circuit':     circuit,
            'min_step_w':  min_step_w,
            'overview':    overview,
            'detail':      detail,
            'circuits':    list_circuits(),
        },
    )


# ---- AC cycling detector (Phase 1.5.3) -------------------------------------

@app.get('/discover/ac', response_class=HTMLResponse)
async def discover_ac_page(request: Request, days: int = 7,
                            min_step_w: float = 200.0):
    runs = detect_thermostat_cycling_all(days=days, min_step_w=min_step_w)
    return templates.TemplateResponse(
        request=request,
        name='discover_ac.html',
        context={
            **_common_ctx('/discover/ac'),
            'days':       days,
            'min_step_w': min_step_w,
            'runs':       runs,
        },
    )


# ---- Co-occurrence mining (Phase 1.5.4) ------------------------------------

@app.get('/discover/cooccur', response_class=HTMLResponse)
async def discover_cooccur_page(request: Request, days: int = 7,
                                  min_events: int = 5):
    pairs = cluster_cooccurrence(days=days, min_events=min_events)
    return templates.TemplateResponse(
        request=request,
        name='discover_cooccur.html',
        context={
            **_common_ctx('/discover/cooccur'),
            'days':       days,
            'min_events': min_events,
            'pairs':      pairs,
        },
    )


# ---- Per-cluster history (Phase 1.5.5) -------------------------------------

@app.get('/discover/cluster', response_class=HTMLResponse)
async def discover_cluster_page(request: Request, circuit: str = '',
                                  amplitude: int = 0,
                                  duration: str = '',
                                  days: int = 30,
                                  min_step_w: float = 200.0):
    if not circuit or amplitude <= 0:
        return RedirectResponse(url='/discover', status_code=302)
    detail = cluster_event_history(
        space_id=circuit, amplitude_bin_w=amplitude,
        duration_label=duration, days=days, min_step_w=min_step_w)
    return templates.TemplateResponse(
        request=request,
        name='discover_cluster.html',
        context={
            **_common_ctx('/discover'),
            'days':       days,
            'min_step_w': min_step_w,
            'circuit':    circuit,
            'amplitude':  amplitude,
            'duration':   duration,
            'detail':     detail,
        },
    )


# ---- FHMM disaggregation / attribution (Phase 2) ---------------------------

@app.get('/attribute', response_class=HTMLResponse)
async def attribute_page(request: Request, circuit: str = '',
                          hours: int = 24,
                          min_step_w: float = 200.0,
                          obs_noise_w: float = 50.0):
    result = None
    if circuit:
        # Import here so the page renders even if numpy is missing
        # (the module emits an informative `error` field in that case).
        from .nilm_fhmm import fhmm_attribute_circuit
        result = fhmm_attribute_circuit(
            space_id=circuit, hours=hours,
            min_step_w=min_step_w, obs_noise_w=obs_noise_w)
    return templates.TemplateResponse(
        request=request,
        name='attribute.html',
        context={
            **_common_ctx('/attribute'),
            'hours':      hours,
            'circuit':    circuit,
            'min_step_w': min_step_w,
            'obs_noise_w': obs_noise_w,
            'circuits':   list_live_circuits(),
            'result':     result,
        },
    )


# ---- Data export -----------------------------------------------------------

def _csv_response(filename: str, fieldnames: list[str], rows: list[dict]):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore')
    w.writeheader()
    for r in rows:
        # Coerce non-JSON-serializable values (datetimes/dates) to strings.
        clean = {}
        for k in fieldnames:
            v = r.get(k)
            if hasattr(v, 'isoformat'):
                clean[k] = v.isoformat()
            else:
                clean[k] = v
        w.writerow(clean)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type='text/csv',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.get('/export/top')
async def export_top(window: str = 'all', fmt: str = 'csv'):
    rows = top_consumers(window)
    if fmt == 'json':
        return JSONResponse(rows)
    return _csv_response(f'top-{window}.csv', ['name', 'kwh', 'share'], rows)


@app.get('/export/best-days')
async def export_best_days(window: str = '12mo', fmt: str = 'csv'):
    rows = top_days(window, WHOLE_HOUSE, limit=1000)
    if fmt == 'json':
        return JSONResponse(rows)
    return _csv_response(f'best-days-{window}.csv', ['date', 'kwh', 'dow', 'day_type'], rows)


@app.get('/export/solar-monthly')
async def export_solar_monthly(fmt: str = 'csv'):
    rows = solar_monthly_full()
    if fmt == 'json':
        return JSONResponse(rows)
    return _csv_response(
        'solar-monthly.csv',
        ['month', 'produced', 'consumed', 'exported', 'house_total', 'from_grid', 'self_pct', 'offset_pct'],
        rows,
    )


@app.get('/export/sessions')
async def export_sessions(days: int = 365, fmt: str = 'csv'):
    rows = ev_charging_sessions(days=days)
    if fmt == 'json':
        return JSONResponse(rows)
    return _csv_response(
        f'ev-sessions-{days}d.csv',
        ['start', 'end', 'date', 'duration_min', 'kwh', 'avg_kw', 'peak_kw', 'space_name'],
        rows,
    )


@app.get('/export/appliances')
async def export_appliances(days: int = 90, only: str = '', fmt: str = 'csv'):
    only_key = only.strip().lower()
    valid = {p['key'] for p in APPLIANCE_PROFILES}
    if only_key and only_key in valid:
        # Re-fetch so high_cycle appliances (fridge) — excluded from the
        # combined view — still get a full session list when filtered to.
        rows = appliance_sessions(only_key, days=days)
        filename = f'appliances-{only_key}-{days}d.csv'
    else:
        rows = all_appliance_sessions(days=days)['sessions']
        filename = f'appliances-{days}d.csv'
    if fmt == 'json':
        return JSONResponse(rows)
    return _csv_response(
        filename,
        ['appliance', 'label', 'start', 'end', 'date', 'duration_min', 'kwh',
         'avg_kw', 'peak_kw', 'secondary_kwh', 'secondary_min', 'space_name'],
        rows,
    )


@app.get('/export/anomalies')
async def export_anomalies(window: int = 365, threshold: float = 3.0, fmt: str = 'csv'):
    rows = anomaly_history(window_days=window, threshold=threshold)
    if fmt == 'json':
        return JSONResponse(rows)
    return _csv_response(
        f'anomalies-{window}d-{threshold}x.csv',
        ['date', 'space_name', 'space_id', 'kwh', 'baseline', 'ratio'],
        rows,
    )


# ---- Duty-cycle circuits (circuits.yaml) -----------------------------------

@app.get('/duty', response_class=HTMLResponse)
async def duty_page(request: Request, days: int = 30):
    days = max(1, min(days, 730))
    circuits = duty_cycle_overview(days=days)
    return templates.TemplateResponse(
        request=request,
        name='duty.html',
        context={
            **_common_ctx('/duty'),
            'days': days,
            'circuits': circuits,
            'circuits_json': json.dumps([
                {'key': c['key'], 'label': c['label'], 'glyph': c['glyph'],
                 'days': list(reversed(c['days']))} for c in circuits]),
        },
    )


# ---- The dehumidifier (duty_cycle key `dehumidifier`), month by month -------

@app.get('/dehumidifier', response_class=HTMLResponse)
async def dehumidifier_page(request: Request, days: int = 60):
    data = dehumidifier_overview(days=days)
    return templates.TemplateResponse(
        request=request,
        name='dehumidifier.html',
        context={
            **_common_ctx('/dehumidifier'),
            'days': days,
            'd': data,
            'daily_json': json.dumps(data['daily']),
        },
    )


# ---- Hour-of-day pattern ---------------------------------------------------

@app.get('/hours', response_class=HTMLResponse)
async def hours_page(request: Request, circuit: str = WHOLE_HOUSE,
                     weeks: int = 12):
    rows = hourly_pattern(circuit, weeks)
    return templates.TemplateResponse(
        request=request,
        name='hours.html',
        context={
            **_common_ctx('/hours'),
            'circuits': list_circuits(),
            'selected_circuit': circuit,
            'weeks': weeks,
            'rows_json': json.dumps(rows),
        },
    )
