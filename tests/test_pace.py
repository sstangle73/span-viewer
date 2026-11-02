"""The month pace page: its windows and arithmetic need no database, and its
totals are checked against the sample data's raw rows."""

import datetime

import pytest

from app import queries as q

D = datetime.date


def test_mid_month_compares_the_same_dates_a_year_earlier():
    w = q.pace_windows(D(2026, 11, 15))
    assert (w['start'], w['through']) == (D(2026, 11, 1), D(2026, 11, 14))
    assert (w['last_start'], w['last_through']) == (D(2025, 11, 1), D(2025, 11, 14))
    assert (w['days_done'], w['days_in_month'], w['finished']) == (14, 30, False)


def test_the_first_of_the_month_shows_last_month_finished():
    w = q.pace_windows(D(2026, 11, 1))
    assert (w['start'], w['through']) == (D(2026, 10, 1), D(2026, 10, 31))
    assert w['finished'] is True
    assert w['last_through'] == D(2025, 10, 31)


def test_new_years_day_shows_december_against_the_december_before():
    w = q.pace_windows(D(2027, 1, 1))
    assert (w['start'], w['through']) == (D(2026, 12, 1), D(2026, 12, 31))
    assert (w['last_start'], w['last_through']) == (D(2025, 12, 1), D(2025, 12, 31))


def test_a_leap_day_stops_last_years_window_at_the_28th():
    w = q.pace_windows(D(2028, 3, 1))
    assert (w['through'], w['days_in_month']) == (D(2028, 2, 29), 29)
    assert w['last_through'] == D(2027, 2, 28)
    assert (w['last_days'], w['last_days_in_month'], w['last_month_end']) == (28, 28, D(2027, 2, 28))


def test_the_year_after_a_leap_february_counts_its_29_days_from_the_28():
    w = q.pace_windows(D(2029, 3, 1))
    assert (w['through'], w['days_in_month']) == (D(2029, 2, 28), 28)
    assert (w['last_through'], w['last_days'], w['last_days_in_month']) == (D(2028, 2, 28), 28, 29)


WINDOW = q.pace_windows(D(2026, 11, 11))  # 10 days done of 30, 10 compared, 30 last year


def test_a_row_compares_and_projects():
    r = q.pace_row(100.0, 80.0, 300.0, WINDOW, 10, 10, 30)
    assert r['now_kwh'] == 100.0
    assert r['last_kwh'] == 80.0
    assert r['last_full_kwh'] == 300.0
    assert r['projected_kwh'] == pytest.approx(300.0)
    assert r['change'] == pytest.approx(0.25)
    assert r['is_new'] is False


def test_missing_days_leave_the_projection_on_the_days_with_data():
    assert q.pace_row(80.0, 80.0, 300.0, WINDOW, 8, 10, 30)['projected_kwh'] == pytest.approx(300.0)


def test_a_part_of_last_years_window_gives_no_comparison():
    r = q.pace_row(100.0, 40.0, 300.0, WINDOW, 10, 5, 30)
    assert r['last_kwh'] is None
    assert r['change'] is None
    assert r['is_new'] is False


def test_a_part_of_last_years_month_gives_no_whole_month():
    assert q.pace_row(100.0, 80.0, 200.0, WINDOW, 10, 10, 20)['last_full_kwh'] is None


def test_a_circuit_with_nothing_last_year_is_new_not_infinite():
    r = q.pace_row(5.0, 0.0, 0.0, WINDOW, 10, 10, 30)
    assert r['change'] is None
    assert r['is_new'] is True


def test_no_data_this_month_projects_nothing():
    assert q.pace_row(0.0, 80.0, 300.0, WINDOW, 0, 10, 30)['projected_kwh'] is None


@pytest.fixture(scope='module')
def pace(client):
    return q.month_pace(D.today())


def test_sample_data_leads_with_the_whole_house(pace):
    first = pace['rows'][0]
    assert (first['circuit_id'], first['name']) == (q.WHOLE_HOUSE, 'Whole house')
    assert first['now_kwh'] > 0
    assert first['last_kwh'] > 0
    assert len(pace['rows']) > 1


def test_sample_data_totals_match_the_raw_rows(pace):
    tz = q.TZ
    with q.pool().connection() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT SUM(energy_kwh)::float FROM panel_readings
            WHERE metric = 'building_total'
              AND ts >= %s::timestamp AT TIME ZONE '{tz}'
              AND ts <  %s::timestamp AT TIME ZONE '{tz}';
            """,
            (pace['last_start'], pace['last_through'] + datetime.timedelta(days=1)),
        )
        expected = cur.fetchone()[0]
    assert pace['rows'][0]['last_kwh'] == pytest.approx(expected)


def _pace_with(today, now_days, last_days_seen, change):
    w = q.pace_windows(today)
    row = {'circuit_id': q.WHOLE_HOUSE, 'name': 'Whole house', 'now_kwh': 100.0, 'last_kwh': 80.0,
           'last_full_kwh': 300.0, 'projected_kwh': 150.0, 'change': change, 'is_new': False}
    return {**w, 'now_days': now_days, 'last_days_seen': last_days_seen, 'rows': [row]}


def test_a_finished_month_says_so_and_leaves_out_the_projection_note(client, monkeypatch):
    monkeypatch.setattr('app.main.month_pace', lambda: _pace_with(D(2026, 11, 1), 31, 31, 0.25))
    r = client.get('/pace')
    assert 'October 2026, finished' in r.text
    assert 'Finished at' in r.text
    assert 'daily average' not in r.text
    assert '▲ 25% more' in r.text


def test_missing_days_are_named_on_the_page(client, monkeypatch):
    monkeypatch.setattr('app.main.month_pace', lambda: _pace_with(D(2026, 11, 11), 8, 6, -0.25))
    r = client.get('/pace')
    assert '2 of these 10 days have no data' in r.text
    assert 'data for only 6 of the 10 days' in r.text
    assert '▼ 25% less' in r.text


def test_the_page_says_what_it_compares(client):
    r = client.get('/pace')
    assert r.status_code == 200
    assert 'Same days' in r.text
    assert 'Whole house' in r.text
    assert '$' not in r.text.split('<section class="panel">', 1)[1].split('</section>', 1)[0]
