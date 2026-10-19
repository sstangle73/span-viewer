"""app/config.py: precedence, parsing and circuits.yaml."""

import pytest

from app import config


@pytest.fixture(autouse=True)
def clean(monkeypatch, tmp_path):
    for k in ('DATABASE_URL', 'SPAN_HOST', 'MQTT_URL', 'MQTT_USERNAME', 'MQTT_PASSWORD',
              'MQTT_TOPIC_PREFIX', 'NTFY_URL', 'NTFY_TOPIC', 'APP_URL', 'VM_URL', 'TZ',
              'RATE_IMPORT_PER_KWH', 'RATE_EXPORT_CREDIT_PER_KWH', 'FORWARDED_ALLOW_IPS',
              'REDIRECTS'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('SPAN_VIEWER_CONFIG', str(tmp_path / 'absent.toml'))
    config.reload()
    yield
    config.reload()


def _use(monkeypatch, path):
    monkeypatch.setenv('SPAN_VIEWER_CONFIG', str(path))
    config.reload()


def test_env_wins_over_file(monkeypatch, tmp_path):
    f = tmp_path / 'c.toml'
    f.write_text('span_host = "10.0.0.5"\nTZ = "Europe/Paris"\n')
    _use(monkeypatch, f)
    assert config.span_host() == '10.0.0.5'
    assert config.tz() == 'Europe/Paris'
    monkeypatch.setenv('SPAN_HOST', 'http://192.168.1.9/')
    assert config.span_host() == '192.168.1.9'
    assert config.span_base_url() == 'http://192.168.1.9'


def test_env_file_format(monkeypatch, tmp_path):
    f = tmp_path / 'span-viewer.env'
    f.write_text('# comment\nNTFY_URL="http://ntfy.lan"\nNTFY_TOPIC=abc\n\nAPP_URL=https://x.lan/\n')
    _use(monkeypatch, f)
    assert config.ntfy() == ('http://ntfy.lan', 'abc')
    assert config.app_url() == 'https://x.lan'


def test_no_hosts_by_default():
    assert config.span_host() is None
    assert config.span_base_url() is None
    assert config.mqtt_url() is None
    assert config.ntfy() is None
    assert config.app_url() is None
    assert config.vm_url() is None
    assert config.flat_rate() is None
    assert config.tz() == 'UTC'
    assert config.forwarded_allow_ips() == '127.0.0.1'


def test_bad_tz_falls_back(monkeypatch):
    monkeypatch.setenv('TZ', ':/etc/localtime')
    config.reload()
    assert config.tz() == 'UTC'


def test_mqtt_url(monkeypatch):
    monkeypatch.setenv('MQTT_URL', 'mqtt://u:p@broker.lan:1884/home/span')
    assert config.mqtt_url() == {'host': 'broker.lan', 'port': 1884, 'username': 'u',
                                 'password': 'p', 'topic_prefix': 'home/span'}
    monkeypatch.setenv('MQTT_URL', 'broker.lan')
    monkeypatch.setenv('MQTT_USERNAME', 'v')
    assert config.mqtt_url() == {'host': 'broker.lan', 'port': 1883, 'username': 'v',
                                 'password': None, 'topic_prefix': None}


def test_flat_rate(monkeypatch):
    monkeypatch.setenv('RATE_IMPORT_PER_KWH', '0.31')
    assert config.flat_rate() == {'import_per_kwh': 0.31, 'export_credit_per_kwh': 0.31}
    monkeypatch.setenv('RATE_EXPORT_CREDIT_PER_KWH', '0.08')
    assert config.flat_rate()['export_credit_per_kwh'] == 0.08


def test_database_url(monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://a:b@h/span')
    assert config.database_url() == 'postgresql://a:b@h/span'


def test_circuits_example(monkeypatch):
    monkeypatch.setenv('CIRCUITS_FILE', str(config.REPO_DIR / 'circuits.example.yaml'))
    config.reload()
    c = config.circuits()
    assert [a['key'] for a in c['appliances']] == ['dryer', 'oven', 'water_heater', 'fridge']
    assert len({a['glyph'] for a in c['appliances']}) == len(c['appliances'])
    assert config.duty_circuit('furnace')['on_kw'] == pytest.approx(0.066)
    assert config.duty_circuit('dehumidifier')['on_kw'] == 0.2
    assert c['ev']['threshold_kw'] == 1.5


def test_circuits_missing(monkeypatch, tmp_path):
    monkeypatch.setenv('CIRCUITS_FILE', str(tmp_path / 'none.yaml'))
    config.reload()
    c = config.circuits()
    assert c['appliances'] == [] and c['duty_cycle'] == []
    assert c['ev']['name_match'] == ['%charge%', '%EV%']


def test_redirects(monkeypatch, tmp_path):
    assert config.redirects() == {}
    monkeypatch.setenv('REDIRECTS', '/weather=https://h.lan/insights/solar/, /bills/=https://h.lan/b,bad=x')
    assert config.redirects() == {'/weather': 'https://h.lan/insights/solar/', '/bills': 'https://h.lan/b'}
    monkeypatch.delenv('REDIRECTS')
    f = tmp_path / 'c.toml'
    f.write_text('TZ = "UTC"\n[REDIRECTS]\n"/boiler" = "https://h.lan/insights/boiler/"\n')
    _use(monkeypatch, f)
    assert config.redirects() == {'/boiler': 'https://h.lan/insights/boiler/'}
