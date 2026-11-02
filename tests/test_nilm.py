"""Load disaggregation with made-up events and samples: the energy each step
reports, and that the factorial HMM recovers a square wave it was built for."""

import datetime

import numpy as np
import pytest

from app import nilm_fhmm as fhmm
from app import queries as q

UTC = datetime.timezone.utc
T0 = datetime.datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def new_york(monkeypatch):
    monkeypatch.setattr(q, 'TZ', 'America/New_York')


def event(minutes_after, on_min=10.0, avg_w=1000.0, kwh=0.1):
    start = T0 + datetime.timedelta(minutes=minutes_after)
    end = start + datetime.timedelta(minutes=on_min)
    return {'start': start.isoformat(), 'end': end.isoformat(), 'duration_min': on_min,
            'duration_s': int(on_min * 60), 'avg_w': avg_w, 'peak_w': avg_w * 1.1, 'kwh': kwh}


def durations(*mins):
    return [event(i, on_min=m) for i, m in enumerate(mins)]


# ---- Events and clusters ----------------------------------------------------

def event_row(avg_w, duration_s=360, start=T0):
    return {'start_ts': start, 'end_ts': start, 'avg_w': avg_w, 'peak_w': avg_w, 'min_w': avg_w,
            'step_w': avg_w - 10, 'samples': 120, 'duration_s': duration_s}


def stats(n=100):
    return {'n': n, 'base_w': 10.0, 'median_w': 12.0, 'p95_w': 900.0, 'peak_w': 1100.0}


def test_event_energy_is_power_over_time(fake_db):
    fake_db([{'name': 'Kitchen'}], [stats()], [event_row(1000.0, duration_s=360)])
    d = q.discovered_loads_for_circuit('s1')
    e = d['events'][0]
    assert e['kwh'] == pytest.approx(0.1)            # 1 kW for 6 minutes
    assert e['duration_min'] == 6.0
    assert d['clusters'][0]['total_kwh'] == pytest.approx(0.1)
    assert d['space_name'] == 'Kitchen'
    assert d['baseline_w'] == 10.0


def test_clusters_bin_to_50_w_and_rank_by_energy(fake_db):
    fake_db([{'name': 'Kitchen'}], [stats()], [
        event_row(1024.0), event_row(1026.0), event_row(1040.0), event_row(300.0, duration_s=36000)])
    clusters = q.discovered_loads_for_circuit('s1')['clusters']
    assert [(c['amplitude_bin_w'], c['count']) for c in clusters] == [(300, 1), (1050, 2), (1000, 1)]


def test_no_samples_no_event_query(fake_db):
    db = fake_db([], [stats(n=0)])
    d = q.discovered_loads_for_circuit('__feedthrough__')
    assert d['events'] == [] and d['sample_count'] == 0
    assert len(db.calls) == 2


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "app/queries.py:1697: as ev_charging_sessions, .date() is the session's day, not TZ's, so "
    'a 21:00 event sits under the next date on /discover'))
def test_event_date_is_the_local_day(fake_db):
    evening = datetime.datetime(2026, 3, 5, 2, 0, tzinfo=UTC)     # 21:00 on the 4th in New York
    fake_db([{'name': 'Kitchen'}], [stats()], [event_row(1000.0, start=evening)])
    assert q.discovered_loads_for_circuit('s1')['events'][0]['date'] == '2026-03-04'


def test_split_by_duration_when_clearly_bimodal():
    groups = q._split_by_duration(durations(2, 2.1, 2.2, 12, 12.1, 12.2))
    assert [(len(evs), label) for evs, label in groups] == [(3, 'short'), (3, 'long')]
    assert max(e['duration_min'] for e in groups[0][0]) == 2.2


@pytest.mark.parametrize('mins', [
    (2, 2.1, 2.2, 12, 12.1),            # too few events
    (5, 5, 5, 5, 5, 5),                 # no spread at all
    (2, 2, 2, 2, 2, 12),                # one outlier isn't a second mode
    (1, 2, 3, 4, 5, 6),                 # evenly spread
])
def test_split_by_duration_otherwise_leaves_it(mins):
    groups = q._split_by_duration(durations(*mins))
    assert [(len(evs), label) for evs, label in groups] == [(len(mins), '')]


