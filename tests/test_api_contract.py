"""The README's API v1 contract, the exports, and what the pages do with the
flat rate. With a fake database: the app runs without its lifespan, so no pool,
poller or MQTT client starts."""

import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import api as api_module
from app import main
from app import queries as q

TODAY = datetime.date(2026, 10, 17)
HEALTH = {'last_run': None, 'latest_ts': None}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(q, 'today_local', lambda: TODAY)
    monkeypatch.setattr(main, 'ingest_health', lambda: HEALTH)
    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def daily_range(monkeypatch):
    """Records the range /panel/daily asks for."""
    seen = {}

    def panel_daily_local(start, end):
        seen.update(start=start, end=end)
        return []

    monkeypatch.setattr(q, 'panel_daily_local', panel_daily_local)
    return seen


def d(iso):
    return datetime.date.fromisoformat(iso)


# ---- GET /api/v1/panel/daily ------------------------------------------------

def test_panel_daily_defaults_to_the_last_30_days(api, daily_range):
    r = api.get('/api/v1/panel/daily')
    assert r.status_code == 200
    body = r.json()
    assert (body['start'], body['end']) == ('2026-09-18', '2026-10-17')
    assert body['timezone'] == q.TZ
    assert daily_range == {'start': d('2026-09-18'), 'end': TODAY}


def test_panel_daily_start_defaults_from_a_given_end(api, daily_range):
    body = api.get('/api/v1/panel/daily?end=2026-03-31').json()
    assert (body['start'], body['end']) == ('2026-03-02', '2026-03-31')


def test_panel_daily_clips_a_later_end_to_today(api, daily_range):
    body = api.get('/api/v1/panel/daily?start=2026-10-01&end=2027-01-01').json()
    assert (body['start'], body['end']) == ('2026-10-01', '2026-10-17')
    assert daily_range['end'] == TODAY


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'app/api.py:168: the default start comes from end before it is clipped to today, so a '
    'future end alone narrows the range, or, past 29 days ahead, answers 400 "start is after '
    'end" to a caller who sent no start'))
@pytest.mark.parametrize('end', ['2026-10-27', '2030-01-01'])
def test_panel_daily_default_start_follows_the_clipped_end(api, daily_range, end):
    r = api.get(f'/api/v1/panel/daily?end={end}')
    assert r.status_code == 200
    assert (r.json()['start'], r.json()['end']) == ('2026-09-18', '2026-10-17')


def test_panel_daily_one_day(api, daily_range):
    body = api.get('/api/v1/panel/daily?start=2026-10-16&end=2026-10-16').json()
    assert (body['start'], body['end']) == ('2026-10-16', '2026-10-16')


@pytest.mark.parametrize('query', ['start=yesterday', 'end=2026-13-01', 'start=2026-02-30'])
def test_panel_daily_bad_date_is_a_400(api, daily_range, query):
    r = api.get(f'/api/v1/panel/daily?{query}')
    assert r.status_code == 400
    assert isinstance(r.json()['detail'], str)


def test_panel_daily_start_after_end_is_a_400(api, daily_range):
    r = api.get('/api/v1/panel/daily?start=2026-10-02&end=2026-10-01')
    assert r.status_code == 400
    assert r.json() == {'detail': 'start is after end'}


def test_panel_daily_range_limit(api, daily_range):
    end = d('2026-10-01')
    at_limit = end - datetime.timedelta(days=3659)
    assert api.get(f'/api/v1/panel/daily?start={at_limit}&end={end}').status_code == 200
    over = at_limit - datetime.timedelta(days=1)
    r = api.get(f'/api/v1/panel/daily?start={over}&end={end}')
    assert r.status_code == 400
    assert r.json() == {'detail': 'range over 3660 days'}


