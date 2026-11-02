"""ingester/backfill.py and ongoing.py with a fake SPAN cloud and connection:
what's kept from a response, tier order, and how a failed run is recorded.
The upsert SQL itself needs a database: see test_db_energy.py."""

import datetime
import sys
from types import SimpleNamespace

import psycopg
import pytest
import requests

from ingester import backfill, ongoing

UTC = datetime.timezone.utc
DAY_START = '2026-03-04T05:00:00Z'


def m(value, start=DAY_START, duration='PT10M'):
    return {'value': value, 'startTime': start, 'duration': duration}


def series(*measurements):
    return {'measurements': list(measurements)}


def panel_body(**fields):
    """A getPanel response; `fields` are aliases, e.g. solar_mh=series(...)."""
    tops = {}
    for name, value in fields.items():
        top, alias = name.split('_', 1)
        tops.setdefault(top, {})[alias] = value
    return {'data': {'data': tops}}


def spaces_body(*spaces):
    return {'data': {'data': {'spaces': [
        {'spaceId': sid, 'name': name, 'measurementHistory': {'measurements': list(ms)}}
        for sid, name, ms in spaces]}}}


class FakeCloud:
    panel_id = 'panel-under-test'

    def __init__(self, body):
        self.body = body
        self.calls = []

    def gql(self, query, variables=None, op=None):
        self.calls.append((query, variables, op))
        return self.body


@pytest.fixture(autouse=True)
def no_future(monkeypatch):
    """Every startTime above is in the past as far as is_future() goes."""
    monkeypatch.setattr(backfill, '_NOW_ISO', '2026-03-05T00:00:00.000000Z')


@pytest.mark.parametrize('text, seconds', [
    ('PT10M', 600), ('PT30M', 1800), ('PT1H', 3600), ('PT1H30M', 5400), ('PT45S', 45),
    ('P1D', 86400), ('P1W', 604800), ('P1M', 2592000), ('P1Y', 31536000),
])
def test_iso_duration_to_seconds(text, seconds):
    assert backfill.iso_duration_to_seconds(text) == seconds


def test_unknown_duration_is_an_error_not_a_zero():
    with pytest.raises(KeyError):
        backfill.iso_duration_to_seconds('P2D')


def test_all_zero():
    assert backfill.all_zero([])
    assert backfill.all_zero([m(0), m(0.0), m(None)])
    assert not backfill.all_zero([m(0), m(0.001)])
    assert not backfill.all_zero([m(-0.001)])


def test_two_bucket_probe(monkeypatch):
    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 3, 4, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(backfill, 'datetime', SimpleNamespace(datetime=Frozen, timezone=datetime.timezone))
    today, earlier = '2026-03-04T00:00:00Z', '2026-03-03T00:00:00Z'
    probe = [m(1, today, 'PT30M'), m(2, today, 'PT30M')]
    assert backfill.is_2_bucket_probe(probe)
    assert not backfill.is_2_bucket_probe(probe + [m(3, today, 'PT30M')])
    assert not backfill.is_2_bucket_probe([m(1, today), m(2, today)])
    assert not backfill.is_2_bucket_probe([m(1, earlier, 'PT30M'), m(2, earlier, 'PT30M')])


def test_is_future():
    assert not backfill.is_future('2026-03-04T23:50:00Z')
    assert backfill.is_future('2026-03-05T00:10:00Z')
    assert not backfill.is_future('')


def test_panel_window_maps_every_series_to_its_metric():
    body = panel_body(
        solar_mh=series(m(1.0)), solar_toGrid=series(m(2.0)), grid_mh=series(m(3.0)),
        battery_ch=series(m(4.0)), battery_dc=series(m(5.0)),
        buildingConnection_bcTotal=series(m(6.0)), buildingConnection_bcSolar=series(m(7.0)),
        buildingConnection_bcBattery=series(m(8.0)), buildingConnection_bcGrid=series(m(9.0)))
    rows = backfill.fetch_panel_window(FakeCloud(body), 'DAY', 'PHONE_PORTRAIT', 0)
    assert {r[1]: r[2] for r in rows} == {
        'solar': 1.0, 'solar_to_grid': 2.0, 'grid_consuming': 3.0,
        'battery_charging': 4.0, 'battery_discharging': 5.0, 'building_total': 6.0,
        'building_from_solar': 7.0, 'building_from_battery': 8.0, 'building_from_grid': 9.0}


