"""The live path, with no panel and no broker: the snapshots span_local and
span_ebus shape from made-up payloads, live_store's tick, and the topics
mqtt_publisher sends to a fake MQTT client."""

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app import config, live_store, mqtt_publisher, span_ebus, span_local


@pytest.fixture(autouse=True)
def feedthrough_name(monkeypatch):
    monkeypatch.setattr(config, 'circuits', lambda: {'feedthrough_name': 'Sub-panel'})


# ---- span_local._shape: signs and sums --------------------------------------

def v1_panel(**over):
    return {
        'instantGridPowerW': 1000, 'feedthroughPowerW': 200,
        'mainRelayState': 'CLOSED', 'dsmGridState': 'DSM_GRID_UP', 'currentRunConfig': 'PANEL_ON_GRID',
        'branches': [
            {'id': 1, 'instantPowerW': -300}, {'id': 17, 'instantPowerW': -700},
            {'id': 30, 'instantPowerW': 600}, {'id': 32, 'instantPowerW': 600},
            {'id': 5, 'instantPowerW': 3},      # noise: not solar, but on phase A
        ], **over}


def v1_circuits():
    return {'circuits': {
        'c1': {'id': 'c1', 'name': 'Fridge', 'instantPowerW': -300, 'relayState': 'CLOSED',
               'priority': 'MUST_HAVE', 'tabs': [1]},
        'c2': {'id': 'c2', 'name': 'Dryer', 'instantPowerW': -700, 'relayState': 'CLOSED',
               'priority': 'NICE_TO_HAVE', 'tabs': [17]},
    }}


def test_v1_shape_totals():
    p = span_local._shape(v1_panel(), v1_circuits())['panel']
    assert p['grid_power_w'] == 1000.0               # + importing, as the panel reports it
    assert p['feedthrough_power_w'] == 200.0
    assert p['solar_power_w'] == 1200.0              # the two solar breakers, not the 3 W
    assert p['load_power_w'] == 1200.0               # 300 + 700 + 200
    assert p['solar_share_pct'] == 100.0
    assert (p['phase_a_w'], p['phase_b_w']) == (303.0, 1900.0)
    assert p['phase_imbalance_pct'] == pytest.approx(1597 / 1900 * 100)
    assert (p['main_relay'], p['grid_state']) == ('CLOSED', 'DSM_GRID_UP')


def test_v1_circuits_are_positive_when_consuming():
    out = span_local._shape(v1_panel(), v1_circuits())['circuits']
    assert [(c['id'], c['power_w']) for c in out] == [
        ('c2', 700.0), ('c1', 300.0), ('__feedthrough__', 200.0)]
    assert out[2]['name'] == 'Sub-panel'


def test_v1_backfeeding_circuit_is_negative_and_lowers_the_load():
    circuits = v1_circuits()
    circuits['circuits']['c1']['instantPowerW'] = 100
    p = span_local._shape(v1_panel(), circuits)
    assert p['panel']['load_power_w'] == 800.0       # -100 + 700 + 200
    assert p['circuits'][-1]['power_w'] == -100.0


def test_v1_sub_panel_backfeed_subtracts():
    p = span_local._shape(v1_panel(feedthroughPowerW=-50), v1_circuits())['panel']
    assert p['load_power_w'] == 950.0


def test_v1_share_is_a_fraction_of_demand():
    panel = v1_panel(branches=[{'id': 30, 'instantPowerW': 300}])
    p = span_local._shape(panel, v1_circuits())['panel']
    assert p['solar_share_pct'] == 25.0              # 300 of 1200


def test_v1_no_demand_has_no_share_or_imbalance():
    p = span_local._shape({'instantGridPowerW': 0, 'branches': []}, {'circuits': {}})['panel']
    assert p['load_power_w'] == 0.0
    assert p['solar_share_pct'] is None
    assert p['phase_imbalance_pct'] is None


def test_v1_tolerates_bad_numbers():
    circuits = {'circuits': {'c1': {'id': 'c1', 'instantPowerW': 'n/a'},
                             'junk': 'not a circuit'}}
    out = span_local._shape({'instantGridPowerW': None, 'branches': [{'id': 1}]}, circuits)
    assert out['panel']['grid_power_w'] is None
    assert out['circuits'] == [{'id': 'c1', 'name': '(unnamed)', 'power_w': None, 'relay': None,
                                'priority': None, 'tabs': None}]


