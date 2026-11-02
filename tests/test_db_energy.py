"""Energy totals through the real SQL: local days, DST, gaps, SPAN's retention
tiers meeting, and the upserts. They need the database CI's smoke test uses
(see .gitlab-ci.yml) and are skipped without DATABASE_URL.

Except for the last two tests, which read the synthetic house, every test
writes its own rows, dated 2021 so they can't meet the sample data, inside the
`txn` fixture's transaction, which is always rolled back. Rows are written in
UTC and the expected totals are worked out from them by hand."""

import datetime
from zoneinfo import ZoneInfo

import pytest

from app import queries as q
from ingester import backfill

UTC = datetime.timezone.utc
NY = ZoneInfo('America/New_York')

MONTH_S = 30 * 86400


def d(iso):
    return datetime.date.fromisoformat(iso)


def midnight(iso):
    """A local day's start in UTC. DST changes at 02:00, so it's never ambiguous."""
    day = d(iso)
    return datetime.datetime(day.year, day.month, day.day, tzinfo=NY).astimezone(UTC)


def buckets(start, n, metric, kwh, step=600):
    return [(start + datetime.timedelta(seconds=i * step), metric, kwh, step) for i in range(n)]


def insert(conn, rows):
    with conn.cursor() as cur:
        cur.executemany('INSERT INTO panel_readings (ts, metric, energy_kwh, duration_s) '
                        'VALUES (%s, %s, %s, %s)', rows)


def daily(first, last=None):
    return q.panel_daily_local(d(first), d(last or first))


# ---- Local days -------------------------------------------------------------

def test_a_reading_belongs_to_its_local_day(txn):
    insert(txn, [(datetime.datetime(2021, 1, 16, 4, 50, tzinfo=UTC), 'building_total', 0.1, 600),
                 (datetime.datetime(2021, 1, 16, 5, 0, tzinfo=UTC), 'building_total', 0.2, 600)])
    first, second = daily('2021-01-15', '2021-01-16')     # 23:50 and 00:00, EST
    assert (first['load_kwh'], second['load_kwh']) == (0.1, 0.2)
    assert first['coverage'] == 0.0069                    # 600 s of 86400


@pytest.mark.parametrize('date, n, session_tz', [
    ('2021-03-14', 138, 'UTC'),                 # spring forward: 23 h
    ('2021-11-07', 150, 'UTC'),                 # fall back: 25 h
    ('2021-06-10', 144, 'UTC'),
    ('2021-03-14', 138, 'Asia/Tokyo'),          # the session's zone must not matter
])
def test_a_complete_day_of_any_length(txn, date, n, session_tz):
    txn.execute("SELECT set_config('TimeZone', %s, true)", (session_tz,))
    t0 = midnight(date)
    for metric, kwh in (('building_total', 0.1), ('grid_consuming', 0.08),
                        ('solar_to_grid', 0.03), ('solar', 0.05)):
        insert(txn, buckets(t0, n, metric, kwh))
    day_before = (d(date) - datetime.timedelta(days=1)).isoformat()
    day_after = (d(date) + datetime.timedelta(days=1)).isoformat()
    before, day, after = daily(day_before, day_after)

    assert day['date'] == date
    assert day['load_kwh'] == pytest.approx(n * 0.1)
    assert day['import_kwh'] == pytest.approx(n * 0.08)
    assert day['export_kwh'] == pytest.approx(n * 0.03)
    assert day['solar_kwh'] == pytest.approx(n * 0.05)
    assert day['import_kwh'] - day['export_kwh'] + day['solar_kwh'] == pytest.approx(day['load_kwh'])
    assert (day['coverage'], day['resolution_s']) == (1.0, 600)
    # Nothing spilled over the local midnights, and an empty day is null, not 0.
    for empty in (before, after):
        assert (empty['load_kwh'], empty['coverage'], empty['resolution_s']) == (None, 0.0, None)


def test_year_edges_are_local(txn):
    insert(txn, [(datetime.datetime(2021, 1, 1, 4, 50, tzinfo=UTC), 'building_total', 0.1, 600),
                 (datetime.datetime(2021, 1, 1, 5, 0, tzinfo=UTC), 'building_total', 0.2, 600)])
    assert q.daily_kwh_for_year(2021) == [{'date': '2021-01-01', 'kwh': 0.2}]


