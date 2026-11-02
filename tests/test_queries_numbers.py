"""The arithmetic app/queries.py does in Python on what SQL returns: units,
percentages, cost, projections and on-time. Each result set is invented here
and the expected numbers are worked out by hand. What the SQL itself does
(time zones, tiers, sums) is in test_db_energy.py."""

import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

from app import queries as q

UTC = datetime.timezone.utc
NY = ZoneInfo('America/New_York')


@pytest.fixture(autouse=True)
def new_york(monkeypatch):
    monkeypatch.setattr(q, 'TZ', 'America/New_York')


def day(iso):
    return datetime.date.fromisoformat(iso)


# ---- today ------------------------------------------------------------------

def frozen_clock(monkeypatch, instant):
    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz)

    monkeypatch.setattr(q, 'datetime', SimpleNamespace(datetime=Clock))


def test_today_is_the_local_day_not_the_servers(monkeypatch):
    frozen_clock(monkeypatch, datetime.datetime(2026, 3, 15, 1, 30, tzinfo=UTC))
    assert q.today_local() == day('2026-03-14')
    monkeypatch.setattr(q, 'TZ', 'Asia/Tokyo')
    assert q.today_local() == day('2026-03-15')


# ---- /api/v1/panel/daily ----------------------------------------------------

def daily_row(**fields):
    return {'date': '2026-10-16', 'import_kwh': 43.13, 'export_kwh': 0.2, 'solar_kwh': 5.59,
            'load_kwh': 48.52, 'coverage': 1.0, 'resolution_s': 600, **fields}


def test_panel_daily_passes_readings_through(fake_db):
    fake_db([daily_row()])
    assert q.panel_daily_local(day('2026-10-16'), day('2026-10-16')) == [daily_row()]


def test_panel_daily_export_is_zero_when_solar_ran_and_none_went_out(fake_db):
    """SPAN sends no export series on a day all the solar stayed in the house."""
    fake_db([daily_row(export_kwh=None)])
    assert q.panel_daily_local(day('2026-10-16'), day('2026-10-16'))[0]['export_kwh'] == 0.0


def test_panel_daily_export_stays_unknown_without_a_solar_series(fake_db):
    fake_db([daily_row(export_kwh=None, solar_kwh=None)])
    row = q.panel_daily_local(day('2026-10-16'), day('2026-10-16'))[0]
    assert row['export_kwh'] is None and row['solar_kwh'] is None


def test_panel_daily_a_day_without_readings_is_null_not_zero(fake_db):
    empty = dict.fromkeys(('import_kwh', 'export_kwh', 'solar_kwh', 'load_kwh', 'resolution_s'))
    fake_db([{'date': '2026-10-16', 'coverage': 0.0, **empty}])
    assert q.panel_daily_local(day('2026-10-16'), day('2026-10-16')) == [
        {'date': '2026-10-16', 'coverage': 0.0, **empty}]


@pytest.mark.parametrize('raw, shown', [(0.123456, 0.1235), (1.0, 1.0), (1.0004, 1.0)])
def test_panel_daily_coverage_is_rounded_and_capped_at_one(fake_db, raw, shown):
    fake_db([daily_row(coverage=raw)])
    assert q.panel_daily_local(day('2026-10-16'), day('2026-10-16'))[0]['coverage'] == shown


# ---- Solar ------------------------------------------------------------------

def test_solar_overview(fake_db):
    fake_db(
        [{'produced': 1000.0, 'consumed': 600.0, 'exported': 400.0}],
        [{'produced': 50.0, 'consumed': 30.0, 'exported': None}],
        [{'produced': None, 'consumed': None, 'exported': None}],
        [{'date': '2026-06-21', 'produced': 62.5}],
    )
    assert q.solar_overview() == {
        'lifetime': {'produced': 1000.0, 'consumed': 600.0, 'exported': 400.0, 'self_pct': 60.0},
        'this_month': {'produced': 50.0, 'consumed': 30.0, 'exported': 0.0},
        'last_month': {'produced': 0.0, 'consumed': 0.0, 'exported': 0.0},
        'best_day': {'date': '2026-06-21', 'produced': 62.5},
    }


