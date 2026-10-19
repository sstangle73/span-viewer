"""span_ebus.py — the panel's own MQTT broker (SPAN API v2, Homie 5 / eBus).

Replacement for app/span_local.py, whose v1 REST endpoints SPAN removes on
2026-12-31. Instead of polling, one long-lived subscription to `ebus/5/#` keeps
the latest value of every property in memory; `snapshot()` builds the SAME
shaped dict span_local._shape() produces, so live_store, /api/live, panel_live,
space_live and the MQTT republisher don't change.

Credentials: /etc/span-viewer/span-api-credentials.json + span-ca.pem, written
by scripts/register-span-api-v2.py.

What the recon established (CLAUDE.md, "Recon results"), and what this relies on:
  - The panel samples ~1 Hz. From r202639 it stops republishing unchanged
    values, so every property is forward-filled: the retained value delivered
    on subscribe is the starting point, and silence means "unchanged".
  - Devices are discovered from $description `type`, never hardcoded IDs:
    lugs by `info/direction`, PV-feeding circuits by `connection/feeds-device-id`
    pointing at a `pv` device (from r202639 there can be several, and their IDs
    change).
  - Circuit IDs are the v1 circuit IDs, so space_live history continues.

Field mapping to the v1-shaped snapshot (signs verified 2026-10-06/07):
  grid_power_w         upstream lugs meter/active-power   (+ importing) — same
                       sign as v1 instantGridPowerW. NOT power-flows/grid,
                       which is derived and inverted.
  feedthrough_power_w  downstream lugs meter/active-power (+ sub-panel consuming)
  circuits[].power_w   −(circuit meter/active-power): eBus reports consumption
                       negative, the wire format is + = consuming (as v1)
  solar_power_w        the PV-feeding circuits' backfeed. Equal to
                       −power-flows/pv to the Wh, and to v1's branch-sum estimate
                       (the same breaker), so the numbers don't move. Those
                       circuits are left out of circuits[] and the load sum,
                       exactly as v1's /api/v1/circuits left them out.
  phase_a_w/phase_b_w  each circuit's |power| split evenly over its spaces;
                       spaces 1-16 = A, 17-32 = B (v1 summed its branches the
                       same way)
  main_relay           panel status/relay
  grid_state           DSM_GRID_UP / DSM_GRID_DOWN (v1's strings) from the bus
                       voltage (meter/voltage-a, -b). v1's dsmGridState has no
                       eBus equivalent without a
                       battery (the MID only exists with one); with no battery,
                       bus voltage is the grid's.
  run_config           None: no eBus equivalent.

TLS: verified against the panel CA by IP (the cert's SANs carry the panel IP);
never connect by the reported span-<serial>.local name: a host without mDNS
may resolve it through its DNS search domain to somewhere else entirely. VERIFY_X509_STRICT is cleared
because SPAN's certs have no Authority Key Identifier (Python 3.13 rejects them).

Read-only: this client never publishes, so it can't reach a /set topic.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import threading
import time
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt

from . import config

EBUS_CREDS = Path(os.environ.get(
    'SPAN_EBUS_CREDS', '/etc/span-viewer/span-api-credentials.json'))

# After a disconnect, or this long without a single message, the snapshot is an
# error rather than stale numbers: a stale snapshot would be written into
# space_live as if it were real. The upstream lugs' power changes almost every
# second, so 10 s of total silence means the broker or the panel is gone.
STALE_AFTER_S = 10.0

# MQTT keepalive. This client never publishes, so paho pings the broker every
# KEEPALIVE_S and drops the connection when a ping goes unanswered, which is the
# only way to notice a connection that went silent without closing. On
# 2026-10-08 16:48 UTC the panel did exactly that: v1 lost one poll, but at a
# 30-s keepalive the subscription took 90 s to notice and reconnect.
KEEPALIVE_S = 5

# A bus below this is not on grid (nominal 120 V per leg).
_GRID_UP_MIN_V = 90.0

# PV circuits under this (W) count as idle: the inverter's night-time draw
# shows as a few watts of consumption, never backfeed, but keep v1's threshold.
_SOLAR_MIN_W = 5.0


log = logging.getLogger('span_ebus')


def _f(v) -> float | None:
    try:
        return float(v) if v not in (None, '') else None
    except (TypeError, ValueError):
        return None


class EbusState:
    """One MQTT connection, a background network thread, and the latest value
    of every property. snapshot() is safe to call from any thread."""

    def __init__(self, cfg: dict) -> None:
        # SPAN_HOST, when set, wins over the address registration stored.
        host = config.span_host() or cfg['broker_host']
        if host.endswith('.local'):
            raise ValueError(f'broker_host {host!r} is a .local name; re-run '
                             'register-span-api-v2.py (it stores the panel IP)')
        self.host = host
        self.port = int(cfg.get('broker_mqtts_port') or 8883)
        self.serial = cfg.get('serial_number') or cfg['broker_username']

        self._lock = threading.Lock()
        self._props: dict[tuple[str, str], str] = {}   # (device, 'cap/prop') -> payload
        self._desc: dict[str, dict] = {}               # device -> parsed $description
        self._state: dict[str, str] = {}               # device -> $state
        self._connected = False
        self._connected_at: float | None = None
        self._last_msg_at: float | None = None
        self._last_error: str | None = 'not connected yet'
        self.messages = 0

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f'span-viewer-{socket.gethostname()}-{os.getpid()}')
        self._client.username_pw_set(cfg['broker_username'], cfg['broker_password'])
        ctx = ssl.create_default_context(cafile=cfg['ca_path'])
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self._client.tls_set_context(ctx)
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # connect_async + loop_start: the network thread owns the connection
        # and its reconnects, so a panel that's down at startup doesn't raise.
        self._client.connect_async(self.host, self.port, keepalive=KEEPALIVE_S)
        self._client.loop_start()
        log.info(f'span_ebus connecting to {self.host}:{self.port} as {self.serial}')

    def stop(self) -> None:
        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()

    # ---- paho callbacks (network thread) -------------------------------------

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None):
        if reason_code.is_failure:
            with self._lock:
                self._connected = False
                self._last_error = f'broker refused the connection: {reason_code}'
            log.warning(f'span_ebus: {self._last_error}')
            return
        # Retained messages re-deliver the whole tree on every (re)subscribe,
        # so state rebuilt from scratch is complete within a second or two.
        client.subscribe('ebus/5/#', qos=0)
        with self._lock:
            self._connected = True
            self._connected_at = time.time()
            self._last_error = None
        log.info('span_ebus connected, subscribed to ebus/5/#')

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _props=None):
        with self._lock:
            self._connected = False
            self._last_error = f'disconnected from the broker: {reason_code}'
        log.warning(f'span_ebus: {self._last_error} (paho reconnects by itself)')

    def _on_message(self, _client, _userdata, msg):
        parts = msg.topic.split('/', 3)
        if len(parts) < 4 or parts[0] != 'ebus':
            return
        device, rest = parts[2], parts[3]
        try:
            payload = msg.payload.decode('utf-8')
        except UnicodeDecodeError:
            return
        with self._lock:
            self.messages += 1
            self._last_msg_at = time.time()
            if rest == '$description':
                try:
                    self._desc[device] = json.loads(payload)
                except json.JSONDecodeError:
                    pass
            elif rest == '$state':
                self._state[device] = payload
            elif not rest.startswith('$') and not rest.endswith('/set'):
                self._props[(device, rest)] = payload

    # ---- reading ---------------------------------------------------------

    def health(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            return {
                'connected':       self._connected,
                'panel_state':     self._state.get(self.serial),
                'devices':         len(self._desc),
                'messages':        self.messages,
                'last_msg_age_s':  None if self._last_msg_at is None else round(now - self._last_msg_at, 1),
                'connected_for_s': None if self._connected_at is None else round(now - self._connected_at, 1),
                'last_error':      self._last_error,
                'firmware':        self._props.get((self.serial, 'info/firmware-version')),
            }

    def snapshot(self) -> dict[str, Any]:
        """The v1-shaped snapshot (see span_local._shape). Never raises."""
        try:
            return self._snapshot()
        except Exception as e:  # noqa: BLE001 — a bad payload must not kill /live
            return _err(f'span_ebus snapshot failed: {e}')

    def _snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if not self._connected:
                return _err(self._last_error or 'not connected')
            if self._last_msg_at is None or now - self._last_msg_at > STALE_AFTER_S:
                return _err(f'no message from the panel for over {STALE_AFTER_S:.0f} s')
            if self._state.get(self.serial) != 'ready':
                return _err(f'panel $state is {self._state.get(self.serial)!r}, not ready')
            props = dict(self._props)
            desc = dict(self._desc)

        def prop(dev, key):
            return props.get((dev, key))

        def dtype(dev):
            return (desc.get(dev) or {}).get('type', '')

        lugs_up = lugs_dn = None
        circuits, pv_devices = [], set()
        for dev in desc:
            t = dtype(dev)
            if t == 'energy.ebus.device.lugs':
                direction = prop(dev, 'info/direction')
                if direction == 'UPSTREAM':
                    lugs_up = dev
                elif direction == 'DOWNSTREAM':
                    lugs_dn = dev
            elif t == 'energy.ebus.device.circuit':
                circuits.append(dev)
            elif t == 'energy.ebus.device.pv':
                pv_devices.add(dev)
        if lugs_up is None:
            return _err('no upstream lugs device seen yet')

        grid_w = _f(prop(lugs_up, 'meter/active-power'))
        feedthrough_w = _f(prop(lugs_dn, 'meter/active-power')) if lugs_dn else None

        solar_w = 0.0
        pv_circuits = 0
        phase_a_w = phase_b_w = 0.0
        circuits_out: list[dict] = []
        circuits_consuming_w = 0.0
        for dev in circuits:
            raw = _f(prop(dev, 'meter/active-power'))
            spaces = _spaces(prop(dev, 'info/spaces'))
            if raw is not None and spaces:
                share = abs(raw) / len(spaces)
                for s in spaces:
                    if 1 <= s <= 16:
                        phase_a_w += share
                    elif 17 <= s <= 32:
                        phase_b_w += share
            if prop(dev, 'connection/feeds-device-id') in pv_devices:
                # The breaker the inverter lands on: its backfeed is solar.
                pv_circuits += 1
                if raw is not None and raw > _SOLAR_MIN_W:
                    solar_w += raw
                continue
            p = -raw if raw is not None else None
            if p is not None:
                circuits_consuming_w += p
            circuits_out.append({
                'id':       dev,
                'name':     prop(dev, 'info/name') or '(unnamed)',
                'power_w':  p,
                'relay':    prop(dev, 'switch/relay'),
                'priority': prop(dev, 'load-shed/priority'),
                'tabs':     spaces or None,
            })

        load_w = circuits_consuming_w
        if feedthrough_w is not None:
            load_w += feedthrough_w
            circuits_out.append({
                'id':       '__feedthrough__',
                'name':     config.circuits()['feedthrough_name'],
                'power_w':  feedthrough_w,
                'relay':    None,
                'priority': None,
                'tabs':     None,
            })
        circuits_out.sort(
            key=lambda x: (x['power_w'] is None, -abs(x['power_w'] or 0.0)))

        solar_share_pct = None
        if load_w > 0:
            solar_share_pct = min(100.0, max(0.0, solar_w / load_w * 100.0))

        phase_imbalance_pct = None
        larger = max(phase_a_w, phase_b_w)
        if larger > 50:
            phase_imbalance_pct = abs(phase_a_w - phase_b_w) / larger * 100.0

        volts = [v for v in (_f(prop(self.serial, 'meter/voltage-a')),
                             _f(prop(self.serial, 'meter/voltage-b'))) if v is not None]
        grid_state = None
        if volts:
            # v1's exact strings, so the republished span/panel/grid_state
            # topic and the /live status tile read the same as before.
            grid_state = 'DSM_GRID_UP' if min(volts) >= _GRID_UP_MIN_V else 'DSM_GRID_DOWN'

        panel_out = {
            'grid_power_w':        grid_w,
            'feedthrough_power_w': feedthrough_w,
            'solar_power_w':       solar_w,
            'load_power_w':        load_w,
            'solar_share_pct':     solar_share_pct,
            'phase_a_w':           phase_a_w,
            'phase_b_w':           phase_b_w,
            'phase_imbalance_pct': phase_imbalance_pct,
            'main_relay':          prop(self.serial, 'status/relay'),
            'grid_state':          grid_state,
            'run_config':          None,
        }
        return {
            'error':    None,
            'panel':    panel_out,
            'circuits': circuits_out,
            'raw':      {'source': 'ebus', 'circuits_count': len(circuits),
                         'pv_circuits': pv_circuits,
                         'power_flows_pv_w': _f(prop(self.serial, 'power-flows/pv')),
                         'firmware': prop(self.serial, 'info/firmware-version')},
        }


def _spaces(v: str | None) -> list[int]:
    out = []
    for part in (v or '').split(','):
        try:
            out.append(int(part))
        except ValueError:
            pass
    return out


def _err(msg: str) -> dict[str, Any]:
    return {'error': msg, 'panel': None, 'circuits': [], 'raw': {'source': 'ebus'}}


# ---- module singleton ---------------------------------------------------------

_state: EbusState | None = None
_state_lock = threading.Lock()


def available() -> bool:
    """True when the v2 credentials exist, i.e. the panel has registered us."""
    return EBUS_CREDS.exists()


def get_state() -> EbusState | None:
    """Start the subscription on first use. None (and logged) if the
    credentials are missing or malformed; the next call retries."""
    global _state
    with _state_lock:
        if _state is None:
            try:
                _state = EbusState(json.loads(EBUS_CREDS.read_text()))
                _state.start()
            except (OSError, KeyError, ValueError) as e:
                log.warning(f'span_ebus unavailable: {e}')
                _state = None
        return _state


def snapshot() -> dict[str, Any]:
    st = get_state()
    return st.snapshot() if st else _err(f'no usable credentials at {EBUS_CREDS}')


def shutdown() -> None:
    global _state
    with _state_lock:
        if _state is not None:
            _state.stop()
            _state = None