def test_cluster_summary_uses_local_hours():
    evs = [event(0, on_min=5, kwh=0.1), event(60, on_min=7, kwh=0.2), event(120, on_min=9, kwh=0.3)]
    evs[0]['start'] = '2026-07-01T01:30:00+00:00'    # 21:30 EDT
    evs[1]['start'] = '2026-07-02T01:40:00+00:00'    # 21:40 EDT
    evs[2]['start'] = '2026-01-15T01:30:00+00:00'    # 20:30 EST
    c = q._summarize_cluster(1000, evs, '', days=7)
    assert c['hour_distribution'][21] == 2 and c['hour_distribution'][20] == 1
    assert c['peak_hour'] == 21
    assert c['total_kwh'] == pytest.approx(0.6)
    assert c['total_min'] == 21.0
    assert c['median_duration_min'] == 7


@pytest.mark.parametrize('amp, durs, count, days, expected', [
    (50, [10.0], 3, 7, 'phantom load (always-on)'),
    (900, [12.0], 14, 7, 'window AC (thermostat cycling)'),
    (1200, [10.0], 2, 7, 'air fryer / slow cooker / electric pan'),
    (1200, [3.0], 2, 7, 'microwave (medium reheat)'),
    (1500, [40.0], 2, 7, 'air fryer / slow cooker / electric pan'),
    (1700, [40.0], 2, 7, 'dishwasher heating element'),
    (200, [2.0], 2, 7, 'fan / small motor (kicked on briefly)'),
    (4000, [30.0], 2, 7, 'high-draw appliance (oven / dryer / EVSE)'),
    (450, [20.0], 2, 7, 'fridge compressor or similar cycle'),
    (2000, [8.0], 2, 7, 'unclassified (2000 W, 8.0 min typical)'),
])
def test_classify_load(amp, durs, count, days, expected):
    assert q._classify_load(amp, durs, count, days) == expected


def test_classify_load_uses_the_time_of_day():
    morning = [0] * 24
    morning[7] = 5
    assert q._classify_load(1200, [1.0], 5, 7, hour_dist=morning, peak_hour=7) == (
        'kettle / coffee maker (morning burst)')
    daytime = [0] * 24
    daytime[13] = 5
    assert q._classify_load(450, [20.0], 5, 7, hour_dist=daytime, peak_hour=13) == (
        'small heat pump / mini-split cycle')


# ---- Thermostat cycling -----------------------------------------------------

def cycling(monkeypatch, events):
    monkeypatch.setattr(q, 'discovered_loads_for_circuit', lambda *a, **k: {
        'events': events, 'space_name': 'Attic'})


def test_cycling_run(monkeypatch):
    # Six 10-minute firings, one every 20 minutes.
    cycling(monkeypatch, [event(i * 20) for i in range(6)])
    runs = q.detect_thermostat_cycling('s1')
    assert len(runs) == 1
    r = runs[0]
    assert (r['cycles'], r['period_min'], r['on_duration_min'], r['off_duration_min']) == (6, 20.0, 10.0, 10.0)
    assert r['duty_pct'] == 50.0
    assert r['cov'] == 0.0
    assert r['amplitude_w'] == 1000.0
    assert r['total_kwh'] == pytest.approx(0.6)


def test_a_different_load_breaks_the_run(monkeypatch):
    events = [event(i * 20, avg_w=2000.0 if i >= 3 else 1000.0) for i in range(6)]
    cycling(monkeypatch, events)
    assert q.detect_thermostat_cycling('s1') == []     # 3 + 3: neither reaches 4


def test_irregular_spacing_is_not_a_thermostat(monkeypatch):
    starts = [0, 3, 44, 47, 88, 91]                    # gaps of 3 and 41 minutes
    cycling(monkeypatch, [event(s, on_min=1.0) for s in starts])
    assert q.detect_thermostat_cycling('s1') == []


def test_a_short_run_is_not_a_thermostat(monkeypatch):
    cycling(monkeypatch, [event(i * 5, on_min=1.0) for i in range(4)])     # 16 minutes
    assert q.detect_thermostat_cycling('s1') == []