def test_v1_snapshot_without_credentials_is_an_error_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(span_local, 'LOCAL_CREDS', tmp_path / 'none.json')
    monkeypatch.setattr(span_local, '_client', None)
    snap = span_local.snapshot()
    assert 'credentials file not found' in snap['error']
    assert (snap['panel'], snap['circuits']) == (None, [])


# ---- span_ebus: the same snapshot from the broker ---------------------------

class FakeMqtt:
    """paho.mqtt.client.Client, minus the network."""

    def __init__(self, *_args, **kwargs):
        self.client_id = kwargs.get('client_id')
        self.published = []
        self.subscribed = []
        self.will = None
        self.connected_to = None

    def __getattr__(self, name):
        return lambda *a, **k: None

    def subscribe(self, topic, qos=0):
        self.subscribed.append(topic)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append(SimpleNamespace(topic=topic, payload=payload, qos=qos, retain=retain))

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = (topic, payload, retain)

    def connect_async(self, host, port, keepalive=60):
        self.connected_to = (host, port)


class Reason:
    is_failure = False


class Refused:
    is_failure = True

    def __str__(self):
        return 'Not authorized'


CREDS = {'broker_host': '192.0.2.10', 'broker_mqtts_port': 8883, 'serial_number': 'sim-panel',
         'broker_username': 'sim-panel', 'broker_password': 'made-up', 'ca_path': 'unused.pem'}


@pytest.fixture
def ebus(monkeypatch):
    monkeypatch.setattr(span_ebus.mqtt, 'Client', FakeMqtt)
    monkeypatch.setattr(span_ebus.ssl, 'create_default_context',
                        lambda cafile=None: SimpleNamespace(verify_flags=0))
    monkeypatch.delenv('SPAN_HOST', raising=False)
    return span_ebus.EbusState(dict(CREDS))


def send(state, device, rest, payload):
    state._on_message(None, None, SimpleNamespace(topic=f'ebus/5/{device}/{rest}',
                                                  payload=str(payload).encode()))


def describe(state, device, kind):
    send(state, device, '$description', json.dumps({'type': f'energy.ebus.device.{kind}'}))


def house(state):
    """Solar 1200 W, importing 1000 W: the same house as v1_panel()."""
    state._on_connect(state._client, None, None, Reason())
    send(state, 'sim-panel', '$state', 'ready')
    send(state, 'sim-panel', 'meter/voltage-a', 121.5)
    send(state, 'sim-panel', 'meter/voltage-b', 120.5)
    send(state, 'sim-panel', 'status/relay', 'CLOSED')
    # Derived and inverted: this must never become grid_power_w.
    send(state, 'sim-panel', 'power-flows/grid', -1000)
    describe(state, 'lugs-up', 'lugs')
    send(state, 'lugs-up', 'info/direction', 'UPSTREAM')
    send(state, 'lugs-up', 'meter/active-power', 1000)
    describe(state, 'lugs-dn', 'lugs')
    send(state, 'lugs-dn', 'info/direction', 'DOWNSTREAM')
    send(state, 'lugs-dn', 'meter/active-power', 200)
    describe(state, 'c1', 'circuit')
    send(state, 'c1', 'info/name', 'Fridge')
    send(state, 'c1', 'info/spaces', '1,3')
    send(state, 'c1', 'meter/active-power', -300)
    describe(state, 'c2', 'circuit')
    send(state, 'c2', 'info/name', 'Dryer')
    send(state, 'c2', 'info/spaces', '17')
    send(state, 'c2', 'meter/active-power', -700)
    describe(state, 'pv1', 'pv')
    describe(state, 'c3', 'circuit')
    send(state, 'c3', 'info/name', 'Solar breaker')
    send(state, 'c3', 'info/spaces', '30,32')
    send(state, 'c3', 'connection/feeds-device-id', 'pv1')
    send(state, 'c3', 'meter/active-power', 1200)


def test_ebus_totals_match_the_v1_shape(ebus):
    house(ebus)
    p = ebus.snapshot()['panel']
    assert p['grid_power_w'] == 1000.0
    assert p['feedthrough_power_w'] == 200.0
    assert p['solar_power_w'] == 1200.0
    assert p['load_power_w'] == 1200.0
    assert p['solar_share_pct'] == 100.0
    assert (p['phase_a_w'], p['phase_b_w']) == (300.0, 1900.0)
    assert p['phase_imbalance_pct'] == pytest.approx(1600 / 1900 * 100)
    assert (p['main_relay'], p['grid_state']) == ('CLOSED', 'DSM_GRID_UP')
    assert p['run_config'] is None


