"""live_store.py — background poller + DB writer for /live.

A single asyncio task spun up by the FastAPI lifespan takes a snapshot every
POLL_INTERVAL_S seconds, writes the panel-level row into `panel_live` and the
per-circuit rows into `space_live`, and stashes the *full* snapshot in a
module-level cache so /api/live can read it without touching the panel.

Decoupling means:
  - /api/live returns in <1 ms (cache read) rather than 50-200 ms (LAN
    round-trip to the panel).
  - Refreshing /live no longer drops the 5-min chart — the chart now
    rehydrates from `panel_live`.
  - Multiple browser sessions share one upstream poll budget.

SOURCE (env SPAN_LIVE_SOURCE) picks where snapshots come from. Both produce the
same shaped dict, so nothing downstream knows which one it got:

  v1      app.span_local: polls the panel's v1 REST API, which SPAN removes on
          2026-12-31.
  shadow  v1 stays authoritative (cache, DB, MQTT republish), and each tick
          also builds the app.span_ebus snapshot from the panel's own MQTT
          broker and compares the two. The running comparison is logged every
          10 minutes and served at /api/live/source. A diagnostic mode now;
          it was the default for the first day (2026-10-08).
  ebus    The default since 2026-10-08. app.span_ebus drives everything. If it
          errors (broker down, panel not ready) and v1 still answers, that tick
          falls back to v1 and says so in raw.source. Every outage is logged
          when it starts and ends, and counted under `ticks` at
          /api/live/source, so v1 covering for eBus never goes unnoticed. After v1's sunset on
          2026-12-31 the fallback can only return v1's error, and span_local
          goes.

Without the v2 credentials (/etc/span-viewer/span-api-credentials.json),
shadow and ebus behave as v1.

The cache is process-local; with single-worker uvicorn there's exactly
one poller. If we ever bump workers, each worker would spin up its own
poller — fine for cache, but the DB would see duplicate inserts at
slightly different timestamps. Stay on workers=1 until we change the
schema to dedupe.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import statistics
import time
from typing import Any

from . import config, span_ebus
from .db import pool
from .mqtt_publisher import publish_snapshot as mqtt_publish
from .span_local import snapshot as span_local_snapshot

log = logging.getLogger('live_store')

POLL_INTERVAL_S = 3.0
STARTUP_WAIT_S = 10.0

# `off`: no poller at all (a demo on sample data, with no panel to poll).
SOURCE = (config.get('SPAN_LIVE_SOURCE', 'ebus') or 'ebus').strip().lower()
if SOURCE not in ('v1', 'shadow', 'ebus', 'off'):
    log.warning(f'unknown SPAN_LIVE_SOURCE {SOURCE!r}; using v1')
    SOURCE = 'v1'

# Module-level cache of the latest full snapshot (the same shape /api/live
# used to return synchronously). Fetched by /api/live without hitting the
# panel.
_latest_snapshot: dict | None = None
_latest_snapshot_at: float | None = None     # time.time() when stored


def latest_snapshot() -> dict | None:
    """Return the last full snapshot the poller has stashed, or None if
    the poller hasn't run yet."""
    return _latest_snapshot


def latest_snapshot_age_s() -> float | None:
    """Seconds since the cache was last refreshed. None if never."""
    if _latest_snapshot_at is None:
        return None
    return time.time() - _latest_snapshot_at


def _ebus_on() -> bool:
    return SOURCE in ('shadow', 'ebus') and span_ebus.available()


def _tag(snap: dict, source: str, **extra) -> dict:
    snap['raw'] = {**(snap.get('raw') or {}), 'source': source, **extra}
    return snap


def source_snapshot() -> dict:
    """The authoritative snapshot for SOURCE. Never raises. Also the cold-start
    fall-through for /api/live before the poller's first tick."""
    if SOURCE == 'ebus' and span_ebus.available():
        snap = span_ebus.snapshot()
        if not snap.get('error'):
            return snap
        fallback = span_local_snapshot()
        if not fallback.get('error'):
            return _tag(fallback, 'v1-fallback', ebus_error=snap['error'])
        return snap
    return _tag(span_local_snapshot(), 'v1')