def test_solar_overview_with_no_solar_doesnt_divide_by_zero(fake_db):
    nothing = {'produced': None, 'consumed': None, 'exported': None}
    fake_db([dict(nothing)], [dict(nothing)], [dict(nothing)], [])
    o = q.solar_overview()
    assert o['lifetime']['self_pct'] == 0.0
    assert o['best_day'] == {'date': None, 'produced': 0.0}


def test_solar_monthly_percentages(fake_db):
    fake_db([
        {'month': '2026-06-01', 'produced': 500.0, 'consumed': 300.0, 'exported': 200.0,
         'house_total': 900.0, 'from_grid': 600.0},
        {'month': '2026-01-01', 'produced': None, 'consumed': None, 'exported': None,
         'house_total': 700.0, 'from_grid': 700.0},
    ])
    june, january = q.solar_monthly_full()
    assert june['self_pct'] == 60.0                     # of production, used at home
    assert june['offset_pct'] == pytest.approx(100 / 3)  # of the house, met by solar
    assert (january['produced'], january['self_pct'], january['offset_pct']) == (0.0, 0.0, 0.0)


def test_capacity_factor(fake_db):
    t0 = datetime.datetime(2025, 1, 1, tzinfo=UTC)
    fake_db([{'peak_kw': 6.0}],
            [{'total_kwh': 5256.0, 'first_ts': t0, 'last_ts': t0 + datetime.timedelta(hours=8760)}])
    assert q.capacity_factor() == {
        'peak_kw': 6.0, 'total_kwh': 5256.0, 'observation_hours': 8760.0,
        'capacity_factor_pct': pytest.approx(10.0),   # 5256 / (6 kW x 8760 h)
        'first_ts': '2025-01-01T00:00:00+00:00', 'last_ts': '2026-01-01T00:00:00+00:00'}


def test_capacity_factor_with_no_solar(fake_db):
    fake_db([{'peak_kw': None}], [{'total_kwh': None, 'first_ts': None, 'last_ts': None}])
    cf = q.capacity_factor()
    assert cf['capacity_factor_pct'] is None and cf['total_kwh'] == 0.0


FORECAST_MONTHS = [(6, 300.0), (7, 320.0), (12, 100.0)]


@pytest.mark.parametrize('today, projected', [
    # 1234 YTD + June's 300 x 20/30 left + July 320 + December 100
    ('2026-06-10', 1234 + 200 + 420),
    # June is done: only the later months remain.
    ('2026-06-30', 1234 + 420),
    # August onward: nothing for August to November in the history, December 100.
    ('2026-08-15', 1234 + 100),
    # The year's last day adds nothing.
    ('2026-12-31', 1234),
    # 29 days in a leap February, and no history for it.
    ('2028-02-10', 1234 + 300 + 320 + 100),
])
def test_year_end_projection(fake_db, monkeypatch, today, projected):
    monkeypatch.setattr(q, 'today_local', lambda: day(today))
    fake_db([(1234.0,)], FORECAST_MONTHS, [(2025, 4000.0)])
    f = q.year_end_production_forecast()
    assert f['projected_total'] == pytest.approx(projected)
    assert f['ytd_kwh'] == 1234.0
    assert f['monthly_avg'] == {6: 300.0, 7: 320.0, 12: 100.0}
    assert f['prior_totals'] == {2025: 4000.0}


def test_year_end_projection_with_no_data(fake_db, monkeypatch):
    monkeypatch.setattr(q, 'today_local', lambda: day('2026-06-10'))
    fake_db([(None,)], [], [])
    assert q.year_end_production_forecast()['projected_total'] == 0.0