def test_panel_window_stores_kwh_as_a_magnitude():
    """SPAN's sign varies by series; a stored energy is never negative, and the
    value is kWh already, so it isn't scaled."""
    body = panel_body(grid_mh=series(m(-0.125, DAY_START, 'PT1H')))
    assert backfill.fetch_panel_window(FakeCloud(body), 'WEEK', 'PHONE_PORTRAIT', 2) == [
        (DAY_START, 'grid_consuming', 0.125, 3600)]


def test_panel_window_skips_nulls_and_future_placeholders():
    body = panel_body(solar_mh=series(
        m(1.5), m(None, '2026-03-04T05:10:00Z'), m(9.9, '2026-03-05T00:10:00Z')))
    rows = backfill.fetch_panel_window(FakeCloud(body), 'DAY', 'PHONE_PORTRAIT', 0)
    assert rows == [(DAY_START, 'solar', 1.5, 600)]


def test_panel_window_keeps_a_real_zero_in_a_live_series():
    body = panel_body(solar_mh=series(m(0.0), m(0.2, '2026-03-04T05:10:00Z')))
    rows = backfill.fetch_panel_window(FakeCloud(body), 'DAY', 'PHONE_PORTRAIT', 0)
    assert [r[2] for r in rows] == [0.0, 0.2]


def test_panel_window_drops_an_all_zero_series_alone():
    """Past retention SPAN answers zeros: no row, so never a false 0 kWh. It
    is per series, so the other series of that window survive."""
    body = panel_body(solar_mh=series(m(0), m(0)), grid_mh=series(m(1.0)))
    rows = backfill.fetch_panel_window(FakeCloud(body), 'DAY', 'PHONE_PORTRAIT', 0)
    assert [r[1] for r in rows] == ['grid_consuming']


def test_panel_window_query_error_loses_only_that_window(capsys):
    body = {'errors': [{'message': 'nope'}]}
    assert backfill.fetch_panel_window(FakeCloud(body), 'DAY', 'PHONE_PORTRAIT', 3) == []
    assert 'wb=3' in capsys.readouterr().err


def test_panel_window_sends_the_window_it_was_asked_for():
    cloud = FakeCloud({'data': {'data': None}})
    assert backfill.fetch_panel_window(cloud, 'MONTH', 'CALENDAR', 7) == []
    query, variables, op = cloud.calls[0]
    assert variables == {'panelId': 'panel-under-test', 'duration': 'MONTH', 'wb': 7}
    assert op == 'PA'
    assert query.count('resolution: CALENDAR') == 9


def test_spaces_window():
    body = spaces_body(('s1', 'Dryer', [m(-0.5)]), ('s2', 'Lights', [m(0.0)]))
    rows, meta = backfill.fetch_spaces_window(FakeCloud(body), 'DAY', 'PHONE_PORTRAIT', 0)
    # One idle circuit is real: only a window of nothing but zeros is padding.
    assert rows == [(DAY_START, 's1', 0.5, 600), (DAY_START, 's2', 0.0, 600)]
    assert meta == [('s1', 'Dryer', None), ('s2', 'Lights', None)]


def test_spaces_window_all_zero_keeps_the_names_only():
    body = spaces_body(('s1', 'Dryer', [m(0)]), ('s2', 'Lights', [m(0)]))
    rows, meta = backfill.fetch_spaces_window(FakeCloud(body), 'DAY', 'PHONE_PORTRAIT', 0)
    assert rows == []
    assert [x[0] for x in meta] == ['s1', 's2']


def test_spaces_window_error():
    assert backfill.fetch_spaces_window(
        FakeCloud({'errors': [{'message': 'nope'}]}), 'DAY', 'PHONE_PORTRAIT', 0) == ([], [])


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(backfill.time, 'sleep', sleeps.append)
    return sleeps