# ---- eBus outages (ebus mode) -------------------------------------------------

class _Outages:
    """Counts and logs the ticks eBus didn't serve. Without this, v1 covering
    for a failing subscription is invisible until v1 itself is gone on
    2026-12-31. One warning when an outage starts, one line when it ends."""

    def __init__(self) -> None:
        self.ticks = 0
        self.fallback_ticks = 0      # served by v1 instead
        self.error_ticks = 0         # neither source answered
        self.outages = 0
        self.started: float | None = None
        self.reason: str | None = None
        self.last: dict[str, Any] | None = None
        self._ticks_in = [0, 0]      # fallback, error during the current outage

    def observe(self, snap: dict) -> None:
        self.ticks += 1
        src = (snap.get('raw') or {}).get('source')
        if src == 'ebus' and not snap.get('error'):
            if self.started is not None:
                secs = time.time() - self.started
                self.last = {
                    'start': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.started)),
                    'seconds': round(secs, 1), 'reason': self.reason,
                    'fallback_ticks': self._ticks_in[0], 'error_ticks': self._ticks_in[1]}
                log.info(f'eBus back after {secs:.0f} s ({self._ticks_in[0]} ticks served '
                         f'by v1, {self._ticks_in[1]} with no data); cause: {self.reason}')
                self.started, self.reason, self._ticks_in = None, None, [0, 0]
            return
        fell_back = src == 'v1-fallback' and not snap.get('error')
        if fell_back:
            self.fallback_ticks += 1
            self._ticks_in[0] += 1
        else:
            self.error_ticks += 1
            self._ticks_in[1] += 1
        if self.started is None:
            self.started = time.time()
            self.outages += 1
            self.reason = (snap.get('raw') or {}).get('ebus_error') or snap.get('error')
            log.warning(f'eBus unavailable ({self.reason}); '
                        f'{"serving v1 until it recovers" if fell_back else "no data from either source"}')

    def report(self) -> dict[str, Any]:
        return {'ticks': self.ticks, 'fallback_ticks': self.fallback_ticks,
                'error_ticks': self.error_ticks, 'outages': self.outages,
                'current_outage_since': (time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.started))
                                         if self.started else None),
                'last_outage': self.last}


_outages = _Outages()


# ---- shadow comparison --------------------------------------------------------