def test_energy_flows_windows(fake_db):
    db = fake_db([{'produced': 10.0, 'from_solar': 6.0, 'to_grid': 4.0,
                   'from_grid': None, 'consumed': 6.0}])
    flows = q.energy_flows('12mo')
    assert flows == {'window': '12mo', 'produced': 10.0, 'from_solar': 6.0, 'to_grid': 4.0,
                     'from_grid': 0.0, 'consumed': 6.0}
    assert "INTERVAL '365 days'" in db.sql[0]
    db = fake_db([{}])
    assert q.energy_flows('all')['produced'] == 0.0
    assert 'INTERVAL' not in db.sql[0]


# ---- Power from energy ------------------------------------------------------

def test_load_24h_ago_is_average_watts(fake_db):
    ts = datetime.datetime(2026, 3, 4, 12, 0, tzinfo=UTC)
    fake_db([{'ts': ts, 'energy_kwh': 0.1, 'duration_s': 600}])
    assert q.load_24h_ago_w() == {'ts': '2026-03-04T12:00:00+00:00', 'load_w': pytest.approx(600.0)}


def test_load_24h_ago_without_a_bucket(fake_db):
    fake_db([])
    assert q.load_24h_ago_w() == {'ts': None, 'load_w': None}


def test_day_curve_power_is_comparable_across_tiers(fake_db):
    t = datetime.datetime(2026, 3, 4, 5, 0, tzinfo=UTC)
    fake_db([
        {'ts': t, 'metric': 'solar', 'kwh': 0.5, 'duration_s': 600},
        {'ts': t, 'metric': 'building_total', 'kwh': 2.0, 'duration_s': 3600},
    ])
    d = q.day_curve('2026-03-04')
    assert d['granularity'] == 600
    assert d['series']['solar'][0]['power_kw'] == pytest.approx(3.0)
    assert d['series']['building_total'][0]['power_kw'] == pytest.approx(2.0)


def test_day_curve_rejects_a_bad_date():
    with pytest.raises(ValueError):
        q.day_curve('2026-13-01')


# ---- Rankings ---------------------------------------------------------------

def test_top_consumers_share(fake_db):
    fake_db([{'name': 'Dryer', 'kwh': 30.0}, {'name': 'Lights', 'kwh': 10.0}])
    assert q.top_consumers('30d') == [{'name': 'Dryer', 'kwh': 30.0, 'share': 0.75},
                                      {'name': 'Lights', 'kwh': 10.0, 'share': 0.25}]


def test_top_consumers_bad_window():
    with pytest.raises(ValueError):
        q.top_consumers('forever')


def test_top_days_weekends(fake_db):
    fake_db([
        {'date': '2026-03-01', 'kwh': 9.0, 'dow': 0, 'from_solar': 1.0, 'from_grid': 8.0,
         'produced': 2.0, 'exported': 1.0},
        {'date': '2026-03-06', 'kwh': 8.0, 'dow': 5, 'from_solar': 0.0, 'from_grid': 8.0,
         'produced': 0.0, 'exported': 0.0},
        {'date': '2026-03-07', 'kwh': 7.0, 'dow': 6, 'from_solar': 0.0, 'from_grid': 7.0,
         'produced': 0.0, 'exported': 0.0},
    ])
    rows = q.top_days('30d')
    assert [r['day_type'] for r in rows] == ['weekend', 'weekday', 'weekend']
    assert rows[0]['from_grid'] == 8.0 and rows[0]['exported'] == 1.0


def test_top_days_has_no_panel_metrics_for_a_circuit(fake_db):
    fake_db()
    assert q.top_days('30d', 'some-circuit', metric='production') == []


# ---- Duty cycles ------------------------------------------------------------

FURNACE = {'key': 'furnace', 'label': 'Furnace', 'space_id': 's-furnace', 'on_kw': 0.066,
           'standby_w': 12.0, 'on_w': 120.0, 'since': None, 'glyph': '◆'}
