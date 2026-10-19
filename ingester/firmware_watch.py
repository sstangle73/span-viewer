#!/usr/bin/env python3
"""firmware_watch.py — watch the SPAN panel's firmware version and push ntfy
when it changes.

We care because of one specific upcoming change. SPAN's r202633 release
restructures the eBus/MQTT surface from the flat single-device model
(r202603-r202627) to a parent/child device + capabilities model — a documented
breaking change — and it rolls out during September 2026. Everything we would
build against today's r202627 topics has to be rewritten when it lands, so the
right sequencing is "wait, then port once". This watcher is the trigger.

It also covers the duller case: any firmware bump can change behaviour under
app/span_local.py, and silently discovering that from a broken /live page is
worse than getting a push.

Two alerts, both deduped through a state file so timer fires are idempotent:

  firmware changed      any version transition, priority 3
  r202633 reached       the eBus schema migration is now unblocked, priority 4

Uses the v2 status endpoint, which is stable across the v1 sunset (2026-12-31)
and needs no auth beyond the bearer token we already hold.

Config (env or the config file, see app/config.py; defaults):

   SPAN_HOST                unset: the credentials file's base_url (SPAN_BASE_URL, a
                            full URL, still works and wins)
   SPAN_CREDENTIALS         /etc/span-viewer/span-local-credentials.json
   NTFY_URL, NTFY_TOPIC     no default: unset, a change is recorded and printed but
                            nothing is pushed. Treat the topic as a secret: anyone
                            who knows it can read and post. It's never printed.
   APP_URL                  no default: the push's click-through to /health
   FIRMWARE_STATE_PATH      /opt/span-viewer/data/firmware-state.json
   MIGRATION_RELEASE        r202633

Run from the span-viewer LXC:

   /opt/span-viewer/.venv/bin/python -m ingester.firmware_watch
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from pathlib import Path

import requests

from app import config

# Empty means SPAN_HOST, else the credentials file's base_url, the same address the app's /live uses.
SPAN_BASE_URL = (os.environ.get('SPAN_BASE_URL', '') or config.span_base_url() or '').rstrip('/')
SPAN_CREDENTIALS = os.environ.get(
    'SPAN_CREDENTIALS', '/etc/span-viewer/span-local-credentials.json')

NTFY = config.ntfy()          # (url, topic) or None
APP_URL = config.app_url()    # or None

STATE_PATH = Path(os.environ.get('FIRMWARE_STATE_PATH',
                                 '/opt/span-viewer/data/firmware-state.json'))
MIGRATION_RELEASE = os.environ.get('MIGRATION_RELEASE', 'r202633')


def _release_number(version: str) -> int:
    """'spanos3/r202627/06' -> 202627. Returns 0 when unparseable so an
    unexpected version string can never fake a migration-reached alert."""
    m = re.search(r'r(\d+)', version or '')
    return int(m.group(1)) if m else 0


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {'firmware_version': None, 'migration_alerted': False}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2) + '\n')


def _post_ntfy(title: str, message: str, tag: str, priority: int = 3) -> None:
    # HTTP headers don't carry UTF-8 reliably (the retired anomaly alerter learned
    # this the hard way) — keep the Title ASCII-safe.
    if NTFY is None:
        print('   ntfy not configured (NTFY_URL / NTFY_TOPIC) - not pushed')
        return
    headers = {
        'Title':    title,
        'Priority': str(priority),
        'Tags':     tag,
    }
    if APP_URL:
        headers['Click'] = f'{APP_URL}/health'
    url, topic = NTFY
    r = requests.post(
        f'{url}/{topic}',
        data=message.encode('utf-8'),
        headers=headers, timeout=10,
    )
    r.raise_for_status()


def _fetch_status() -> dict:
    try:
        creds = json.loads(Path(SPAN_CREDENTIALS).read_text())
        token = creds['access_token']
    except (OSError, json.JSONDecodeError, KeyError) as e:
        raise RuntimeError(f'cannot read {SPAN_CREDENTIALS}: {e}') from e
    base = SPAN_BASE_URL or str(creds.get('base_url', '')).rstrip('/')
    if not base:
        raise RuntimeError(f'no SPAN_BASE_URL and no base_url in {SPAN_CREDENTIALS}')

    r = requests.get(f'{base}/api/v2/status',
                     headers={'Authorization': f'Bearer {token}'}, timeout=10)
    r.raise_for_status()
    return r.json()


def main() -> int:
    started = datetime.datetime.now(datetime.timezone.utc)
    print(f'== firmware watch — {started.isoformat(timespec="seconds")} ==')
    # The topic is a secret: name the server only.
    print(f'   panel: {SPAN_BASE_URL or "the credentials file base_url"}   '
          f'ntfy: {NTFY[0] + " (topic set)" if NTFY else "off"}')

    try:
        status = _fetch_status()
    except Exception as e:
        print(f'error: {e}', file=sys.stderr)
        return 1

    current = status.get('firmwareVersion', '')
    release = _release_number(current)
    target = _release_number(MIGRATION_RELEASE)
    state = _load_state()
    previous = state.get('firmware_version')

    print(f'   current: {current} (r{release})   previous: {previous}')

    if previous is None:
        # First run: record the baseline without alerting on it.
        print('   first run — recording baseline, no alert')
    elif current != previous:
        print(f'   CHANGED {previous} -> {current}')
        _post_ntfy(
            'SPAN panel firmware changed',
            f'{previous} -> {current}\n\n'
            f'Check /live and the ingest health page for fallout.',
            'satellite,arrow_up', priority=3,
        )
    else:
        print('   unchanged')

    # Separate, one-shot alert: crossing into the release that restructures
    # the eBus schema is the signal to start the MQTT port, and it should fire
    # even if the version transition itself was already alerted above.
    if release >= target > 0 and not state.get('migration_alerted'):
        print(f'   reached {MIGRATION_RELEASE} — migration unblocked')
        _post_ntfy(
            'SPAN eBus schema migration unblocked',
            f'Panel is on {current}, at or past {MIGRATION_RELEASE}.\n\n'
            f'The MQTT surface is now the parent/child + capabilities model. '
            f'Time to port app/live_store.py off the v1 REST poll '
            f'(v1 sunset: 2026-12-31).',
            'construction,electric_plug', priority=4,
        )
        state['migration_alerted'] = True

    state['firmware_version'] = current
    state['checked_at'] = started.isoformat(timespec='seconds')
    _save_state(state)
    print(f'   state -> {STATE_PATH}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