def test_ebus_leaves_the_solar_breaker_out_of_circuits(ebus):
    house(ebus)
    snap = ebus.snapshot()
    assert [(c['id'], c['power_w'], c['tabs']) for c in snap['circuits']] == [
        ('c2', 700.0, [17]), ('c1', 300.0, [1, 3]), ('__feedthrough__', 200.0, None)]
    assert snap['raw']['pv_circuits'] == 1


def test_ebus_forward_fills_silent_properties(ebus):
    house(ebus)
    send(ebus, 'lugs-up', 'meter/active-power', -450)
    p = ebus.snapshot()['panel']
    assert p['grid_power_w'] == -450.0               # exporting
    assert p['load_power_w'] == 1200.0               # the rest stay as last reported


def test_ebus_night_time_inverter_draw_is_neither_solar_nor_load(ebus):
    house(ebus)
    send(ebus, 'c3', 'meter/active-power', -4)
    p = ebus.snapshot()['panel']
    assert p['solar_power_w'] == 0.0
    assert p['load_power_w'] == 1200.0


def test_ebus_grid_state_follows_the_bus_voltage(ebus):
    house(ebus)
    send(ebus, 'sim-panel', 'meter/voltage-b', 85)
    assert ebus.snapshot()['panel']['grid_state'] == 'DSM_GRID_DOWN'


def test_ebus_ignores_set_topics_and_undecodable_payloads(ebus):
    house(ebus)
    send(ebus, 'c1', 'switch/relay/set', 'OPEN')
    ebus._on_message(None, None, SimpleNamespace(topic='ebus/5/c1/meter/active-power',
                                                 payload=b'\xff\xfe'))
    assert ('c1', 'switch/relay/set') not in ebus._props
    assert ebus.snapshot()['circuits'][1]['power_w'] == 300.0


def test_ebus_is_an_error_until_connected(ebus):
    assert ebus.snapshot()['error'] == 'not connected yet'


def test_ebus_refused_connection(ebus):
    ebus._on_connect(ebus._client, None, None, Refused())
    assert 'broker refused the connection' in ebus.snapshot()['error']


def test_ebus_disconnect_is_an_error_not_stale_numbers(ebus):
    house(ebus)
    ebus._on_disconnect(None, None, None, 'keepalive timeout')
    assert 'disconnected from the broker' in ebus.snapshot()['error']


def test_ebus_silence_is_an_error_not_stale_numbers(ebus):
    house(ebus)
    ebus._last_msg_at = time.time() - span_ebus.STALE_AFTER_S - 1
    snap = ebus.snapshot()
    assert 'no message from the panel' in snap['error']
    assert snap['panel'] is None


def test_ebus_panel_not_ready(ebus):
    house(ebus)
    send(ebus, 'sim-panel', '$state', 'init')
    assert "not ready" in ebus.snapshot()['error']


def test_ebus_needs_the_grid_meter(ebus):
    ebus._on_connect(ebus._client, None, None, Reason())
    send(ebus, 'sim-panel', '$state', 'ready')
    assert ebus.snapshot()['error'] == 'no upstream lugs device seen yet'


def test_ebus_subscribes_to_the_tree_and_never_publishes(ebus):
    house(ebus)
    assert ebus._client.subscribed == ['ebus/5/#']
    assert ebus._client.published == []


def test_ebus_refuses_a_local_name(monkeypatch):
    monkeypatch.setattr(span_ebus.mqtt, 'Client', FakeMqtt)
    monkeypatch.delenv('SPAN_HOST', raising=False)
    with pytest.raises(ValueError):
        span_ebus.EbusState({**CREDS, 'broker_host': 'span-sim.local'})


def test_ebus_spaces_parsing():
    assert span_ebus._spaces('1,,x,3') == [1, 3]
    assert span_ebus._spaces(None) == []


# ---- live_store -------------------------------------------------------------

GOOD = {'error': None, 'panel': {'grid_power_w': 1.0}, 'circuits': [], 'raw': {'source': 'ebus'}}


def good():
    return dict(GOOD)


def bad(msg):
    return {'error': msg, 'panel': None, 'circuits': [], 'raw': {'source': 'ebus'}}


@pytest.fixture
def source(monkeypatch):
    def use(mode, available=True, ebus=None, v1=None):
        monkeypatch.setattr(live_store, 'SOURCE', mode)
        monkeypatch.setattr(live_store.span_ebus, 'available', lambda: available)
        monkeypatch.setattr(live_store.span_ebus, 'snapshot', ebus or good)
        monkeypatch.setattr(live_store, 'span_local_snapshot',
                            v1 or (lambda: {'error': None, 'panel': {}, 'circuits': [], 'raw': None}))
    return use