DEHUMIDIFIER = {'key': 'dehumidifier', 'label': 'Dehumidifier', 'space_id': 's-dehum',
                'on_kw': 0.2, 'since': None, 'glyph': '▼'}


@pytest.fixture
def duty(monkeypatch):
    def use(*entries):
        monkeypatch.setattr(q.config, 'circuits', lambda: {'duty_cycle': list(entries)})
    return use


def buckets(date, kwh_list):
    return [{'day': day(date), 'kwh': k} for k in kwh_list]


# Standby is 12 W, so 0.002 kWh a bucket; the burner adds 108 W, 0.0018 kWh a minute.
def firing(minutes):
    return 0.002 + 0.0018 * minutes


def test_duty_minutes_are_energy_above_standby(fake_db, duty):
    duty(FURNACE)
    kwh = [0.002, firing(3), firing(10), 0.002, firing(25), firing(0.5)]
    fake_db([{'space_id': 's-furnace'}], buckets('2026-03-03', kwh),
            [{'day': day('2026-03-04'), 'kwh': 0.1}, {'day': day('2026-03-03'), 'kwh': sum(kwh)}])
    item = q.duty_cycle_overview(days=30)[0]
    done = item['days'][1]
    assert done['minutes_on'] == pytest.approx(23.0)      # 3 + 10 + 10 (capped); under 1 is noise
    assert done['hours_on'] == pytest.approx(23.0 / 60)
    assert done['starts'] == 2                            # adjacent firings count once
    assert done['method'] == '10-min buckets'
    assert done['coverage'] == 6 / 144


def test_duty_totals_leave_out_today(fake_db, duty):
    duty(DEHUMIDIFIER)
    fake_db([{'space_id': 's-dehum'}], [],
            [{'day': day('2026-03-04'), 'kwh': 99.0}, {'day': day('2026-03-03'), 'kwh': 2.0},
             {'day': day('2026-03-02'), 'kwh': 4.0}])
    item = q.duty_cycle_overview(days=30)[0]
    assert item['total_kwh'] == 6.0
    assert item['mean_kwh'] == 3.0


def test_duty_on_kw_decides_without_running_power(fake_db, duty):
    duty(DEHUMIDIFIER)
    # 0.04 kWh in 10 min is 0.24 kW, on; 0.03 is 0.18 kW, off.
    kwh = [0.04, 0.03, 0.04, 0.04]
    fake_db([{'space_id': 's-dehum'}], buckets('2026-03-03', kwh),
            [{'day': day('2026-03-04'), 'kwh': 1.0}, {'day': day('2026-03-03'), 'kwh': sum(kwh)}])
    done = q.duty_cycle_overview(days=30)[0]['days'][1]
    assert done['minutes_on'] == 30.0
    assert done['starts'] == 2


def test_duty_older_days_estimate_from_daily_energy(fake_db, duty):
    duty(FURNACE)
    # 24 h of standby is 0.288 kWh; two hours of the 108-W burner is 0.216 more.
    fake_db([{'space_id': 's-furnace'}], [],
            [{'day': day('2026-03-04'), 'kwh': 1.0}, {'day': day('2026-01-10'), 'kwh': 0.504}])
    old = q.duty_cycle_overview(days=90)[0]['days'][1]
    assert old['minutes_on'] == pytest.approx(120.0)
    assert (old['starts'], old['coverage'], old['method']) == (None, None, 'daily energy')


def test_duty_on_time_is_unknown_without_buckets_or_running_power(fake_db, duty):
    duty(DEHUMIDIFIER)
    fake_db([{'space_id': 's-dehum'}], [],
            [{'day': day('2026-03-04'), 'kwh': 1.0}, {'day': day('2026-01-10'), 'kwh': 2.0}])
    item = q.duty_cycle_overview(days=90)[0]
    assert item['days'][1]['minutes_on'] is None
    assert item['mean_hours_on'] is None