def test_panel_tiers_cover_retention_finest_first(monkeypatch, no_sleep):
    windows = []

    def window(_cloud, duration, resolution, wb):
        windows.append((duration, resolution, wb))
        return [(duration, wb)]

    monkeypatch.setattr(backfill, 'fetch_panel_window', window)
    rows = backfill.fetch_panel_all(object())
    assert windows == (
        [('DAY', 'PHONE_PORTRAIT', wb) for wb in range(31)]
        + [('WEEK', 'PHONE_PORTRAIT', wb) for wb in range(13)]
        + [('MONTH', 'CALENDAR', wb) for wb in range(1, 25)]
        + [('YEAR', 'CALENDAR', wb) for wb in range(backfill.TIER_D_YEARS + 1)])
    assert len(rows) == len(windows)


def test_spaces_tiers_keep_the_first_name_seen(monkeypatch, no_sleep):
    def window(_cloud, duration, _resolution, wb):
        name = 'New name' if duration == 'DAY' else 'Old name'
        return [(duration, wb)], [('s1', name, None)]

    monkeypatch.setattr(backfill, 'fetch_spaces_window', window)
    rows, meta = backfill.fetch_spaces_all(object())
    assert meta == [('s1', 'New name', None)]
    assert len(rows) == 31 + 13 + 24 + backfill.TIER_D_YEARS + 1


def cloud_for_gql():
    c = backfill.SpanCloud.__new__(backfill.SpanCloud)
    c.token = 'made-up-token'
    c._auth_at = backfill.time.time()
    return c


class Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def scripted_post(monkeypatch, *answers):
    calls = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        answer = answers[len(calls) - 1]
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)

    monkeypatch.setattr(backfill.requests, 'post', post)
    return calls


def test_gql_retries_spans_internal_errors(monkeypatch, no_sleep):
    ok = {'data': {'data': {}}}
    flaky = {'errors': [{'message': 'Internal server error'}]}
    calls = scripted_post(monkeypatch, flaky, flaky, ok)
    assert cloud_for_gql().gql('query', {'a': 1}, op='X') == ok
    assert len(calls) == 3
    assert no_sleep == [1.0, 1.5]


def test_gql_gives_a_real_error_back_at_once(monkeypatch, no_sleep):
    denied = {'errors': [{'message': 'Not authorized'}]}
    calls = scripted_post(monkeypatch, denied)
    assert cloud_for_gql().gql('query') == denied
    assert len(calls) == 1


def test_gql_raises_when_the_network_never_recovers(monkeypatch, no_sleep):
    down = requests.ConnectionError('refused')
    scripted_post(monkeypatch, down, down, down)
    with pytest.raises(requests.ConnectionError):
        cloud_for_gql().gql('query')


def test_gql_reauthenticates_an_old_token(monkeypatch):
    c = cloud_for_gql()
    c._auth_at = backfill.time.time() - 51 * 60
    renewed = []
    c._auth = lambda: (renewed.append(1), setattr(c, '_auth_at', backfill.time.time()))
    scripted_post(monkeypatch, {'data': {}})
    c.gql('query')
    assert renewed == [1]


def test_upserts_of_nothing_never_touch_the_database():
    assert backfill.bulk_upsert_panel(None, []) == 0
    assert backfill.bulk_upsert_space_readings(None, []) == 0
    assert backfill.bulk_upsert_spaces_dim(None, []) == 0


class Recorder:
    """A connection that behaves like psycopg's across an error: once a
    statement fails, every statement fails until rollback()."""

    def __init__(self):
        self.events = []
        self.aborted = False
        self.closed = False
        self.statements = []
        self.results = [[(41,)]]        # open_run's RETURNING id

    def fail_the_transaction(self):
        self.aborted = True

    def commit(self):
        self.events.append('commit')

    def rollback(self):
        self.events.append('rollback')
        self.aborted = False

    def close(self):
        self.closed = True

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.aborted:
            raise psycopg.errors.InFailedSqlTransaction('current transaction is aborted')
        self.statements.append((sql, params))
        self.events.append(sql.split()[0].lower())

    def fetchone(self):
        return self.results.pop(0)[0]


