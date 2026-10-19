"""span_local.py — client for the SPAN panel's on-LAN HTTP API.

Used by /api/live for real-time per-circuit power. The cloud GraphQL pipeline
only gives 10-min buckets; this endpoint updates near-second on LAN.

Auth recipe (one-time): POST hopPassphrase to /api/v1/auth/register to mint a
bearer token. The token survives panel reboots. Credentials live at
/etc/span-viewer/span-local-credentials.json (chmod 600) in the shape:

    {"base_url": "http://<panel-ip>", "access_token": "<bearer>"}

SPAN_HOST (config), when set, overrides base_url's host.

See scripts/register-span-local.py for the registration helper.

The local API gives us per-circuit instantaneous power, grid main power, and
the feedthrough lugs — but no explicit solar/battery split. Solar/battery
flow attribution still has to come from the cloud GraphQL ingest. So this
module deliberately surfaces only what the panel itself reports.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import requests

from . import config

LOCAL_CREDS = Path(os.environ.get(
    'SPAN_LOCAL_CREDS', '/etc/span-viewer/span-local-credentials.json'))

# Per-request timeout — SPAN's local API responds in tens of ms on LAN.
# 2 s bounds the worst case if the panel is busy without blocking the
# FastAPI worker for noticeable time.
REQ_TIMEOUT_S = 2.0

log = logging.getLogger('span_local')


class SpanLocalError(Exception):
    """Raised when the local API is unreachable or unusable."""


class SpanLocalClient:
    """Thin wrapper over the panel's REST endpoints. One requests.Session
    per process keeps the TCP connection warm between polls."""

    def __init__(self) -> None:
        cfg = json.loads(LOCAL_CREDS.read_text())
        # SPAN_HOST, when set, wins over the file's address.
        self.base_url = config.span_base_url(cfg['base_url'])
        self._token = cfg['access_token']
        self._session = requests.Session()
        self._session.headers.update(
            {'Authorization': f'Bearer {self._token}'})

    def _get(self, path: str) -> dict[str, Any]:
        url = f'{self.base_url}{path}'
        try:
            r = self._session.get(url, timeout=REQ_TIMEOUT_S)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise SpanLocalError(f'GET {path} failed: {e}') from e
        except ValueError as e:
            raise SpanLocalError(f'GET {path} returned non-JSON: {e}') from e

    def panel(self) -> dict[str, Any]:
        return self._get('/api/v1/panel')

    def circuits(self) -> dict[str, Any]:
        return self._get('/api/v1/circuits')


# Module-level singleton so we share the TCP connection across requests.
# Reset to None if construction fails (e.g. creds missing); next call will
# retry — handy if the user provisions the file while the app is running.
_client: SpanLocalClient | None = None


def _get_client() -> SpanLocalClient:
    global _client
    if _client is None:
        _client = SpanLocalClient()
    return _client


def snapshot() -> dict[str, Any]:
    """Single JSON snapshot for /api/live. Never raises.

    On any local-API failure (creds missing, panel unreachable, malformed
    response) returns {'error': '...', 'panel': None, 'circuits': []} so the
    browser polling stays in HTTP 200 land and the page can show a
    "disconnected" badge instead of breaking the fetch loop.
    """
    global _client
    try:
        c = _get_client()
    except FileNotFoundError:
        return _err(f'credentials file not found at {LOCAL_CREDS}')
    except (KeyError, json.JSONDecodeError) as e:
        _client = None
        return _err(f'credentials file is malformed: {e}')

    try:
        panel = c.panel()
        circuits_raw = c.circuits()
    except SpanLocalError as e:
        return _err(str(e))

    return _shape(panel, circuits_raw)


def _err(msg: str) -> dict[str, Any]:
    log.warning(f'span_local snapshot failed: {msg}')
    return {'error': msg, 'panel': None, 'circuits': [], 'raw': None}


# Branches under this threshold (W) are treated as idle for the solar
# inference. Filters out inverter night-time tare oscillations and general
# measurement noise on unused breaker positions, while still catching a
# 100-W solar trickle at dawn.
_SOLAR_BRANCH_MIN_W = 5.0


def _as_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _shape(panel: dict, circuits_raw: dict) -> dict:
    """Reshape raw API responses into the stable wire format /live consumes.

    SIGN CONVENTIONS — verified live against a panel on 2026-05-12:

      panel.instantGridPowerW
          + importing from grid · − exporting to grid

      panel.feedthroughPowerW
          + flowing OUT of the main bus, INTO the sub-panel (sub-panel
            consuming — the common case for this install)
          − flowing FROM the sub-panel UP into the main bus (backfeed;
            rare here because the sub-panel has no generation)

      circuits[].instantPowerW  /  branches[].instantPowerW
          − consuming (panel delivering power to a load)
          + backfeeding (something pushing power INTO the main bus, e.g.
            solar disconnect, EV-V2H)

    We flip the per-circuit sign so the wire format uses "+ = consuming"
    everywhere — keeps the UI labels straightforward.

    Solar production is INFERRED from the branches array: the panel has
    no explicit "solar" output but the solar disconnect appears as two
    positive-flowing branches (240 V → 2 poles, ~half the power each).
    Sum branches with sustained positive flow to get solar W. Works
    cleanly for installs without a battery (a battery would also show
    positive during discharge and contaminate the sum).
    """
    grid_w        = _as_float(panel.get('instantGridPowerW'))
    feedthrough_w = _as_float(panel.get('feedthroughPowerW'))

    # Solar inference: sum branches whose net flow is INTO the bus
    # (positive), above a small threshold to ignore noise.
    # While iterating: also split the branches into the two columns of a
    # split-phase 200 A service to derive phase imbalance. SPAN tabs 1-16
    # are one column (phase A here), 17-32 the other (phase B). Loads on
    # 240 V breakers straddle both columns so their power gets evenly
    # attributed by SPAN already — we just sum the magnitudes per column.
    solar_w = 0.0
    phase_a_w = 0.0   # |net power| on tabs 1-16
    phase_b_w = 0.0   # |net power| on tabs 17-32
    branches = panel.get('branches') or []
    for b in branches:
        bp = _as_float(b.get('instantPowerW'))
        if bp is None:
            continue
        if bp > _SOLAR_BRANCH_MIN_W:
            solar_w += bp
        bid = b.get('id')
        if isinstance(bid, int):
            mag = abs(bp)
            if 1 <= bid <= 16:
                phase_a_w += mag
            elif 17 <= bid <= 32:
                phase_b_w += mag

    circs = circuits_raw.get('circuits') or {}
    circuits_out: list[dict] = []
    circuits_consuming_w = 0.0     # sum of named-circuit consumption (positive)
    for cid, c in circs.items():
        if not isinstance(c, dict):
            continue
        raw_w = _as_float(c.get('instantPowerW'))
        # Flip: consumption positive, backfeed negative.
        p = (-raw_w) if raw_w is not None else None
        if p is not None:
            circuits_consuming_w += p
        circuits_out.append({
            'id':       c.get('id', cid),
            'name':     c.get('name') or '(unnamed)',
            'power_w':  p,
            'relay':    c.get('relayState'),
            'priority': c.get('priority'),
            'tabs':     c.get('tabs'),
        })
    # Sort by |W| desc; None values land at the bottom.
    circuits_out.sort(
        key=lambda x: (x['power_w'] is None, -abs(x['power_w'] or 0.0)))

    # Total house load = named-circuit consumption + feedthrough
    # (when feedthrough > 0 the sub-panel is consuming; when < 0 it's
    # backfeeding, so the signed sum is still the right thing to add).
    load_w = circuits_consuming_w
    if feedthrough_w is not None:
        load_w += feedthrough_w

    # Inject the feedthrough/sub-panel as a synthetic "circuit" row so it
    # shows up in the per-circuit bars ranked alongside the real ones.
    # A sub-panel fed from the feedthrough lugs is, to the house, just
    # another load, so the UI ranks it like one.
    if feedthrough_w is not None:
        circuits_out.append({
            'id':       '__feedthrough__',
            # Match the cloud spaces-table name for space b7755… (the same
            # physical lug measurement, just exposed under a different
            # identifier in the cloud GraphQL surface). Using the same
            # display name everywhere — live, historical, NILM picker —
            # keeps the UI coherent: one name per appliance.
            'name':     config.circuits()['feedthrough_name'],
            'power_w':  feedthrough_w,
            'relay':    None,
            'priority': None,
            'tabs':     None,
        })
        circuits_out.sort(
            key=lambda x: (x['power_w'] is None, -abs(x['power_w'] or 0.0)))

    # Self-sufficiency = share of current demand covered by solar. Capped
    # at 100 (any surplus shows up as a negative grid_power_w, i.e. export).
    solar_share_pct = None
    if load_w is not None and load_w > 0:
        solar_share_pct = min(100.0, max(0.0, (solar_w / load_w) * 100.0))

    # Phase imbalance as a % of the larger phase. 0 % = perfectly balanced;
    # NEC-style guidance is ≤10 %, panel-mfr alarms typically fire ≥20 %.
    phase_imbalance_pct = None
    larger_phase = max(phase_a_w, phase_b_w)
    if larger_phase > 50:   # need some load to compute meaningfully
        phase_imbalance_pct = abs(phase_a_w - phase_b_w) / larger_phase * 100.0

    panel_out = {
        'grid_power_w':         grid_w,
        'feedthrough_power_w':  feedthrough_w,
        'solar_power_w':        solar_w,
        'load_power_w':         load_w,
        'solar_share_pct':      solar_share_pct,
        'phase_a_w':            phase_a_w,
        'phase_b_w':            phase_b_w,
        'phase_imbalance_pct':  phase_imbalance_pct,
        'main_relay':           panel.get('mainRelayState'),
        'grid_state':           panel.get('dsmGridState'),
        'run_config':           panel.get('currentRunConfig'),
    }

    return {
        'error':    None,
        'panel':    panel_out,
        'circuits': circuits_out,
        # Small raw echo for the debug expander — full panel dump but only
        # circuits-count to keep the response tight.
        'raw':      {'panel': panel, 'circuits_count': len(circs)},
    }