def test_duty_circuit_not_found(fake_db, duty):
    duty(FURNACE)
    fake_db([])
    item = q.duty_cycle_overview()[0]
    assert (item['space_id'], item['days']) == (None, [])


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'app/queries.py:2653: coverage divides by 144 buckets, but a DST day has 138 or 150, so '
    'a complete day reads 0.958 or 1.042 (README: DST days are 23 or 25 h)'))
@pytest.mark.parametrize('date, n', [('2026-03-08', 138), ('2026-11-01', 150)])
def test_duty_coverage_of_a_complete_dst_day_is_one(fake_db, duty, date, n):
    duty(DEHUMIDIFIER)
    fake_db([{'space_id': 's-dehum'}], buckets(date, [0.01] * n),
            [{'day': day('2026-12-01'), 'kwh': 1.0}, {'day': day(date), 'kwh': n * 0.01}])
    assert q.duty_cycle_overview(days=365)[0]['days'][1]['coverage'] == pytest.approx(1.0)


# ---- $: the optional flat rate ----------------------------------------------

def test_no_rate_means_no_rates(monkeypatch):
    monkeypatch.delenv('RATE_IMPORT_PER_KWH', raising=False)
    monkeypatch.delenv('RATE_EXPORT_CREDIT_PER_KWH', raising=False)
    assert q.current_rates() is None


def test_rates_come_from_the_config(monkeypatch):
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '0.31')
    monkeypatch.setenv('RATE_EXPORT_CREDIT_PER_KWH', '0.08')
    assert q.current_rates() == {'all_in_per_kwh': 0.31, 'credit_dollars_per_kwh': 0.08,
                                 'source': 'flat'}


def test_a_free_rate_is_still_a_rate(monkeypatch):
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '0')
    monkeypatch.setenv('RATE_EXPORT_CREDIT_PER_KWH', '0')
    assert q.current_rates() == {'all_in_per_kwh': 0.0, 'credit_dollars_per_kwh': 0.0,
                                 'source': 'flat'}


def test_a_mistyped_rate_prices_nothing_rather_than_guessing(monkeypatch):
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '31 cents')
    assert q.current_rates() is None
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '0.31')
    monkeypatch.setenv('RATE_EXPORT_CREDIT_PER_KWH', 'n/a')
    assert q.current_rates()['credit_dollars_per_kwh'] == 0.31


def dehumidifier_rows():
    daily = [{'day': day('2026-03-01'), 'kwh': 2.4, 'on_buckets': 72, 'buckets': 144},
             {'day': day('2026-03-02'), 'kwh': 1.2, 'on_buckets': 36, 'buckets': 144},
             {'day': day('2026-03-03'), 'kwh': 9.9, 'on_buckets': 6, 'buckets': 12}]   # today
    monthly = [{'month': day('2026-02-01'), 'kwh': 30.0, 'days': 15},
               {'month': day('2026-03-01'), 'kwh': 13.5, 'days': 3}]
    return [{'space_id': 's-dehum'}], daily, monthly


def test_dehumidifier_cost_and_duty(fake_db, duty, monkeypatch):
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '0.25')
    duty(DEHUMIDIFIER)
    fake_db(*dehumidifier_rows())
    d = q.dehumidifier_overview()
    assert d['rate'] == 0.25
    first = d['daily'][0]
    assert first == {'date': '2026-03-01', 'kwh': 2.4, 'cost': pytest.approx(0.6),
                     'hours_on': 12.0, 'duty_pct': 50.0}
    # The two full days: today's partial one is left out.
    assert d['last30_days'] == 2
    assert d['last30_kwh'] == pytest.approx(3.6)
    assert d['last30_cost'] == pytest.approx(0.9)
    assert d['last30_duty_pct'] == pytest.approx(37.5)    # 18 h of 48
    assert d['monthly'][0] == {'month': '2026-02', 'kwh': 30.0, 'days': 15, 'cost': 7.5,
                               'cost_per_30d': 15.0}