def test_month_edges_are_local_and_a_monthly_bucket_counts_in_its_month(txn):
    insert(txn, [(datetime.datetime(2021, 3, 1, 4, 50, tzinfo=UTC), 'building_total', 0.1, 600),
                 (datetime.datetime(2021, 3, 1, 5, 0, tzinfo=UTC), 'building_total', 0.2, 600),
                 (midnight('2021-04-01'), 'building_total', 300.0, MONTH_S)])
    months = [m for m in q.yoy_monthly(q.WHOLE_HOUSE)['months'] if m['year'] == 2021]
    assert months == [{'year': 2021, 'month': 2, 'kwh': 0.1}, {'year': 2021, 'month': 3, 'kwh': 0.2},
                      {'year': 2021, 'month': 4, 'kwh': 300.0}]


# ---- Gaps and tiers ---------------------------------------------------------

def test_a_gap_shows_as_coverage(txn):
    insert(txn, buckets(midnight('2021-06-10'), 72, 'building_total', 0.1))
    row = daily('2021-06-10')[0]
    assert row['load_kwh'] == pytest.approx(7.2)
    assert row['coverage'] == 0.5


def test_tiers_meeting_inside_a_day_add_up(txn):
    t0 = midnight('2021-06-10')
    insert(txn, buckets(t0, 12, 'building_total', 0.6, step=3600))
    insert(txn, buckets(t0 + datetime.timedelta(hours=12), 72, 'building_total', 0.1))
    row = daily('2021-06-10')[0]
    assert row['load_kwh'] == pytest.approx(14.4)         # 12 x 0.6 + 72 x 0.1
    assert row['coverage'] == 1.0
    assert row['resolution_s'] == 3600


def test_a_daily_bucket_covers_its_day_even_a_short_one(txn):
    insert(txn, [(midnight('2021-03-14'), 'building_total', 20.0, 86400)])
    row = daily('2021-03-14')[0]
    assert (row['load_kwh'], row['coverage'], row['resolution_s']) == (20.0, 1.0, 86400)


def test_a_monthly_bucket_is_no_figure_for_a_day(txn):
    insert(txn, [(midnight('2021-06-01'), 'building_total', 300.0, MONTH_S)])
    row = daily('2021-06-01')[0]
    assert (row['load_kwh'], row['coverage'], row['resolution_s']) == (None, 0.0, None)


def test_export_is_zero_with_solar_and_null_without(txn):
    insert(txn, [(midnight('2021-06-10'), 'building_total', 1.0, 600),
                 (midnight('2021-06-10'), 'solar', 0.4, 600),
                 (midnight('2021-06-11'), 'building_total', 1.0, 600)])
    with_solar, without = daily('2021-06-10', '2021-06-11')
    assert (with_solar['solar_kwh'], with_solar['export_kwh']) == (0.4, 0.0)
    assert (without['solar_kwh'], without['export_kwh']) == (None, None)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'app/queries.py:464: a day with grid readings but no building_total has coverage 0 and '
    'import_kwh set; README: "Every kWh field is null when coverage is 0"'))
def test_coverage_zero_means_every_kwh_is_null(txn):
    insert(txn, [(midnight('2021-06-10'), 'grid_consuming', 5.0, 86400)])
    row = daily('2021-06-10')[0]
    assert row['coverage'] == 0.0
    assert row['import_kwh'] is None


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "app/queries.py:283 and :598: the year heatmaps don't filter duration_s, so a monthly "
    "bucket shows as one day holding the whole month's kWh on the 1st"))
@pytest.mark.parametrize('metric, read', [
    ('building_total', q.daily_kwh_for_year),
    ('solar', q.solar_daily_for_year),
])
def test_a_monthly_bucket_is_not_a_day_on_the_heatmap(txn, metric, read):
    insert(txn, [(midnight('2021-06-01'), metric, 300.0, MONTH_S)])
    assert read(2021) == []


# ---- The upserts ------------------------------------------------------------