class _Shadow:
    """Rolling comparison of the v1 and eBus snapshots taken in the same tick.
    The two sample up to a second apart, so small differences are expected on
    loads that fluctuate; what matters is that the medians are near zero and
    the circuit sets agree."""

    FIELDS = ('grid_power_w', 'feedthrough_power_w', 'solar_power_w',
              'load_power_w', 'phase_a_w', 'phase_b_w')
    WINDOW = 1200          # ticks, one hour at 3 s
    LOG_EVERY_S = 600.0

    def __init__(self) -> None:
        self.rows: collections.deque = collections.deque(maxlen=self.WINDOW)
        self.ticks = 0
        self.ebus_errors = 0
        self.v1_errors = 0
        self.last_ebus_error: str | None = None
        self.started = time.time()
        self._last_log = time.time()

    def observe(self, v1: dict, eb: dict) -> None:
        self.ticks += 1
        if eb.get('error'):
            self.ebus_errors += 1
            self.last_ebus_error = eb['error']
        if v1.get('error'):
            self.v1_errors += 1
        if v1.get('error') or eb.get('error'):
            return
        pa, pb = v1['panel'], eb['panel']
        row: dict[str, Any] = {}
        for f in self.FIELDS:
            if pa.get(f) is not None and pb.get(f) is not None:
                row[f] = abs(pa[f] - pb[f])
        ca = {c['id']: c for c in v1['circuits']}
        cb = {c['id']: c for c in eb['circuits']}
        row['only_v1'] = sorted(set(ca) - set(cb))
        row['only_ebus'] = sorted(set(cb) - set(ca))
        row['circuits'] = [abs(ca[i]['power_w'] - cb[i]['power_w'])
                           for i in set(ca) & set(cb)
                           if ca[i]['power_w'] is not None and cb[i]['power_w'] is not None]
        row['meta_mismatch'] = sorted(
            i for i in set(ca) & set(cb)
            if (ca[i]['name'], ca[i]['tabs'], ca[i]['relay'])
            != (cb[i]['name'], cb[i]['tabs'], cb[i]['relay']))
        self.rows.append(row)
        if time.time() - self._last_log >= self.LOG_EVERY_S:
            self._last_log = time.time()
            r = self.report()
            fields = ' '.join(f'{f.removesuffix("_power_w").removesuffix("_w")}={v["median"]:.0f}/{v["p95"]:.0f}'
                              for f, v in r['fields'].items())
            log.info(f'shadow v1 vs ebus over {r["compared_ticks"]} ticks, |Δ| W median/p95: {fields} '
                     f'circuits={r["circuits"]["median"]:.1f}/{r["circuits"]["p95"]:.1f}; '
                     f'id mismatch ticks {r["id_mismatch_ticks"]}, meta mismatch ticks '
                     f'{r["meta_mismatch_ticks"]}, ebus errors {self.ebus_errors}/{self.ticks}')

    def report(self) -> dict[str, Any]:
        rows = list(self.rows)

        def dist(vals):
            vals = sorted(vals)
            if not vals:
                return {'n': 0, 'median': 0.0, 'p95': 0.0, 'max': 0.0}
            return {'n': len(vals), 'median': round(statistics.median(vals), 2),
                    'p95': round(vals[int(len(vals) * 0.95)], 2), 'max': round(vals[-1], 2)}

        return {
            'since':               time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(self.started)),
            'ticks':               self.ticks,
            'compared_ticks':      len(rows),
            'ebus_errors':         self.ebus_errors,
            'v1_errors':           self.v1_errors,
            'last_ebus_error':     self.last_ebus_error,
            'fields':              {f: dist(r[f] for r in rows if f in r) for f in self.FIELDS},
            'circuits':            dist(d for r in rows for d in r['circuits']),
            'id_mismatch_ticks':   sum(1 for r in rows if r['only_v1'] or r['only_ebus']),
            'id_mismatch_example': next(({'only_v1': r['only_v1'], 'only_ebus': r['only_ebus']}
                                         for r in reversed(rows) if r['only_v1'] or r['only_ebus']), None),
            'meta_mismatch_ticks': sum(1 for r in rows if r['meta_mismatch']),
        }


_shadow = _Shadow()


def source_report() -> dict[str, Any]:
    """For /api/live/source: which source drives /live, the eBus connection's
    health, and (in shadow mode) the running comparison."""
    out: dict[str, Any] = {'mode': SOURCE, 'ebus_credentials': span_ebus.available()}
    if _ebus_on():
        st = span_ebus.get_state()
        out['ebus'] = st.health() if st else None
    if SOURCE == 'shadow':
        out['shadow'] = _shadow.report()
    if SOURCE == 'ebus' and span_ebus.available():
        out['ticks'] = _outages.report()
    return out


# ---- DB writes --------------------------------------------------------------