def test_dehumidifier_at_a_free_rate_costs_nothing_rather_than_unknown(fake_db, duty, monkeypatch):
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '0')
    duty(DEHUMIDIFIER)
    fake_db(*dehumidifier_rows())
    d = q.dehumidifier_overview()
    assert (d['rate'], d['last30_cost'], d['daily'][0]['cost']) == (0.0, 0.0, 0.0)


def test_dehumidifier_without_a_rate_has_no_dollars(fake_db, duty, monkeypatch):
    monkeypatch.delenv('RATE_IMPORT_PER_KWH', raising=False)
    duty(DEHUMIDIFIER)
    fake_db(*dehumidifier_rows())
    d = q.dehumidifier_overview()
    assert d['rate'] is None and d['last30_cost'] is None
    assert all(x['cost'] is None for x in d['daily'])
    assert all(x['cost'] is None and x['cost_per_30d'] is None for x in d['monthly'])


def test_dehumidifier_not_configured(duty, monkeypatch):
    monkeypatch.delenv('RATE_IMPORT_PER_KWH', raising=False)
    duty()
    d = q.dehumidifier_overview()
    assert d['configured'] is False
    assert (d['last30_kwh'], d['last30_cost'], d['daily']) == (0.0, None, [])


# ---- Sessions ---------------------------------------------------------------

EV_SPACE = {'space_id': 's-ev', 'name': 'EV Charger'}


def session_row(start, end, kwh=3.6, **extra):
    return {'start_ts': start, 'end_ts': end, 'kwh': kwh, 'avg_kw': 7.2, 'peak_kw': 7.4,
            'buckets': 3, **extra}


@pytest.fixture
def ev_config(monkeypatch):
    monkeypatch.setattr(q.config, 'circuits', lambda: {
        'ev': {'space_ids': [], 'name_match': ['%EV%'], 'threshold_kw': 1.5}})


def test_ev_session_length_counts_the_last_bucket(fake_db, ev_config):
    start = datetime.datetime(2026, 3, 4, 21, 0, tzinfo=NY)
    end = datetime.datetime(2026, 3, 4, 21, 20, tzinfo=NY)
    fake_db([EV_SPACE], [session_row(start, end)])
    s = q.ev_charging_sessions(days=30)[0]
    assert s['duration_min'] == 30.0
    assert (s['kwh'], s['avg_kw'], s['peak_kw']) == (3.6, 7.2, 7.4)
    assert (s['date'], s['space_id'], s['space_name']) == ('2026-03-04', 's-ev', 'EV Charger')


def test_ev_sessions_use_the_configured_threshold(fake_db, ev_config):
    db = fake_db([EV_SPACE], [])
    assert q.ev_charging_sessions() == []
    assert db.calls[-1][1] == ('s-ev', '90 days', 1.5)


def test_ev_circuit_prefers_configured_ids(fake_db, monkeypatch):
    monkeypatch.setattr(q.config, 'circuits', lambda: {
        'ev': {'space_ids': ['s-a'], 'name_match': ['%EV%'], 'threshold_kw': 1.5}})
    db = fake_db([], [EV_SPACE])    # the id is gone from spaces: fall back to the name
    with db.cursor() as cur:
        assert q.ev_circuit(cur) == EV_SPACE
    assert db.calls[1][1] == ['%EV%']


# A 21:00 start in New York in March, as a database whose time zone is UTC
# returns it. psycopg gives timestamptz back in the session's zone, and that is
# UTC in the compose quickstart, where the db service gets no TZ.
EVENING = datetime.datetime(2026, 3, 5, 2, 0, tzinfo=UTC)
LOCAL_DAY = '2026-03-04'


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'app/queries.py:2327: .date() on a timestamptz gives the Postgres session\'s day, not '
    'config TZ\'s, so an evening session lands on the next day (README: dates are local days)'))