def test_panel_daily_row_has_the_readme_fields(api, fake_db):
    readme_row = {'date': '2026-10-16', 'import_kwh': 43.13, 'export_kwh': 0.2, 'solar_kwh': 5.59,
                  'load_kwh': 48.52, 'coverage': 1.0, 'resolution_s': 600}
    fake_db([readme_row])
    body = api.get('/api/v1/panel/daily?start=2026-10-16&end=2026-10-16').json()
    assert set(body) == {'timezone', 'start', 'end', 'days'}
    assert body['days'] == [readme_row]
    # The README's identity, on its own example.
    row = body['days'][0]
    assert row['import_kwh'] - row['export_kwh'] + row['solar_kwh'] == pytest.approx(row['load_kwh'])


# ---- The rest of v1 ---------------------------------------------------------

def test_the_index_lists_every_endpoint():
    index = {e['path'] for e in TestClient(main.app).get('/api/v1').json()['endpoints']}
    routes = {r.path.replace('{circuit_id}', '{id}') for r in api_module.router.routes}
    assert index == routes - {'/api/v1'}


def test_docs_are_served():
    client = TestClient(main.app)
    assert '/api/v1/panel/daily' in client.get('/openapi.json').json()['paths']
    assert client.get('/docs').status_code == 200


def test_top_bad_window_is_a_400(api):
    r = api.get('/api/v1/top?window=forever')
    assert r.status_code == 400
    assert isinstance(r.json()['detail'], str)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "app/api.py:199: /top-days doesn't catch top_days()'s ValueError as /top does, so a bad "
    'window is a 500 (README: bad parameters get a 400 with {"detail": "..."})'))
def test_top_days_bad_window_is_a_400(api):
    r = api.get('/api/v1/top-days?window=forever')
    assert r.status_code == 400
    assert isinstance(r.json()['detail'], str)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "app/api.py:196: FastAPI answers a non-numeric int parameter with its own 422 and a list "
    'in detail (README: bad parameters get a 400 with {"detail": "..."})'))
def test_non_numeric_parameter_is_a_400(api):
    r = api.get('/api/v1/top-days?limit=lots')
    assert r.status_code == 400
    assert isinstance(r.json()['detail'], str)


def test_unknown_circuit_is_a_404(api, fake_db):
    fake_db([])
    r = api.get('/api/v1/circuits/no-such-circuit/yoy')
    assert r.status_code == 404
    assert r.json() == {'detail': 'unknown circuit_id: no-such-circuit'}


def test_circuit_day_bad_date_is_a_400(api):
    r = api.get('/api/v1/circuits/whole_house/day?date=soon')
    assert r.status_code == 400
    assert r.json() == {'detail': 'bad date: soon'}


def test_circuit_day_and_year_default_to_today(api, monkeypatch):
    seen = {}
    monkeypatch.setattr(q, 'day_curve', lambda date, cid: seen.update(day=date) or {})
    monkeypatch.setattr(q, 'daily_kwh_for_year', lambda year, cid: seen.update(year=year) or [])
    assert api.get('/api/v1/circuits/whole_house/day').status_code == 200
    assert api.get('/api/v1/circuits/whole_house/daily').status_code == 200
    assert seen == {'day': '2026-10-17', 'year': 2026}


def test_duty_cycle_days_are_clamped(api, monkeypatch):
    seen = []
    monkeypatch.setattr(q, 'duty_cycle_overview', lambda days: seen.append(days) or [])
    api.get('/api/v1/duty-cycle?days=0')
    api.get('/api/v1/duty-cycle?days=99999')
    api.get('/api/v1/duty-cycle')
    assert seen == [1, 3660, 30]


def test_live_serves_the_cached_snapshot_with_its_age(api, monkeypatch):
    monkeypatch.setattr(api_module, 'latest_snapshot', lambda: {'error': None, 'panel': {'x': 1}})
    monkeypatch.setattr(api_module, 'latest_snapshot_age_s', lambda: 1.5)
    assert api.get('/api/v1/live').json() == {'error': None, 'panel': {'x': 1}, 'cached_at_s': 1.5}