def test_cluster_history_days_are_local(monkeypatch):
    monday_night = event(0, kwh=0.25)
    monday_night['start'] = '2026-07-07T03:30:00+00:00'  # Monday 2026-07-06 23:30 EDT
    tuesday = event(0, kwh=0.5)
    tuesday['start'] = '2026-07-07T04:30:00+00:00'       # Tuesday 00:30 EDT
    other = event(0, avg_w=300.0)
    monkeypatch.setattr(q, 'discovered_loads_for_circuit', lambda *a, **k: {
        'space_name': 'Kitchen', 'events': [monday_night, tuesday, other],
        'clusters': [{'amplitude_bin_w': 1000, 'duration_label': ''}]})
    h = q.cluster_event_history('s1', 1000)
    assert h['daily_kwh'] == [{'date': '2026-07-06', 'kwh': 0.25}, {'date': '2026-07-07', 'kwh': 0.5}]
    assert h['dow_dist'][:2] == [1, 1]
    assert (h['hour_dist'][23], h['hour_dist'][0]) == (1, 1)
    assert len(h['events']) == 2


def test_cluster_history_unknown_cluster(monkeypatch):
    monkeypatch.setattr(q, 'discovered_loads_for_circuit', lambda *a, **k: {
        'space_name': 'Kitchen', 'events': [], 'clusters': []})
    h = q.cluster_event_history('s1', 1000)
    assert h['cluster'] is None and h['hour_dist'] == [0] * 24


def test_cooccurrence_lift(monkeypatch, fake_db):
    monkeypatch.setattr(q, 'list_live_circuits', lambda: [
        {'space_id': 'a', 'name': 'AC'}, {'space_id': 'b', 'name': 'Fan'}])
    monkeypatch.setattr(q, 'discovered_loads_for_circuit', lambda sid, days: {'clusters': [
        {'count': 8, 'avg_amplitude_w': 1000.0, 'amplitude_bin_w': 1000, 'hypothesis': 'x',
         'duration_label': ''}]})
    minutes = [T0 + datetime.timedelta(minutes=i) for i in range(10)]
    db = fake_db([{'mb': m} for m in minutes], [{'mb': m} for m in minutes])
    rows = q.cluster_cooccurrence(days=1)
    assert len(rows) == 1
    # Both on for the same 10 of 1440 minutes: lift = (10/1440) / (10/1440)^2.
    assert rows[0]['lift'] == pytest.approx(144.0)
    assert rows[0]['minutes_both'] == 10
    assert db.calls[0][1][3] == 700.0                  # 70 % of the amplitude


# ---- FHMM -------------------------------------------------------------------

def appliance(mu, name='a', on=20.0, off=100.0):
    return {'name': name, 'mu_w': mu, 'on_samples': on, 'off_samples': off, 'hypothesis': name,
            'amplitude_bin_w': int(mu)}


def test_transition_matrix():
    m = fhmm.build_transition_matrix(on_samples=20.0, off_samples=100.0)
    assert m.flatten().tolist() == pytest.approx([0.99, 0.01, 0.05, 0.95])


def test_fit_from_clusters_caps_the_appliances():
    cluster = {'avg_amplitude_w': 1234.4, 'median_duration_min': 2.0, 'count': 5, 'total_min': 10.0,
               'hypothesis': 'kettle', 'amplitude_bin_w': 1250}
    fit = fhmm.fit_appliances_from_clusters([cluster] * (fhmm.MAX_APPLIANCES + 2))
    assert len(fit) == fhmm.MAX_APPLIANCES
    assert fit[0]['name'] == '1234 W kettle'
    assert (fit[0]['on_samples'], fit[0]['off_samples']) == (40.0, 200.0)    # 20 samples a minute


def test_viterbi_recovers_a_square_wave():
    obs = np.array([0.0] * 20 + [1000.0] * 20 + [0.0] * 20)
    states = fhmm.viterbi_fhmm(obs, [appliance(1000.0)])
    assert states[:, 0].tolist() == [0] * 20 + [1] * 20 + [0] * 20


def test_viterbi_separates_two_overlapping_loads():
    obs = np.array([0.0] * 5 + [1000.0] * 5 + [1300.0] * 5 + [300.0] * 5 + [0.0] * 5)
    states = fhmm.viterbi_fhmm(obs, [appliance(1000.0), appliance(300.0)])
    assert states[:, 0].tolist() == [0] * 5 + [1] * 10 + [0] * 10
    assert states[:, 1].tolist() == [0] * 10 + [1] * 10 + [0] * 5


def test_viterbi_starts_with_everything_off():
    states = fhmm.viterbi_fhmm(np.array([1000.0]), [appliance(1000.0)])
    assert states.tolist() == [[0]]


def test_viterbi_edges():
    assert fhmm.viterbi_fhmm(np.array([]), [appliance(1.0)]).shape == (0, 1)
    assert fhmm.viterbi_fhmm(np.array([1.0, 2.0]), []).shape == (2, 0)
    with pytest.raises(ValueError):
        fhmm.viterbi_fhmm(np.array([1.0]), [appliance(1.0)] * (fhmm.MAX_APPLIANCES + 1))