T = '2021-06-10T04:00:00Z'
DAILY = (T, 'solar', 20.0, 86400)
HOURLY = (T, 'solar', 2.0, 3600)
TEN_MIN = (T, 'solar', 0.1, 600)


def upsert(conn, rows):
    n = backfill.bulk_upsert_panel(conn, rows)
    conn.execute('DROP TABLE _stg_panel')     # ON COMMIT DROP, and this never commits
    return n


def stored(conn):
    return conn.execute(
        "SELECT energy_kwh, duration_s FROM panel_readings WHERE metric = 'solar' "
        "AND ts >= '2021-01-01' AND ts < '2022-01-01' ORDER BY ts").fetchall()


def test_one_fetch_with_every_tier_keeps_the_finest(txn):
    assert upsert(txn, [DAILY, TEN_MIN, HOURLY]) == 3      # rows staged
    assert stored(txn) == [(0.1, 600)]


def test_a_finer_tier_replaces_a_coarser_one(txn):
    upsert(txn, [DAILY])
    upsert(txn, [HOURLY])
    assert stored(txn) == [(2.0, 3600)]
    upsert(txn, [TEN_MIN])
    assert stored(txn) == [(0.1, 600)]


def test_a_coarser_tier_never_replaces_a_finer_one(txn):
    upsert(txn, [TEN_MIN])
    upsert(txn, [DAILY])
    upsert(txn, [HOURLY])
    assert stored(txn) == [(0.1, 600)]


def test_rerunning_a_window_changes_nothing(txn):
    window = [(f'2021-06-10T04:{m:02d}:00Z', 'solar', 0.1, 600) for m in (0, 10, 20)]
    upsert(txn, window)
    upsert(txn, window)
    assert stored(txn) == [(0.1, 600)] * 3


def test_a_revised_bucket_takes_the_new_value(txn):
    """The bucket in progress when the ingest ran is partial until the next run."""
    upsert(txn, [TEN_MIN])
    upsert(txn, [(T, 'solar', 0.15, 600)])
    assert stored(txn) == [(0.15, 600)]


def test_circuit_upserts_follow_the_same_rules(txn):
    backfill.bulk_upsert_spaces_dim(txn, [('s-upsert', 'First name', None)])
    backfill.bulk_upsert_spaces_dim(txn, [('s-upsert', 'New name', None)])
    for rows in ([(T, 's-upsert', 20.0, 86400)], [(T, 's-upsert', 0.1, 600)],
                 [(T, 's-upsert', 2.0, 3600)], [(T, 's-upsert', 0.2, 600)]):
        backfill.bulk_upsert_space_readings(txn, rows)
        txn.execute('DROP TABLE _stg_space')
    assert txn.execute("SELECT energy_kwh, duration_s FROM space_readings "
                       "WHERE space_id = 's-upsert'").fetchall() == [(0.2, 600)]
    assert txn.execute("SELECT name FROM spaces WHERE space_id = 's-upsert'").fetchone() == ('New name',)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'ingester/backfill.py:424 and app/queries.py:435: a finer tier only displaces a coarser '
    'row at the same ts. fetch_panel_window drops a null bucket, so if the 10-minute tier has '
    'no midnight value the daily row stays beside the other 143 buckets and the day sums both'))
def test_a_day_is_not_counted_in_two_tiers(txn):
    t0 = midnight('2021-06-10')
    upsert(txn, [(t0.isoformat(), 'building_total', 20.0, 86400)])
    fine = buckets(t0 + datetime.timedelta(seconds=600), 143, 'building_total', 0.1)
    upsert(txn, [(ts.isoformat(), metric, kwh, step) for ts, metric, kwh, step in fine])
    assert daily('2021-06-10')[0]['load_kwh'] in (pytest.approx(20.0), pytest.approx(14.3))


# ---- Sessions and anomalies -------------------------------------------------

@pytest.fixture
def ev_space(txn, monkeypatch):
    """A circuit of the test's own, so the sample EV charger's sessions stay out."""
    txn.execute("INSERT INTO spaces (space_id, name) VALUES ('s-ev', 'Test charger')")
    monkeypatch.setattr(q, 'ev_circuit', lambda _cur: {'space_id': 's-ev', 'name': 'Test charger'})
    now = datetime.datetime.now(UTC)
    # A day back, on a 10-minute boundary: inside days=2 whenever this runs.
    return (now - datetime.timedelta(days=1)).replace(minute=now.minute - now.minute % 10,
                                                      second=0, microsecond=0)