def test_source_ebus_serves_directly(source):
    source('ebus')
    assert live_store.source_snapshot()['raw'] == {'source': 'ebus'}


def test_source_ebus_failure_falls_back_to_v1_and_says_so(source):
    source('ebus', ebus=lambda: bad('broker down'))
    assert live_store.source_snapshot()['raw'] == {'source': 'v1-fallback', 'ebus_error': 'broker down'}


def test_source_both_down_reports_the_ebus_error(source):
    source('ebus', ebus=lambda: bad('broker down'), v1=lambda: bad('v1 down'))
    assert live_store.source_snapshot()['error'] == 'broker down'


@pytest.mark.parametrize('mode, available', [('v1', True), ('shadow', True), ('ebus', False)])
def test_source_v1_cases(source, mode, available):
    source(mode, available=available)
    assert live_store.source_snapshot()['raw'] == {'source': 'v1'}


@pytest.fixture
def tick(monkeypatch, source):
    """_tick_sync with its DB writes and MQTT publish recorded."""
    seen = SimpleNamespace(panel=[], circuits=[], mqtt=[])
    monkeypatch.setattr(live_store, '_insert_live_row', seen.panel.append)
    monkeypatch.setattr(live_store, '_insert_space_live_rows', seen.circuits.append)
    monkeypatch.setattr(live_store, 'mqtt_publish', seen.mqtt.append)
    monkeypatch.setattr(live_store, '_latest_snapshot', None)
    monkeypatch.setattr(live_store, '_latest_snapshot_at', None)
    monkeypatch.setattr(live_store, '_outages', live_store._Outages())
    return seen


def test_tick_writes_a_good_snapshot(source, tick):
    source('ebus')
    live_store._tick_sync()
    assert tick.panel == [GOOD['panel']]
    assert tick.circuits == [[]]
    assert tick.mqtt[0]['error'] is None
    assert live_store.latest_snapshot()['panel'] == GOOD['panel']
    assert live_store.latest_snapshot_age_s() < 5


def test_tick_never_stores_an_error_snapshot(source, tick):
    source('ebus', ebus=lambda: bad('broker down'), v1=lambda: bad('v1 down'))
    live_store._tick_sync()
    assert tick.panel == [] and tick.circuits == []
    assert live_store.latest_snapshot()['error'] == 'broker down'


def test_tick_survives_a_database_failure(monkeypatch, source, tick):
    source('ebus')

    def broken(_panel):
        raise RuntimeError('db down')

    monkeypatch.setattr(live_store, '_insert_live_row', broken)
    live_store._tick_sync()
    assert live_store.latest_snapshot() is not None
    assert tick.circuits == [[]] and len(tick.mqtt) == 1


def test_panel_row_columns_line_up(fake_db):
    db = fake_db([], module=live_store)
    live_store._insert_live_row({
        'grid_power_w': 1.0, 'feedthrough_power_w': 2.0, 'solar_power_w': 3.0,
        'load_power_w': 4.0, 'phase_a_w': 5.0, 'phase_b_w': 6.0})
    sql, params = db.calls[0]
    assert 'ts, grid_power_w, feedthrough_power_w, solar_power_w,' in ' '.join(sql.split())
    assert params == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)


def test_circuit_rows_keep_the_sign_and_skip_idless_ones(fake_db):
    db = fake_db(module=live_store)
    live_store._insert_space_live_rows([{'id': 'c1', 'power_w': 300.0}, {'power_w': 9.0},
                                        {'id': '__feedthrough__', 'power_w': -50.0}])
    assert db.calls[0][1] == [('c1', 300.0), ('__feedthrough__', -50.0)]


def test_circuit_rows_with_nothing_to_write(fake_db):
    db = fake_db(module=live_store)
    live_store._insert_space_live_rows([])
    live_store._insert_space_live_rows([{'power_w': 9.0}])
    assert db.calls == []