def test_attribution_energy():
    obs = np.full(1200, 1000.0)                        # 1 kW for an hour at 3 s
    out = fhmm.attribute_to_appliances(obs, np.ones((1200, 1), dtype=np.int8), [appliance(900.0)])
    assert out['total_kwh'] == pytest.approx(1.0)
    assert out['attributed_kwh'] == pytest.approx(0.9)
    assert out['residual_kwh'] == pytest.approx(0.1)
    assert out['residual_share_pct'] == pytest.approx(10.0)
    row = out['rows'][0]
    assert (row['on_seconds'], row['on_minutes']) == (3600.0, 60.0)
    assert row['share_pct'] == pytest.approx(90.0)


def test_attribution_residual_is_never_negative():
    obs = np.full(1200, 500.0)
    out = fhmm.attribute_to_appliances(obs, np.ones((1200, 1), dtype=np.int8), [appliance(900.0)])
    assert out['residual_kwh'] == 0.0


def test_attribution_of_nothing():
    out = fhmm.attribute_to_appliances(np.zeros(3), np.zeros((3, 1), dtype=np.int8), [appliance(9.0)])
    assert (out['total_kwh'], out['residual_share_pct'], out['rows'][0]['share_pct']) == (0.0, 0.0, 0.0)


def samples(powers, step_s=3.0):
    return [{'ts': T0 + datetime.timedelta(seconds=i * step_s), 'power_w': p}
            for i, p in enumerate(powers)]


CLUSTER = {'avg_amplitude_w': 1000.0, 'median_duration_min': 20.0, 'count': 1, 'total_min': 20.0,
           'hypothesis': 'heater', 'amplitude_bin_w': 1000}


def attribute(monkeypatch, fake_db, rows, clusters=(CLUSTER,), hours=24):
    monkeypatch.setattr(q, 'discovered_loads_for_circuit', lambda *a, **k: {'clusters': list(clusters)})
    fake_db([{'name': 'Attic'}], rows, module=fhmm)
    return fhmm.fhmm_attribute_circuit('s1', hours=hours)


def test_attribute_end_to_end(monkeypatch, fake_db):
    powers = [0.0] * 400 + [1000.0] * 400 + [None] * 400     # a NULL reading counts as 0 W
    r = attribute(monkeypatch, fake_db, samples(powers))
    assert r['sample_count'] == 1200
    assert r['sample_period_s'] == 3.0
    assert r['attribution']['total_kwh'] == pytest.approx(1 / 3)     # 400 x 3 s at 1 kW
    assert r['attribution']['attributed_kwh'] == pytest.approx(1 / 3)
    assert r['attribution']['residual_kwh'] == pytest.approx(0.0, abs=1e-9)
    assert len(r['chart']['ts']) == 600                  # every other sample
    assert sum(r['chart']['states'][0]['series']) == 200


@pytest.mark.parametrize('step, period', [(5.0, 5.0), (0.5, 3.0), (30.0, 3.0)])
def test_attribute_measures_the_sample_spacing_within_reason(monkeypatch, fake_db, step, period):
    r = attribute(monkeypatch, fake_db, samples([1000.0] * 50, step_s=step))
    assert r['sample_period_s'] == period


def test_attribute_without_clusters(monkeypatch, fake_db):
    r = attribute(monkeypatch, fake_db, samples([100.0] * 5), clusters=())
    assert 'No Phase-1 clusters' in r['error']
    assert r['sample_count'] == 5


def test_attribute_a_circuit_the_panel_never_reported(monkeypatch, fake_db):
    monkeypatch.setattr(q, 'discovered_loads_for_circuit', lambda *a, **k: {'clusters': []})
    fake_db([{'name': 'Sub-panel'}], [], [], module=fhmm)
    assert "isn't exposed by SPAN's local API" in fhmm.fhmm_attribute_circuit('s1')['error']


def test_attribute_with_a_stalled_poller(monkeypatch, fake_db):
    monkeypatch.setattr(q, 'discovered_loads_for_circuit', lambda *a, **k: {'clusters': []})
    fake_db([{'name': 'Attic'}], [], [(1,)], module=fhmm)
    assert 'No space_live samples in the last 6 h' in fhmm.fhmm_attribute_circuit('s1', hours=6)['error']