def test_live_before_the_first_tick_asks_the_source(api, monkeypatch):
    monkeypatch.setattr(api_module, 'latest_snapshot', lambda: None)
    monkeypatch.setattr(api_module, 'source_snapshot', lambda: {'error': 'not yet', 'panel': None})
    assert api.get('/api/v1/live').json() == {'error': 'not yet', 'panel': None}


def test_live_history_fields_and_fallback(fake_db):
    ts = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
    fake_db([{'ts': ts, 'grid_w': -100.0, 'solar_w': 900.0, 'load_w': 800.0, 'feedthrough_w': 50.0}])
    h = q.live_history('not-a-window')
    assert (h['window'], h['period'], h['bucket']) == ('5m', '5 minutes', '3 seconds')
    assert h['samples'] == [{'ts': '2026-03-04T12:00:00+00:00', 'grid_w': -100.0, 'solar_w': 900.0,
                             'load_w': 800.0, 'feedthrough_w': 50.0}]


def test_jsonable():
    when = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=datetime.timezone.utc)
    assert api_module._jsonable({
        'a': Decimal('1.25'), 'b': [datetime.date(2026, 3, 4), (when, None)], 'c': True, 'd': 3,
    }) == {'a': 1.25, 'b': ['2026-03-04', ['2026-03-04T12:00:00+00:00', None]], 'c': True, 'd': 3}


# ---- Exports ----------------------------------------------------------------

def test_csv_export_quotes_names_and_keeps_full_precision(api, monkeypatch):
    monkeypatch.setattr(main, 'top_consumers', lambda window: [
        {'name': 'Kitchen, Outlets', 'kwh': 1234.5678, 'share': 0.25}])
    r = api.get('/export/top?window=30d&fmt=csv')
    assert r.text.splitlines() == ['name,kwh,share', '"Kitchen, Outlets",1234.5678,0.25']
    assert r.headers['content-type'].startswith('text/csv')
    assert 'filename="top-30d.csv"' in r.headers['content-disposition']


def test_json_export_is_the_same_rows(api, monkeypatch):
    rows = [{'name': 'Dryer', 'kwh': 1.5, 'share': 1.0}]
    monkeypatch.setattr(main, 'top_consumers', lambda window: rows)
    assert api.get('/export/top?fmt=json').json() == rows


def test_csv_blanks_a_missing_field_and_formats_dates(api, monkeypatch):
    monkeypatch.setattr(main, 'all_appliance_sessions', lambda days: {'sessions': [
        {'appliance': 'dryer', 'label': 'Dryer', 'start': datetime.date(2026, 3, 4), 'kwh': 2.0}]})
    lines = api.get('/export/appliances?fmt=csv').text.splitlines()
    assert lines[0] == ('appliance,label,start,end,date,duration_min,kwh,avg_kw,peak_kw,'
                        'secondary_kwh,secondary_min,space_name')
    assert lines[1] == 'dryer,Dryer,2026-03-04,,,,2.0,,,,,'


# ---- The flat rate on the pages ---------------------------------------------

@pytest.fixture
def flat_rate(monkeypatch):
    def use(imp=None, credit=None):
        monkeypatch.delenv('RATE_IMPORT_PER_KWH', raising=False)
        monkeypatch.delenv('RATE_EXPORT_CREDIT_PER_KWH', raising=False)
        if imp is not None:
            monkeypatch.setenv('RATE_IMPORT_PER_KWH', str(imp))
        if credit is not None:
            monkeypatch.setenv('RATE_EXPORT_CREDIT_PER_KWH', str(credit))
    return use


def test_live_page_rate(api, flat_rate):
    flat_rate(0.25)
    assert 'const COST_PER_KWH = 0.25;' in api.get('/live').text
    flat_rate()
    assert 'const COST_PER_KWH = null;' in api.get('/live').text


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "app/templates/live.html:449: the grid tile prices exports at COST_PER_KWH, the import "
    "rate; RATE_EXPORT_CREDIT_PER_KWH reaches no page, so exporting at a lower credit "
    "overstates the $/hr"))
def test_live_page_knows_the_export_credit(api, flat_rate):
    flat_rate(0.30, 0.0873)
    assert '0.0873' in api.get('/live').text