def charge(conn, start, minutes_kwh):
    """Rows at (minutes after start, kWh in that 10-minute bucket)."""
    with conn.cursor() as cur:
        cur.executemany(
            'INSERT INTO space_readings (ts, space_id, energy_kwh, duration_s) '
            "VALUES (%s, 's-ev', %s, 600)",
            [(start + datetime.timedelta(minutes=m), kwh) for m, kwh in minutes_kwh])


def test_a_sub_threshold_bucket_splits_sessions(txn, ev_space):
    charge(txn, ev_space, [(0, 1.2), (10, 1.2), (20, 1.2), (30, 0.01), (40, 0.9), (50, 0.9)])
    later, earlier = q.ev_charging_sessions(days=2, threshold_kw=1.5)
    assert (later['duration_min'], later['kwh']) == (20.0, pytest.approx(1.8))
    assert (later['avg_kw'], later['peak_kw']) == (pytest.approx(5.4), pytest.approx(5.4))
    assert (earlier['duration_min'], earlier['kwh']) == (30.0, pytest.approx(3.6))
    assert earlier['avg_kw'] == pytest.approx(7.2)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    'app/queries.py:2299: only a sub-threshold row ends a session, though the comment at :2281 '
    'and sessions.html:46 say a longer gap does too. With no rows between them, two charges '
    'merge into one 80-minute session (appliance_sessions has the same SQL)'))
def test_a_gap_in_the_data_splits_sessions(txn, ev_space):
    charge(txn, ev_space, [(0, 1.2), (10, 1.2), (60, 1.2), (70, 1.2)])
    assert len(q.ev_charging_sessions(days=2, threshold_kw=1.5)) == 2


def test_anomaly_compares_a_full_day_with_the_week_before(txn):
    txn.execute("INSERT INTO spaces (space_id, name) VALUES ('s-spike', 'Test spike')")
    today = q.today_local()

    def row(days_ago, kwh):
        day = today - datetime.timedelta(days=days_ago)
        ts = datetime.datetime(day.year, day.month, day.day, tzinfo=NY)
        return ts, 's-spike', kwh

    rows = [row(n, 2.0) for n in range(2, 9)] + [row(1, 2000.0), row(0, 900000.0)]
    with txn.cursor() as cur:
        cur.executemany('INSERT INTO space_readings (ts, space_id, energy_kwh, duration_s) '
                        'VALUES (%s, %s, %s, 86400)', rows)
    # Far past anything the sample house does, so it sorts into the top 30.
    hits = [r for r in q.anomaly_history(window_days=30) if r['space_id'] == 's-spike']
    assert [(r['date'], r['kwh'], r['baseline'], r['ratio']) for r in hits] == [
        ((today - datetime.timedelta(days=1)).isoformat(), 2000.0, 2.0, 1000.0)]


# ---- The synthetic house ----------------------------------------------------

def test_sample_house_balances_every_day(client):
    """README: without a battery, import - export + solar = load. The sample
    writes 6 decimals per bucket, so allow for 144 buckets' rounding."""
    today = q.today_local()
    start = today - datetime.timedelta(days=300)
    days = client.get(f'/api/v1/panel/daily?start={start}&end={today}').json()['days']
    both = [x for x in days if x['load_kwh'] is not None and x['solar_kwh'] is not None]
    assert len(both) > 250
    for x in both:
        assert x['import_kwh'] - x['export_kwh'] + x['solar_kwh'] == pytest.approx(x['load_kwh'], abs=1e-3)


def test_sample_house_recent_days_are_complete(client):
    """The last 45 days are 10-minute buckets. The newest two days are left
    out: the run may cross midnight after the sample was written."""
    days = client.get('/api/v1/panel/daily').json()['days']
    assert len(days) == 30
    assert all(x['coverage'] == 1.0 and x['resolution_s'] == 600 for x in days[:-2])
