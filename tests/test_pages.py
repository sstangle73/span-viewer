"""Smoke test: every page and API endpoint answers 200 on the sample data."""

import datetime

import pytest

from app.main import NAV

PAGES = sorted({path for _, items in NAV for path, _ in items} | {
    '/yoy/chart?circuit=whole_house',
    '/day?date=' + datetime.date.today().isoformat(),
    '/appliances?only=dryer',
    '/appliances?only=fridge',
    '/duty?days=90',
    '/dehumidifier',
    '/discover?circuit=sample0000000000000000000000fridge',
    '/attribute?circuit=__feedthrough__',
    '/manifest.webmanifest',
    '/sw.js',
})

API = [
    '/api/v1', '/api/v1/live', '/api/v1/live/history?window=1h', '/api/v1/circuits',
    '/api/v1/panel/daily', '/api/v1/circuits/whole_house/yoy',
    '/api/v1/top?window=30d', '/api/v1/top-days', '/api/v1/solar/overview',
    '/api/v1/solar/monthly', '/api/v1/solar/year-end', '/api/v1/sessions',
    '/api/v1/anomalies', '/api/v1/alerts', '/api/v1/duty-cycle', '/api/v1/house/dehumidifier',
    '/api/v1/health',
    '/api/v1/health/vm-check',
    '/export/top?fmt=csv', '/export/appliances?fmt=json',
]


@pytest.mark.parametrize('path', PAGES)
def test_page(client, path):
    r = client.get(path, follow_redirects=True)
    assert r.status_code == 200, f'{path}: {r.status_code}\n{r.text[:2000]}'


@pytest.mark.parametrize('path', API)
def test_api(client, path):
    r = client.get(path)
    assert r.status_code == 200, f'{path}: {r.status_code}\n{r.text[:2000]}'


def test_sample_data_is_there(client):
    days = client.get('/api/v1/panel/daily').json()['days']
    assert any((d['load_kwh'] or 0) > 5 for d in days)
    assert any((d['solar_kwh'] or 0) > 1 for d in days)


def test_ev_sessions(client):
    assert client.get('/api/v1/sessions?days=60').json(), 'the sample EV charger should have sessions'


def test_duty_cycle(client):
    rows = client.get('/api/v1/duty-cycle?days=20').json()
    assert {r['key'] for r in rows} == {'dehumidifier', 'furnace'}
    furnace = next(r for r in rows if r['key'] == 'furnace')
    assert furnace['space_id'] and furnace['days']
    assert any(d['hours_on'] for d in furnace['days'])


def test_duty_cycle_minutes_follow_energy(client):
    """With standby_w and on_w, a day's minutes are its energy above standby
    over the running draw, not whole 10-minute buckets."""
    rows = client.get('/api/v1/duty-cycle?days=20').json()
    furnace = next(r for r in rows if r['key'] == 'furnace')
    standby, net = furnace['standby_w'] / 1000, (furnace['on_w'] - furnace['standby_w']) / 1000
    fine = [d for d in furnace['days'][1:] if d['method'] == '10-min buckets' and d['coverage'] == 1]
    assert fine, 'the sample furnace should have full days of 10-minute buckets'
    for d in fine:
        assert d['minutes_on'] == pytest.approx(d['hours_on'] * 60)
        expected = max(d['kwh'] - standby * 24, 0) / net * 60
        # Per-bucket clipping at standby and at 10 minutes only trims a little.
        assert d['minutes_on'] == pytest.approx(expected, rel=0.1, abs=5)
    assert any(d['minutes_on'] % 10 for d in fine), 'minutes should not all be whole buckets'
    assert all(d['starts'] is not None for d in fine)


def test_vm_check_off_without_vm_url(client):
    assert client.get('/api/v1/health/vm-check').json()['enabled'] is False


# Removed before the first public release: they needed utility bills and weather.
GONE = [
    '/weather', '/bills', '/reconciliation', '/counterfactual', '/boiler',
    '/export/weather-daily', '/api/v1/rates', '/api/v1/bills', '/api/v1/bills/reconciliation',
    '/api/v1/solar/forecast', '/api/v1/solar/sizing', '/api/v1/solar/eom-forecast',
    '/api/v1/house/boiler',
]


@pytest.mark.parametrize('path', GONE)
def test_cross_source_routes_gone(client, path):
    assert client.get(path, follow_redirects=False).status_code == 404


def test_kwh_only_without_a_rate(client, monkeypatch):
    monkeypatch.delenv('RATE_IMPORT_PER_KWH', raising=False)
    assert 'const COST_PER_KWH = null;' in client.get('/live').text
    d = client.get('/api/v1/house/dehumidifier').json()
    assert d['rate'] is None and d['last30_cost'] is None
    assert all(m['cost'] is None for m in d['monthly'])
    assert 'Cost, last' not in client.get('/dehumidifier').text


def test_flat_rate_prices_kwh(client, monkeypatch):
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '0.25')
    assert 'const COST_PER_KWH = 0.25;' in client.get('/live').text
    d = client.get('/api/v1/house/dehumidifier').json()
    assert d['rate'] == 0.25
    assert d['last30_cost'] == pytest.approx(d['last30_kwh'] * 0.25)
    assert 'Cost, last' in client.get('/dehumidifier').text


def test_configured_redirects(client, monkeypatch):
    from app import main
    monkeypatch.setitem(main._REDIRECTS, '/weather', 'https://house.example/insights/solar/')
    r = client.get('/weather?x=1', follow_redirects=False)
    assert r.status_code == 301
    assert r.headers['location'] == 'https://house.example/insights/solar/?x=1'
    assert client.get('/live', follow_redirects=False).status_code == 200