def test_ev_session_date_is_the_local_day(fake_db, ev_config):
    fake_db([EV_SPACE], [session_row(EVENING, EVENING)])
    assert q.ev_charging_sessions()[0]['date'] == LOCAL_DAY


PROFILE = {'key': 'wh', 'label': 'Water heater', 'threshold_kw': 0.25, 'min_kwh': 0.05,
           'secondary_kw': 1.5, 'secondary_label': 'resistive backup', 'glyph': '●',
           'color': 'violet', 'name_match': 'Water Heater', 'note': ''}


@pytest.fixture
def profiles(monkeypatch):
    def use(*entries):
        monkeypatch.setattr(q, 'APPLIANCE_PROFILES', list(entries))
    return use


def test_appliance_session_secondary_split(fake_db, profiles):
    profiles(PROFILE)
    start = datetime.datetime(2026, 3, 4, 7, 0, tzinfo=NY)
    end = datetime.datetime(2026, 3, 4, 7, 50, tzinfo=NY)
    fake_db([{'space_id': 's-wh', 'name': 'Water Heater'}],
            [session_row(start, end, kwh=2.0, secondary_kwh=1.5, secondary_buckets=2)])
    s = q.appliance_sessions('wh')[0]
    assert s['duration_min'] == 60.0
    assert s['secondary_min'] == 20
    assert s['secondary_kwh'] == 1.5
    assert s['secondary_share'] == 0.75


def test_appliance_unknown_key(profiles):
    profiles(PROFILE)
    assert q.appliance_sessions('nope') == []
    assert q.appliance_daily_stats('nope') is None


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'app/queries.py:1449: as ev_charging_sessions, .date() is the session\'s day, not TZ\'s'))
def test_appliance_session_date_is_the_local_day(fake_db, profiles):
    profiles({k: v for k, v in PROFILE.items() if not k.startswith('secondary')})
    fake_db([{'space_id': 's-wh', 'name': 'Water Heater'}], [session_row(EVENING, EVENING)])
    assert q.appliance_sessions('wh')[0]['date'] == LOCAL_DAY


def test_appliance_rollup(monkeypatch, profiles):
    profiles(PROFILE, {**PROFILE, 'key': 'fridge', 'label': 'Fridge', 'high_cycle': True,
                       'secondary_kw': None})
    sessions = {
        'wh': [{'start': '2026-03-04T07:00', 'kwh': 2.0, 'duration_min': 60.0, 'peak_kw': 4.5,
                'secondary_kwh': 1.5, 'secondary_min': 20},
               {'start': '2026-03-05T07:00', 'kwh': 1.0, 'duration_min': 30.0, 'peak_kw': 0.3,
                'secondary_kwh': 0.0, 'secondary_min': 0}],
        'fridge': [{'start': '2026-03-05T08:00', 'kwh': 0.1, 'duration_min': 12.0, 'peak_kw': 0.1}],
    }
    monkeypatch.setattr(q, 'appliance_sessions', lambda key, days: sessions[key])
    out = q.all_appliance_sessions()
    wh, fridge = out['summary']
    assert (wh['count'], wh['kwh'], wh['hours'], wh['peak_kw']) == (2, 3.0, 1.5, 4.5)
    assert (wh['avg_kwh'], wh['avg_min']) == (1.5, 45.0)
    assert (wh['secondary_kwh'], wh['secondary_count'], wh['secondary_min']) == (1.5, 1, 20)
    assert wh['secondary_share'] == 0.5
    assert fridge['count'] == 1
    # A high_cycle appliance has its tile but stays out of the combined list.
    assert [s['start'] for s in out['sessions']] == ['2026-03-05T07:00', '2026-03-04T07:00']