def recorded_ingest(monkeypatch, module, rows=None):
    """Fake everything module.main() does outside its own control flow."""
    conn = Recorder()
    rows = rows or {}

    def upsert(name):
        def run(_conn, data):
            conn.events.append(name)
            if rows.get(name):
                raise rows[name](conn)
            return len(data)
        return run

    monkeypatch.setattr(module, 'db_connect', lambda: conn)
    monkeypatch.setattr(module, 'SpanCloud', lambda: SimpleNamespace(panel_id='p'))
    monkeypatch.setattr(module, 'bulk_upsert_panel', upsert('panel'))
    monkeypatch.setattr(module, 'bulk_upsert_spaces_dim', upsert('spaces_dim'))
    monkeypatch.setattr(module, 'bulk_upsert_space_readings', upsert('space_readings'))
    return conn


def run_status(conn):
    """The status the run was closed with, read from close_run's UPDATE."""
    closing = [s for s, _ in conn.statements if s.startswith('UPDATE ingest_runs')]
    return closing[-1] if closing else None


def ongoing_windows(monkeypatch):
    panel_wbs = []

    def panel(_cloud, duration, resolution, wb):
        panel_wbs.append((duration, resolution, wb))
        return [(f'2026-03-0{4 - wb}T05:00:00Z', 'solar', 1.0, 600)]

    def spaces(_cloud, _duration, _resolution, wb):
        return [('t', 's1', 0.5, 600)], [('s1', f'name {wb}', None)]

    monkeypatch.setattr(ongoing, 'fetch_panel_window', panel)
    monkeypatch.setattr(ongoing, 'fetch_spaces_window', spaces)
    return panel_wbs


def test_ongoing_fetches_two_days_and_writes_names_before_readings(monkeypatch):
    conn = recorded_ingest(monkeypatch, ongoing)
    wbs = ongoing_windows(monkeypatch)
    assert ongoing.main() == 0
    assert wbs == [('DAY', 'PHONE_PORTRAIT', 0), ('DAY', 'PHONE_PORTRAIT', 1)]
    # space_readings has a foreign key on spaces, and the run is only marked a
    # success once the readings are committed.
    assert conn.events == ['insert', 'commit', 'panel', 'spaces_dim', 'space_readings',
                           'commit', 'update', 'commit']
    assert "status = 'success'" in run_status(conn)
    assert conn.closed


def test_ongoing_records_the_period_it_read(monkeypatch):
    conn = recorded_ingest(monkeypatch, ongoing)
    ongoing_windows(monkeypatch)
    ongoing.main()
    _, params = conn.statements[-1]
    assert params == [2, 2, '2026-03-03T05:00:00Z', '2026-03-04T05:00:00Z', 41]


def test_ongoing_cloud_failure_is_recorded_and_writes_nothing(monkeypatch):
    conn = recorded_ingest(monkeypatch, ongoing)

    def down():
        raise RuntimeError('x' * 2000)

    monkeypatch.setattr(ongoing, 'SpanCloud', down)
    with pytest.raises(RuntimeError):
        ongoing.main()
    assert "status = 'failed'" in run_status(conn)
    assert conn.statements[-1][1] == ['x' * 1000, 41]
    assert 'panel' not in conn.events
    assert conn.closed


def _sql_error(conn):
    conn.fail_the_transaction()
    return psycopg.errors.CheckViolation('new row violates check constraint')


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'ingester/ongoing.py:78: close_run() runs without a rollback, in the transaction the '
    'failed upsert aborted, so it raises, is swallowed, and the run stays "running" forever'))
def test_ongoing_sql_failure_is_recorded_as_failed(monkeypatch):
    conn = recorded_ingest(monkeypatch, ongoing, rows={'space_readings': _sql_error})
    ongoing_windows(monkeypatch)
    with pytest.raises(psycopg.errors.CheckViolation):
        ongoing.main()
    assert run_status(conn) and "status = 'failed'" in run_status(conn)