def _insert_live_row(panel: dict) -> None:
    """Append one panel-level snapshot to the panel_live hypertable. NOW()
    is used server-side so all live rows share the postgres clock (and
    the retention policy's age math)."""
    sql = """
        INSERT INTO panel_live (
            ts, grid_power_w, feedthrough_power_w, solar_power_w,
            load_power_w, phase_a_w, phase_b_w
        ) VALUES (NOW(), %s, %s, %s, %s, %s, %s)
        ON CONFLICT (ts) DO NOTHING;
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute(sql, (
            panel.get('grid_power_w'),
            panel.get('feedthrough_power_w'),
            panel.get('solar_power_w'),
            panel.get('load_power_w'),
            panel.get('phase_a_w'),
            panel.get('phase_b_w'),
        ))


def _insert_space_live_rows(circuits: list[dict]) -> None:
    """Append per-circuit snapshots to the space_live hypertable.

    Substrate for NILM and edge-detection analytics (see
    `app.queries.discovered_loads`). One row per circuit per tick at 3-s
    cadence; the synthetic '__feedthrough__' row is included so the
    sub-panel's draw can be inspected like any other circuit.
    """
    if not circuits:
        return
    rows = []
    for c in circuits:
        cid = c.get('id')
        if not cid:
            continue
        # power_w in the shaped snapshot is signed: + = consuming, - = backfeed.
        rows.append((cid, c.get('power_w')))
    if not rows:
        return
    sql = """
        INSERT INTO space_live (ts, space_id, power_w)
        VALUES (NOW(), %s, %s)
        ON CONFLICT (ts, space_id) DO NOTHING;
    """
    with pool().connection() as conn, conn.cursor() as cur:
        cur.executemany(sql, rows)


def _tick_sync() -> None:
    """One poll cycle. Runs in a thread off the event loop so the blocking
    `requests` call doesn't stall other endpoints."""
    global _latest_snapshot, _latest_snapshot_at
    snap = source_snapshot()
    if SOURCE == 'ebus' and span_ebus.available():
        _outages.observe(snap)
    if SOURCE == 'shadow' and _ebus_on():
        try:
            _shadow.observe(snap, span_ebus.snapshot())
        except Exception as e:  # noqa: BLE001 — the comparison must never cost a tick
            log.warning(f'shadow comparison failed: {e}')
    _latest_snapshot = snap
    _latest_snapshot_at = time.time()
    if not snap.get('error') and snap.get('panel'):
        try:
            _insert_live_row(snap['panel'])
        except Exception as e:
            # Don't let DB write failures stop the cache update — the
            # browser-facing /api/live still works without persistence.
            log.warning(f'panel_live insert failed: {e}')
        try:
            _insert_space_live_rows(snap.get('circuits') or [])
        except Exception as e:
            log.warning(f'space_live insert failed: {e}')
    # MQTT publish is fire-and-forget and silent-on-no-creds, so we can call
    # it unconditionally — it'll no-op if MQTT isn't configured. Errors are
    # swallowed inside the module so a broker outage doesn't stall the poll.
    mqtt_publish(snap)


async def poller_loop() -> None:
    """Forever-loop the snapshot → cache → DB write cycle. Cancellation from
    the FastAPI lifespan exits cleanly."""
    if SOURCE == 'off':
        log.info('live_store poller off (SPAN_LIVE_SOURCE=off)')
        return
    log.info(f'live_store poller starting (every {POLL_INTERVAL_S}s, source {SOURCE}, '
             f'eBus credentials {"present" if span_ebus.available() else "absent"})')
    if _ebus_on():
        # Subscribe now and give the retained tree up to STARTUP_WAIT_S to
        # arrive, so the first tick after every restart is served by eBus and
        # doesn't log as an outage (it did until 2026-10-08: the connection
        # takes ~0.5 s, the first tick ran at once).
        await asyncio.to_thread(span_ebus.get_state)
        deadline = time.time() + STARTUP_WAIT_S
        while time.time() < deadline and span_ebus.snapshot().get('error'):
            await asyncio.sleep(0.25)
    try:
        while True:
            t0 = time.time()
            try:
                await asyncio.to_thread(_tick_sync)
            except Exception as e:
                log.warning(f'live_store tick failed: {e}')
            # Sleep the *remainder* of the interval so jitter doesn't
            # accumulate when the panel is slow.
            elapsed = time.time() - t0
            await asyncio.sleep(max(0.0, POLL_INTERVAL_S - elapsed))
    except asyncio.CancelledError:
        log.info('live_store poller cancelled (lifespan shutdown)')
        raise