def test_appliance_daily_stats(fake_db, profiles):
    profiles(PROFILE)
    fake_db([{'space_id': 's-wh', 'name': 'Water Heater'}], [
        {'day': day('2026-03-03'), 'day_kwh': 1.0, 'active_buckets': 72, 'total_buckets': 144,
         'peak_kw': 0.5, 'avg_kw': 0.2},
        {'day': day('2026-03-04'), 'day_kwh': 2.0, 'active_buckets': 36, 'total_buckets': 144,
         'peak_kw': 0.5, 'avg_kw': 0.2},
    ])
    s = q.appliance_daily_stats('wh')
    assert s['daily'][0]['active_min'] == 720
    assert s['daily'][0]['duty_pct'] == 50.0
    assert (s['days'], s['total_kwh'], s['avg_kwh_day']) == (2, 3.0, 1.5)
    assert s['avg_duty_pct'] == 37.5
    assert s['avg_active_min_day'] == 540.0


# ---- DB vs VictoriaMetrics --------------------------------------------------

class VmResponse:
    def __init__(self, values):
        self.values = values

    def raise_for_status(self):
        pass

    def json(self):
        return {'data': {'result': [{'values': [[str(t), str(v)] for t, v in self.values]}]}}


def test_vm_check_off_without_a_url(monkeypatch):
    monkeypatch.delenv('VM_URL', raising=False)
    assert q.db_vs_vm_check()['enabled'] is False


def test_vm_check_reports_a_vm_outage(monkeypatch):
    monkeypatch.setenv('VM_URL', 'http://vm.invalid')

    def down(*_args, **_kwargs):
        raise requests.ConnectionError('refused')

    monkeypatch.setattr(requests, 'get', down)
    assert q.db_vs_vm_check() == {'enabled': True, 'error': 'VictoriaMetrics: refused', 'days': []}


def test_vm_check_counts_only_live_buckets(fake_db, monkeypatch):
    """Bucket 1 is live and 5 % apart; 2 is frozen, its 900 kWh must not count;
    3 is live but SPAN's cloud lost it."""
    monkeypatch.setenv('VM_URL', 'http://vm.invalid')
    monkeypatch.setenv('VM_LOAD_SERIES', 'load_w')
    seen = {}

    def get(_url, timeout=None, params=None):
        t = seen.setdefault('start', params['start'])
        q_text = params['query']
        if q_text.startswith('changes'):
            return VmResponse([(t, 20), (t + 600, 3), (t + 1200, 19)])
        if 'load_w' in q_text:
            return VmResponse([(t, 10.0), (t + 600, 900.0), (t + 1200, 0.0)])
        return VmResponse([(t, 0.5), (t + 600, 900.0), (t + 1200, 0.0)])

    monkeypatch.setattr(requests, 'get', get)
    fake_db(lambda: [(seen['start'] - 600, 'building_total', 10.5),
                     (seen['start'] - 600, 'solar', 0.5),
                     (seen['start'], 'building_total', 800.0)])
    out = q.db_vs_vm_check(days=1)
    assert out['error'] is None
    d = out['days'][0]
    assert (d['buckets'], d['live_buckets'], d['missing_in_db']) == (3, 2, 1)
    assert d['db_load'] == 10.5 and d['vm_load'] == 10.0
    assert d['load_pct'] == pytest.approx(5.0)
    assert d['solar_pct'] is None                    # under 1 kWh proves nothing
    assert d['flagged'] is True


def test_vm_check_within_tolerance_is_not_flagged(fake_db, monkeypatch):
    monkeypatch.setenv('VM_URL', 'http://vm.invalid')
    seen = {}

    def get(_url, timeout=None, params=None):
        t = seen.setdefault('start', params['start'])
        return VmResponse([(t, 20 if params['query'].startswith('changes') else 10.0)])

    monkeypatch.setattr(requests, 'get', get)
    fake_db(lambda: [(seen['start'] - 600, 'building_total', 10.2),
                     (seen['start'] - 600, 'solar', 10.0)])
    assert q.db_vs_vm_check(days=1)['days'][0]['flagged'] is False