def test_outage_accounting():
    o = live_store._Outages()
    ok = {'raw': {'source': 'ebus'}}
    fallback = {'raw': {'source': 'v1-fallback', 'ebus_error': 'broker down'}}
    neither = {'error': 'broker down', 'raw': {'source': 'ebus'}}
    for snap in (ok, fallback, fallback, neither, ok, ok, fallback):
        o.observe(snap)
    r = o.report()
    assert (r['ticks'], r['fallback_ticks'], r['error_ticks'], r['outages']) == (7, 3, 1, 2)
    assert r['current_outage_since'] is not None
    assert r['last_outage']['reason'] == 'broker down'
    assert (r['last_outage']['fallback_ticks'], r['last_outage']['error_ticks']) == (2, 1)


def shadow_snap(grid, circuits):
    return {'error': None,
            'panel': {'grid_power_w': grid, 'solar_power_w': None},
            'circuits': [{'id': i, 'power_w': p, 'name': 'n', 'tabs': [1], 'relay': 'CLOSED'}
                         for i, p in circuits]}


def test_shadow_compares_the_same_tick():
    s = live_store._Shadow()
    s.observe(shadow_snap(100.0, [('a', 10.0), ('b', 20.0)]),
              shadow_snap(103.0, [('a', 14.0), ('c', 1.0)]))
    s.observe({'error': 'v1 down'}, shadow_snap(1.0, []))
    r = s.report()
    assert (r['ticks'], r['compared_ticks'], r['v1_errors']) == (2, 1, 1)
    assert r['fields']['grid_power_w'] == {'n': 1, 'median': 3.0, 'p95': 3.0, 'max': 3.0}
    assert r['fields']['solar_power_w']['n'] == 0
    assert r['circuits']['median'] == 4.0
    assert r['id_mismatch_example'] == {'only_v1': ['b'], 'only_ebus': ['c']}


def test_the_poller_is_off_when_told_to(monkeypatch):
    monkeypatch.setattr(live_store, 'SOURCE', 'off')
    asyncio.run(live_store.poller_loop())


# ---- mqtt_publisher ---------------------------------------------------------

@pytest.fixture
def publisher(monkeypatch):
    monkeypatch.setattr(mqtt_publisher.mqtt, 'Client', FakeMqtt)
    monkeypatch.delenv('MQTT_HA_PREFIX', raising=False)
    return mqtt_publisher._Publisher({'host': '192.0.2.20', 'username': 'u', 'password': 'p'})


def topics(p):
    return {m.topic: m.payload for m in p.client.published}


SNAP = {
    'error': None,
    'panel': {'grid_power_w': -450.5, 'solar_power_w': 1200.0, 'load_power_w': 749.5,
              'feedthrough_power_w': None, 'solar_share_pct': 100.0, 'phase_a_w': 300.0,
              'phase_b_w': 449.5, 'phase_imbalance_pct': 33.2,
              'main_relay': 'CLOSED', 'grid_state': 'DSM_GRID_UP', 'run_config': None},
    'circuits': [{'id': '__feedthrough__', 'name': 'Sub-panel', 'power_w': 200.0},
                 {'id': 'c/1', 'name': 'Dryer', 'power_w': -5.5},
                 {'id': 'c2', 'name': None, 'power_w': None},
                 {'id': None, 'name': 'ghost', 'power_w': 1.0}],
}


def test_panel_fields_publish_with_their_sign(publisher):
    publisher.publish_snapshot(SNAP)
    t = topics(publisher)
    assert t['span/panel/grid_power_w'] == '-450.500'
    assert t['span/panel/load_power_w'] == '749.500'
    assert t['span/panel/main_relay'] == 'CLOSED'
    assert t['span/panel/grid_up'] == '1'
    assert 'span/panel/feedthrough_power_w' not in t     # unknown keeps the retained value
    assert 'span/panel/run_config' not in t


def test_every_publish_is_retained_at_qos_0(publisher):
    publisher.publish_snapshot(SNAP)
    assert all(m.retain and m.qos == 0 for m in publisher.client.published)


@pytest.mark.parametrize('state, up', [('DSM_GRID_UP', '1'), ('DSM_GRID_DOWN', '0'),
                                       ('DSM_ON_GRID', None), (None, None)])
def test_grid_up_is_a_number_for_known_states(publisher, state, up):
    publisher.publish_snapshot({**SNAP, 'panel': {**SNAP['panel'], 'grid_state': state}})
    assert topics(publisher).get('span/panel/grid_up') == up


def test_circuit_topics_use_a_safe_id(publisher):
    publisher.publish_snapshot(SNAP)
    t = topics(publisher)
    assert t['span/circuit/feedthrough/power_w'] == '200.000'
    assert t['span/circuit/c_1/power_w'] == '-5.500'
    assert 'span/circuit/c2/power_w' not in t
    assert t['span/circuit/c2/name'] == 'c2'             # no name: the id
    assert not [k for k in t if 'ghost' in k or k.startswith('span/circuit//')]