DEHUMIDIFIER = {
    'daily': [], 'rate': None, 'last30_days': 30, 'last30_kwh': 60.0, 'last30_cost': None,
    'last30_duty_pct': 40.0, 'on_kw': 0.2, 'label': 'Dehumidifier', 'since': None,
    'configured': True,
    'monthly': [{'month': '2026-02', 'kwh': 30.0, 'days': 15, 'cost': None, 'cost_per_30d': None}],
}


def test_dehumidifier_page_prices_with_a_rate(api, monkeypatch):
    monkeypatch.setattr(main, 'dehumidifier_overview', lambda days: {
        **DEHUMIDIFIER, 'rate': 0.25, 'last30_cost': 15.0,
        'monthly': [{**DEHUMIDIFIER['monthly'][0], 'cost': 7.5, 'cost_per_30d': 15.0}]})
    html = api.get('/dehumidifier').text
    assert '$15</div>' in html
    assert 'at the flat $0.2500/kWh' in html
    assert '$7.50' in html


def test_dehumidifier_page_has_no_dollars_without_a_rate(api, monkeypatch):
    monkeypatch.setattr(main, 'dehumidifier_overview', lambda days: DEHUMIDIFIER)
    html = api.get('/dehumidifier').text
    assert 'Cost, last' not in html
    assert '<th class="num">$</th>' not in html
    assert 'at the flat' not in html


def fridge_page(monkeypatch):
    profile = {'key': 'fridge', 'label': 'Refrigerator', 'glyph': '★', 'color': 'orange',
               'high_cycle': True}
    monkeypatch.setattr(main, 'APPLIANCE_PROFILES', [profile])
    monkeypatch.setattr(main, 'all_appliance_sessions', lambda days: {'sessions': [], 'summary': [
        {**profile, 'count': 0, 'threshold_kw': 0.05, 'since_date': None}]})
    monkeypatch.setattr(main, 'appliance_sessions', lambda key, days: [])
    monkeypatch.setattr(main, 'appliance_daily_stats', lambda key, days: {
        'days': 30, 'total_kwh': 30.0, 'avg_kwh_day': 1.0, 'avg_duty_pct': 40.0,
        'avg_active_min_day': 576.0, 'since_date': None, 'daily': []})


def test_appliance_projection_is_priced_from_kwh_per_day(api, monkeypatch, flat_rate):
    fridge_page(monkeypatch)
    flat_rate(0.5)
    html = api.get('/appliances?only=fridge').text
    assert '≈ $15.20/mo @ 0.5000 $/kWh' in html       # 1 kWh/day x 30.4 x $0.50
    assert '≈ $182/yr' in html                         # 1 kWh/day x 365 x $0.50


def test_appliance_projection_has_no_dollars_without_a_rate(api, monkeypatch, flat_rate):
    fridge_page(monkeypatch)
    flat_rate()
    html = api.get('/appliances?only=fridge').text
    assert '$/kWh' not in html
    assert '/yr</div>' not in html


def cluster_page(monkeypatch):
    monkeypatch.setattr(main, 'cluster_event_history', lambda **_kw: {
        'space_id': 's', 'space_name': 'Kitchen', 'amplitude_bin_w': 1000, 'duration_label': '',
        'events': [], 'hour_dist': [0] * 24, 'dow_dist': [0] * 7, 'daily_kwh': [],
        'cluster': {'avg_amplitude_w': 1000.0, 'peak_amplitude_w': 1100.0,
                    'median_duration_min': 3.0, 'total_kwh': 12.5, 'total_min': 750.0,
                    'hypothesis': 'kettle'}})


def test_cluster_cost(api, monkeypatch, flat_rate):
    cluster_page(monkeypatch)
    flat_rate(0.5)
    assert '$6.25' in api.get('/discover/cluster?circuit=s&amplitude=1000').text
    flat_rate()
    assert 'Cost @' not in api.get('/discover/cluster?circuit=s&amplitude=1000').text