def test_ongoing_sql_failure_commits_no_readings(monkeypatch):
    """Panel and circuit rows go in one transaction: the next run refetches
    the same two days, so a half-written cycle can't be left behind."""
    conn = recorded_ingest(monkeypatch, ongoing, rows={'space_readings': _sql_error})
    ongoing_windows(monkeypatch)
    with pytest.raises(psycopg.errors.CheckViolation):
        ongoing.main()
    assert conn.events.count('commit') == 1          # open_run's alone
    assert conn.closed


def backfill_argv(monkeypatch, *args):
    monkeypatch.setattr(sys, 'argv', ['backfill', *args])


def backfill_data(monkeypatch, panel=None, spaces=None):
    monkeypatch.setattr(backfill, 'fetch_panel_all', panel or (
        lambda _c: [('2026-03-03T05:00:00Z', 'solar', 1.0, 600),
                    ('2026-03-04T05:00:00Z', 'solar', 2.0, 600)]))
    monkeypatch.setattr(backfill, 'fetch_spaces_all', spaces or (
        lambda _c: ([('t', 's1', 0.5, 600)], [('s1', 'Dryer', None)])))


def test_backfill_commits_each_stage_so_a_rerun_resumes(monkeypatch):
    conn = recorded_ingest(monkeypatch, backfill)
    backfill_argv(monkeypatch)
    backfill_data(monkeypatch)
    assert backfill.main() == 0
    assert conn.events == ['insert', 'commit', 'panel', 'commit', 'spaces_dim',
                           'space_readings', 'commit', 'update', 'commit']
    assert "status = 'success'" in run_status(conn)
    assert conn.statements[-1][1] == [2, 1, '2026-03-03T05:00:00Z', '2026-03-04T05:00:00Z', 41]


def test_backfill_keeps_the_panel_stage_when_the_circuits_fail(monkeypatch):
    conn = recorded_ingest(monkeypatch, backfill)
    backfill_argv(monkeypatch)

    def spaces_down(_cloud):
        raise RuntimeError('cloud went away')

    backfill_data(monkeypatch, spaces=spaces_down)
    with pytest.raises(RuntimeError):
        backfill.main()
    assert conn.events[:4] == ['insert', 'commit', 'panel', 'commit']
    assert "status = 'failed'" in run_status(conn)


def test_backfill_dry_run_opens_no_database(monkeypatch):
    def no_db():
        raise AssertionError('a dry run must not connect')

    monkeypatch.setattr(backfill, 'db_connect', no_db)
    monkeypatch.setattr(backfill, 'SpanCloud', lambda: SimpleNamespace(panel_id='p'))
    backfill_argv(monkeypatch, '--dry-run')
    backfill_data(monkeypatch)
    assert backfill.main() == 0


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'ingester/backfill.py:555: like ongoing.py, close_run() runs in the aborted transaction, '
    'so a failed upsert leaves the run "running"'))
def test_backfill_sql_failure_is_recorded_as_failed(monkeypatch):
    conn = recorded_ingest(monkeypatch, backfill, rows={'panel': _sql_error})
    backfill_argv(monkeypatch)
    backfill_data(monkeypatch)
    with pytest.raises(psycopg.errors.CheckViolation):
        backfill.main()
    assert run_status(conn) and "status = 'failed'" in run_status(conn)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'ingester/backfill.py:517: --truncate commits the DELETE before the SPAN sign-in, so a '
    'bad password or a cloud outage leaves empty tables, and 10-minute history older than '
    'SPAN keeps can never be pulled again'))
def test_truncate_keeps_the_data_when_the_cloud_is_down(monkeypatch):
    conn = recorded_ingest(monkeypatch, backfill)
    backfill_argv(monkeypatch, '--truncate')

    def down():
        raise RuntimeError('sign-in failed')

    monkeypatch.setattr(backfill, 'SpanCloud', down)
    with pytest.raises(RuntimeError):
        backfill.main()
    assert not [s for s, _ in conn.statements if s.startswith('DELETE')]