def test_circuit_discovery_matches_the_state_topic(publisher):
    publisher.publish_snapshot(SNAP)
    cfg = json.loads(topics(publisher)['homeassistant/sensor/spanviewer_circuit_c_1/config'])
    assert cfg['state_topic'] == 'span/circuit/c_1/power_w'
    assert (cfg['unit_of_measurement'], cfg['device_class']) == ('W', 'power')
    assert cfg['name'] == 'Circuit · Dryer'


def test_discovery_is_once_per_connection(publisher):
    publisher.publish_snapshot(SNAP)
    publisher.client.published.clear()
    publisher.publish_snapshot(SNAP)
    assert not [m for m in publisher.client.published if m.topic.endswith('/config')]
    publisher._on_connect(publisher.client, None, None, 0)
    publisher.publish_snapshot(SNAP)
    assert 'homeassistant/sensor/spanviewer_circuit_c_1/config' in topics(publisher)


def test_connect_goes_online_and_announces_the_panel(publisher):
    assert publisher.client.will == ('span/status', 'offline', True)
    publisher._on_connect(publisher.client, None, None, 0)
    t = topics(publisher)
    assert t['span/status'] == 'online'
    cfg = json.loads(t['homeassistant/sensor/spanviewer_grid_power_w/config'])
    assert cfg['state_topic'] == 'span/panel/grid_power_w'
    assert cfg['availability_topic'] == 'span/status'


def test_refused_connect_publishes_nothing(publisher):
    publisher._on_connect(publisher.client, None, None, 5)
    assert publisher.client.published == []


def test_error_snapshot_leaves_the_last_good_values(publisher):
    publisher.publish_snapshot({'error': 'broker down', 'panel': None, 'circuits': []})
    assert publisher.client.published == []


def test_full_snapshot_topic_round_trips(publisher):
    publisher.publish_snapshot(SNAP)
    assert json.loads(topics(publisher)['span/snapshot'])['panel']['grid_power_w'] == -450.5


def test_safe_id():
    assert mqtt_publisher._safe_id('__feedthrough__') == 'feedthrough'
    assert mqtt_publisher._safe_id('unmapped_tab_5') == 'unmapped_tab_5'
    assert mqtt_publisher._safe_id('a b/c') == 'a_b_c'
    assert mqtt_publisher._safe_id('///') == 'unknown'


@pytest.fixture
def init(monkeypatch, tmp_path):
    """_try_init with no real file, recording the config a publisher gets."""
    built = []
    creds = tmp_path / 'mqtt-credentials.json'
    monkeypatch.setattr(mqtt_publisher, 'MQTT_CREDS_PATH', creds)
    monkeypatch.setattr(mqtt_publisher, '_Publisher', lambda cfg: built.append(cfg) or cfg)
    monkeypatch.setattr(mqtt_publisher, '_init_attempted', False)
    monkeypatch.delenv('MQTT_URL', raising=False)
    monkeypatch.delenv('MQTT_USERNAME', raising=False)
    monkeypatch.delenv('MQTT_PASSWORD', raising=False)
    monkeypatch.delenv('MQTT_TOPIC_PREFIX', raising=False)
    return SimpleNamespace(creds=creds, built=built)


def test_publishing_is_off_without_a_broker(init):
    assert mqtt_publisher._try_init() is None
    assert init.built == []


def test_broken_credentials_file_turns_it_off(init):
    init.creds.write_text('{not json')
    assert mqtt_publisher._try_init() is None


def test_url_wins_but_a_login_it_lacks_stays(init, monkeypatch):
    init.creds.write_text(json.dumps({'host': 'file.invalid', 'port': 1, 'username': 'filed',
                                      'password': 'filed-pw', 'topic_prefix': 'filed'}))
    monkeypatch.setenv('MQTT_URL', 'mqtt://url.invalid:1884')
    assert mqtt_publisher._try_init() == {'host': 'url.invalid', 'port': 1884, 'username': 'filed',
                                          'password': 'filed-pw', 'topic_prefix': 'filed'}


def test_a_publish_failure_never_reaches_the_poller(monkeypatch):
    class Broken:
        def publish_snapshot(self, snap):
            raise RuntimeError('broker gone')

    monkeypatch.setattr(mqtt_publisher, '_publisher', Broken())
    mqtt_publisher.publish_snapshot(SNAP)
