"""mqtt_publisher.py — push the live snapshot onto MQTT for outside consumers.

The live_store poller already hits the SPAN panel every 3 s and caches the
snapshot for /api/live. This module piggybacks on that same cycle: each
tick, we publish a handful of retained MQTT topics so that anything
subscribed (Home Assistant, mqtt2prometheus → VictoriaMetrics, custom
notebooks) sees fresh values without polling our HTTP API.

Why retained: late subscribers should get the last-known value
immediately, not have to wait up to 3 s for the next tick. Power-on a
Home Assistant box mid-day and it should know the house load is 1.4 kW
right away.

Why QoS 0: the firehose is high-cadence and lossy-is-fine — if we drop a
tick, the next one is 3 s away. QoS 1/2 would add ack round-trips and
broker disk writes that don't earn their keep at this cadence.

Connection: paho-mqtt's reconnect loop runs in its own thread. We never
block the FastAPI worker on publish — `paho.publish()` enqueues onto a
ring buffer that the network thread drains.

Configuration: config MQTT_URL (mqtt://[user:pass@]host[:port][/topic_prefix],
with MQTT_USERNAME / MQTT_PASSWORD for the login), and/or
/etc/span-viewer/mqtt-credentials.json (chmod 600; MQTT_URL's fields win), shape:
  {
    "host":     "<broker-host>",
    "port":     1883,
    "username": "spanviewer",
    "password": "...",
    "topic_prefix": "span"
  }
With neither, or a malformed file, MQTT publishing is silently disabled
(logged once at startup) — the app still serves HTTP fine. There is no
default broker.

Home Assistant auto-discovery: on every successful connect we re-publish
the discovery configs under `<MQTT_HA_PREFIX>/sensor/spanviewer_*/config`
(prefix `homeassistant` by default).
HA picks them up and creates entities with no manual YAML.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from . import config

try:
    import paho.mqtt.client as mqtt
    _PAHO_AVAILABLE = True
except ImportError:                                  # pragma: no cover
    _PAHO_AVAILABLE = False

log = logging.getLogger('mqtt_publisher')

MQTT_CREDS_PATH = Path(os.environ.get(
    'SPAN_MQTT_CREDS', '/etc/span-viewer/mqtt-credentials.json'))

# Slug for Home Assistant entity unique_ids / object_ids. Must match
# `[a-zA-Z0-9_-]+`. Bumping this string forces HA to re-discover
# everything (useful if topic layout changes).
_HA_NODE_ID = 'spanviewer'
_HA_DEVICE = {
    'identifiers':  ['spanviewer-panel'],
    'name':         'SPAN smart panel (via span-viewer)',
    'manufacturer': 'SPAN.IO',
    'model':        'Smart Panel',
    'sw_version':   'span-viewer',
}

# Per-panel-field metadata for the panel-level discovery topics. Order
# doesn't matter — this just keeps the publish call sites small.
# (key in panel dict, friendly name, device_class, state_class, unit)
_PANEL_FIELDS: list[tuple[str, str, str | None, str | None, str | None]] = [
    ('grid_power_w',         'Grid power',         'power', 'measurement', 'W'),
    ('solar_power_w',        'Solar power',        'power', 'measurement', 'W'),
    ('load_power_w',         'House load',         'power', 'measurement', 'W'),
    ('feedthrough_power_w',  'Sub-panel power',    'power', 'measurement', 'W'),
    ('solar_share_pct',      'Solar share',        None,    'measurement', '%'),
    ('phase_a_w',            'Phase A power',      'power', 'measurement', 'W'),
    ('phase_b_w',            'Phase B power',      'power', 'measurement', 'W'),
    ('phase_imbalance_pct',  'Phase imbalance',    None,    'measurement', '%'),
]

# Sanitize an arbitrary id (SPAN circuit ids like "unmapped_tab_5",
# "__feedthrough__", UUID-ish strings) into something topic-safe and
# HA-unique-id-safe.
_SAFE_ID_RE = re.compile(r'[^a-zA-Z0-9_-]+')


def _ha_prefix() -> str:
    return (config.get('MQTT_HA_PREFIX', 'homeassistant') or 'homeassistant').strip('/')


def _safe_id(raw: str) -> str:
    s = _SAFE_ID_RE.sub('_', raw).strip('_')
    return s or 'unknown'


class _Publisher:
    """Holds the paho client, connection state, and known-circuit set.

    Single instance, owned by the module — instantiated lazily so the
    import of this module is side-effect-free when MQTT is disabled."""

    def __init__(self, cfg: dict) -> None:
        self.host = cfg['host']
        self.port = int(cfg.get('port', 1883))
        self.prefix = cfg.get('topic_prefix', 'span').rstrip('/')
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.get('client_id', 'span-viewer'),
            clean_session=True,
        )
        if cfg.get('username'):
            self.client.username_pw_set(cfg['username'], cfg.get('password'))
        # Last-Will: brokers + subscribers know we dropped offline.
        self.client.will_set(
            f'{self.prefix}/status', payload='offline',
            qos=0, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

        # Circuits we've already published discovery for — re-emit on
        # reconnect, skip on subsequent ticks.
        self._known_circuit_ids: set[str] = set()
        self._lock = threading.Lock()

        self.client.connect_async(self.host, self.port, keepalive=30)
        # loop_start spins up the network thread; publish() is now
        # non-blocking from the caller's perspective.
        self.client.loop_start()

    # ---- callbacks ---------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info(f'MQTT connected to {self.host}:{self.port}')
            client.publish(f'{self.prefix}/status', payload='online',
                           qos=0, retain=True)
            # Re-publish panel-level HA discovery on every (re)connect —
            # broker may have lost retained messages, HA may have restarted.
            self._publish_panel_discovery()
            with self._lock:
                # Force re-discovery for circuits on next snapshot
                self._known_circuit_ids.clear()
        else:
            log.warning(f'MQTT connect failed: rc={reason_code}')

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        log.warning(f'MQTT disconnected: rc={reason_code} (will auto-reconnect)')

    # ---- discovery ---------------------------------------------------------

    def _publish_panel_discovery(self) -> None:
        """Emit retained HA discovery configs for the panel-level fields.
        Home Assistant auto-creates an entity per topic with no YAML."""
        for key, name, dev_class, state_class, unit in _PANEL_FIELDS:
            cfg: dict[str, Any] = {
                'name':        name,
                'unique_id':   f'{_HA_NODE_ID}_{key}',
                'object_id':   f'{_HA_NODE_ID}_{key}',
                'state_topic': f'{self.prefix}/panel/{key}',
                'availability_topic': f'{self.prefix}/status',
                'payload_available':     'online',
                'payload_not_available': 'offline',
                'device':      _HA_DEVICE,
            }
            if dev_class:
                cfg['device_class'] = dev_class
            if state_class:
                cfg['state_class'] = state_class
            if unit:
                cfg['unit_of_measurement'] = unit
            topic = f'{_ha_prefix()}/sensor/{_HA_NODE_ID}_{key}/config'
            self.client.publish(topic, payload=json.dumps(cfg),
                                qos=0, retain=True)

    def _publish_circuit_discovery(self, circuit_id: str, name: str) -> None:
        cfg = {
            'name':        f'Circuit · {name}',
            'unique_id':   f'{_HA_NODE_ID}_circuit_{circuit_id}',
            'object_id':   f'{_HA_NODE_ID}_circuit_{circuit_id}',
            'state_topic': f'{self.prefix}/circuit/{circuit_id}/power_w',
            'availability_topic':     f'{self.prefix}/status',
            'payload_available':      'online',
            'payload_not_available':  'offline',
            'device_class':           'power',
            'state_class':            'measurement',
            'unit_of_measurement':    'W',
            'device':                 _HA_DEVICE,
        }
        topic = f'{_ha_prefix()}/sensor/{_HA_NODE_ID}_circuit_{circuit_id}/config'
        self.client.publish(topic, payload=json.dumps(cfg),
                            qos=0, retain=True)

    # ---- publish ----------------------------------------------------------

    def publish_snapshot(self, snap: dict) -> None:
        """Called from live_store after each successful poll. Never raises.

        Layout:
          {prefix}/panel/<field>              numeric (retained)
          {prefix}/panel/main_relay           CLOSED/OPEN (retained)
          {prefix}/panel/grid_state           string (retained)
          {prefix}/panel/grid_up              1 / 0 from grid_state (retained)
          {prefix}/circuit/<safe_id>/power_w  W (retained)
          {prefix}/circuit/<safe_id>/name     human name (retained, on discovery)
          {prefix}/snapshot                   full JSON snapshot (retained)
          {prefix}/status                     online/offline (retained, LWT)
        """
        if snap.get('error'):
            # Don't push stale values on error — let retained-last-good win.
            return
        panel = snap.get('panel') or {}
        for key, *_meta in _PANEL_FIELDS:
            v = panel.get(key)
            if v is None:
                continue
            self.client.publish(
                f'{self.prefix}/panel/{key}',
                payload=f'{v:.3f}' if isinstance(v, float) else str(v),
                qos=0, retain=True)
        for k in ('main_relay', 'grid_state', 'run_config'):
            v = panel.get(k)
            if v is not None:
                self.client.publish(
                    f'{self.prefix}/panel/{k}',
                    payload=str(v), qos=0, retain=True)
        # grid_state as a number, so Prometheus (span_mqtt_exporter, which
        # ignores text topics) can alert on a grid loss. Replaced
        # house_power_exporter's power_panel_grid_up when its v1 SPAN source
        # was retired on 2026-10-08.
        gs = panel.get('grid_state') or ''
        if gs.endswith('GRID_UP') or gs.endswith('GRID_DOWN'):
            self.client.publish(
                f'{self.prefix}/panel/grid_up',
                payload='1' if gs.endswith('GRID_UP') else '0', qos=0, retain=True)

        circuits = snap.get('circuits') or []
        with self._lock:
            for c in circuits:
                cid_raw = str(c.get('id') or '')
                if not cid_raw:
                    continue
                cid = _safe_id(cid_raw)
                p = c.get('power_w')
                if p is not None:
                    self.client.publish(
                        f'{self.prefix}/circuit/{cid}/power_w',
                        payload=f'{p:.3f}', qos=0, retain=True)
                # First time we've seen this circuit — push name + HA discovery.
                if cid not in self._known_circuit_ids:
                    self._known_circuit_ids.add(cid)
                    name = c.get('name') or cid
                    self.client.publish(
                        f'{self.prefix}/circuit/{cid}/name',
                        payload=name, qos=0, retain=True)
                    self._publish_circuit_discovery(cid, name)

        # Full snapshot for consumers that want the kitchen sink in one topic.
        try:
            payload = json.dumps(snap, default=str)
        except (TypeError, ValueError):
            payload = '{}'
        self.client.publish(
            f'{self.prefix}/snapshot', payload=payload,
            qos=0, retain=True)

    def shutdown(self) -> None:
        try:
            self.client.publish(f'{self.prefix}/status', payload='offline',
                                qos=0, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception as e:                       # noqa: BLE001
            log.warning(f'MQTT shutdown error (non-fatal): {e}')


# ---- Module-level singleton ------------------------------------------------

_publisher: _Publisher | None = None
_init_attempted = False


def _try_init() -> _Publisher | None:
    """Construct the publisher from credentials. Returns None and logs
    once if creds are missing / paho isn't installed — caller treats
    None as "publishing disabled, just skip"."""
    global _init_attempted
    if not _PAHO_AVAILABLE:
        if not _init_attempted:
            log.info('paho-mqtt not installed — MQTT publishing disabled')
            _init_attempted = True
        return None
    url_cfg = config.mqtt_url()
    cfg: dict = {}
    if MQTT_CREDS_PATH.exists():
        try:
            cfg = json.loads(MQTT_CREDS_PATH.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f'failed to read {MQTT_CREDS_PATH}: {e}')
            _init_attempted = True
            return None
    elif url_cfg is None:
        if not _init_attempted:
            log.info(f'no MQTT_URL and no MQTT credentials at {MQTT_CREDS_PATH} — '
                     f'MQTT publishing disabled')
            _init_attempted = True
        return None
    if url_cfg:
        # MQTT_URL's host, port and prefix win; a login it lacks keeps the file's.
        cfg.update({k: v for k, v in url_cfg.items() if v is not None})
    if not cfg.get('host'):
        if not _init_attempted:
            log.info('MQTT has no broker host — MQTT publishing disabled')
            _init_attempted = True
        return None
    try:
        return _Publisher(cfg)
    except Exception as e:                           # noqa: BLE001
        log.warning(f'MQTT publisher init failed: {e}')
        _init_attempted = True
        return None


def publish_snapshot(snap: dict) -> None:
    """Fire-and-forget publish from the live_store tick. Lazy-init on
    first call so the import order doesn't depend on creds being present
    at module load."""
    global _publisher
    if _publisher is None:
        _publisher = _try_init()
        if _publisher is None:
            return
    try:
        _publisher.publish_snapshot(snap)
    except Exception as e:                           # noqa: BLE001
        # Never let a publish failure stop the poll loop.
        log.warning(f'MQTT publish failed: {e}')


def shutdown() -> None:
    global _publisher
    if _publisher is not None:
        _publisher.shutdown()
        _publisher = None
