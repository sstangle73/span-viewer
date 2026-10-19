#!/usr/bin/env python3
r"""register-span-api-v2.py — register this LXC as a SPAN API (v2) client and
write the eBus MQTT broker credentials to
/etc/span-viewer/span-api-credentials.json.

This is the successor to register-span-local.py. The v1 REST endpoints that
script authenticates against are deprecated and sunset 2026-12-31 (every v1
response carries `deprecation: true` / `sunset: 2026-12-31` headers). Real-time
panel state moves to the panel's own MQTT broker; v2 REST is auth + config only.

Ways to authenticate; the register endpoint requires one of them:

  (no flag, at a terminal) Prompts for the hopPassphrase without echoing it.
                           It's in the SPAN app -> Settings -> On-Prem Settings.

  --passphrase-stdin       Reads the hopPassphrase from the first line of
                           stdin, e.g. piped over SSH from a password manager,
                           so it never lands on a command line or in shell
                           history.

  --door-bypass            Press the door switch 3 times in rapid succession,
                           then run within ~15 min. See the warning below.

  --hop-passphrase PASS    Still accepted, but the value ends up in shell
                           history and in `ps`. Prefer either of the above.

A bearer token alone is NOT enough. The existing v1 accessToken authenticates
v2 GET endpoints, but POST /api/v2/auth/register rejects it with HTTP 422
("Must provide one-time-password, dashboard password (hopPassphrase or
dashboardPassword) or door-bypass") — verified on spanos3/r202633/04,
2026-09-13. The token is still used, when present, for the read-only
pre-flight checks.

Door-bypass warning: the proximity proof is SINGLE-USE and is consumed by the
first auth/register request that relies on it, even if that request then fails
for an unrelated reason (a client `name` that already exists is the classic
one). This script pre-flights the name against /api/v2/auth/clients before
spending the proof. Opening the panel door counts as the first press, so from
a closed door only 2 more are needed.

Run on the span-viewer host (needs root to write into /etc/):

    /opt/span-viewer/.venv/bin/python scripts/register-span-api-v2.py
    /opt/span-viewer/.venv/bin/python scripts/register-span-api-v2.py --door-bypass

Writes two files:

    /etc/span-viewer/span-api-credentials.json   broker host/port/user/password
    /etc/span-viewer/span-ca.pem                 CA cert for MQTTS verification

The hopPassphrase itself is NOT written to disk. It can mint a token that
opens circuit relays, so keep it in a password manager; this host keeps only
the scoped credentials the MQTT client needs.

Re-running with the same --name replaces that client entry on the panel.
"""

import argparse
import getpass
import json
import sys
import time
from pathlib import Path

import requests

# SPAN_HOST from the environment or /etc/span-viewer/span-viewer.env (app/config.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402

DEFAULT_BASE = config.span_base_url()
V1_CREDS = '/etc/span-viewer/span-local-credentials.json'


def _existing_v1_token(path: str):
    """The v1 accessToken authenticates v2 GET endpoints, which lets the
    pre-flight check existing client names before a proximity proof is spent.
    It cannot authorize auth/register on its own."""
    try:
        return json.loads(Path(path).read_text()).get('access_token')
    except (OSError, json.JSONDecodeError):
        return None


def _get(base: str, path: str, token) -> requests.Response:
    headers = {'Authorization': f'Bearer {token}'} if token else {}
    return requests.get(f'{base}{path}', headers=headers, timeout=15)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--base-url', default=DEFAULT_BASE, required=DEFAULT_BASE is None,
                    help='Panel base URL, http://<panel-ip> (default: http://SPAN_HOST: %(default)s)')
    ap.add_argument('--name', default='span-viewer-ebus',
                    help='Client name on the panel (default: %(default)s)')
    ap.add_argument('--hop-passphrase',
                    help='The passphrase itself (leaks into shell history; '
                         'prefer the prompt or --passphrase-stdin)')
    ap.add_argument('--passphrase-stdin', action='store_true',
                    help='Read the hopPassphrase from the first line of stdin')
    ap.add_argument('--door-bypass', action='store_true',
                    help='Use a proximity proof (3 door presses) instead')
    ap.add_argument('--v1-credentials', default=V1_CREDS,
                    help='Existing v1 creds, used for the read-only '
                         'pre-flight (default: %(default)s)')
    ap.add_argument('--out', default='/etc/span-viewer/span-api-credentials.json',
                    help='Where to write the credentials (default: %(default)s)')
    ap.add_argument('--ca-out', default='/etc/span-viewer/span-ca.pem',
                    help='Where to write the CA cert (default: %(default)s)')
    args = ap.parse_args()

    base = args.base_url.rstrip('/')
    passphrase = args.hop_passphrase
    if args.passphrase_stdin:
        passphrase = sys.stdin.readline().strip()
        if not passphrase:
            print('error: --passphrase-stdin got an empty line', file=sys.stderr)
            return 2
    elif not passphrase and not args.door_bypass:
        if not sys.stdin.isatty():
            print('error: no passphrase. Run at a terminal to be prompted, or '
                  'pass --passphrase-stdin or --door-bypass (the register '
                  'endpoint rejects a bearer token alone)', file=sys.stderr)
            return 2
        passphrase = getpass.getpass(
            'hopPassphrase (SPAN app -> Settings -> On-Prem Settings): ').strip()
        if not passphrase:
            print('error: empty passphrase', file=sys.stderr)
            return 2
    token = _existing_v1_token(args.v1_credentials)

    # --- pre-flight ------------------------------------------------------
    # Every check that could fail runs BEFORE we spend a single-use proof.
    try:
        st = _get(base, '/api/v2/status', token).json()
        print(f'panel {st["serialNumber"]} on {st["firmwareVersion"]}'
              f' (proximityProven={st["proximityProven"]})')
    except Exception as e:
        print(f'error: cannot reach {base}/api/v2/status: {e}', file=sys.stderr)
        return 1

    if args.door_bypass and not st['proximityProven']:
        print('error: proximity is not proven — press the door switch 3 times '
              '(opening the door counts as press 1), then re-run within ~15 min.',
              file=sys.stderr)
        return 1

    if token:
        r = _get(base, '/api/v2/auth/clients', token)
        if r.status_code == 200 and args.name in r.json().get('clients', {}):
            print(f'note: client {args.name!r} already registered; '
                  're-registering replaces it')

    if st['firmwareVersion'] < 'spanos3/r202633':
        print(f'note: {st["firmwareVersion"]} publishes the ARCHIVED flat MQTT '
              'model (r202603-r202627). r202633 restructures to parent/child '
              'devices — expect topic parsing to change.')

    # --- register --------------------------------------------------------
    body = {'name': args.name, 'description': 'span-viewer eBus MQTT client'}
    if passphrase:
        body['hopPassphrase'] = passphrase

    # No bearer header: register authorizes on the passphrase or the
    # proximity proof, and mixing in a token only muddies which one it used.
    # r202639 documents a 503 "Serial number is not available yet" while the
    # panel is still starting up. Retry that one only, and only with a
    # passphrase: whether a 503 spends a door-press proof is undocumented.
    for attempt in range(4):
        r = requests.post(f'{base}/api/v2/auth/register', json=body, timeout=45)
        if r.status_code != 503 or args.door_bypass or attempt == 3:
            break
        print(f'panel not ready to register yet (503); retrying in 15 s '
              f'({attempt + 1}/3)')
        time.sleep(15)
    if r.status_code != 200:
        print(f'registration failed: HTTP {r.status_code}: {r.text}',
              file=sys.stderr)
        if args.door_bypass:
            print('the proximity proof is now spent — press the door switch '
                  'again before retrying.', file=sys.stderr)
        return 1

    auth = r.json()
    missing = [k for k in ('accessToken', 'ebusBrokerHost', 'ebusBrokerUsername',
                           'ebusBrokerPassword', 'ebusBrokerMqttsPort')
               if not auth.get(k)]
    if missing:
        # From r202639 the panel answers 200 with ebusBrokerPassword (and
        # hopPassphrase) null when its own passphrase is unavailable. Nothing
        # is written, so the existing credentials file, if any, stays as it was.
        why = (' — the panel says its passphrase is unavailable (r202639+); '
               'check the SPAN app\'s On-Prem Settings, then retry'
               if 'ebusBrokerPassword' in missing else '')
        print(f'registration returned no {missing}{why}. Nothing was written.',
              file=sys.stderr)
        return 1

    # --- CA cert for MQTTS -----------------------------------------------
    # The broker cert is signed by this CA. Its SANs cover the panel's .local
    # name AND its LAN IP, so verifying by IP works.
    ca = _get(base, '/api/v2/certificate/ca', None)
    ca.raise_for_status()
    ca_path = Path(args.ca_out)
    ca_path.parent.mkdir(parents=True, exist_ok=True)
    ca_path.write_bytes(ca.content)
    ca_path.chmod(0o644)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        'base_url':          base,
        'client_name':       args.name,
        'access_token':      auth['accessToken'],
        'hostname':          auth.get('hostname'),
        'serial_number':     auth.get('serialNumber'),
        # Connect by IP, never by the reported .local name. A host without
        # mDNS resolves span-<serial>.local through its DNS search domain,
        # which may land on a wildcard record somewhere else entirely. A
        # client that did that with verification off would hand the broker
        # password to a third party.
        'broker_host':       base.split('://', 1)[-1].split(':')[0],
        'broker_host_reported': auth['ebusBrokerHost'],
        'broker_username':   auth['ebusBrokerUsername'],
        'broker_password':   auth['ebusBrokerPassword'],
        'broker_mqtts_port': auth['ebusBrokerMqttsPort'],
        'broker_ws_port':    auth.get('ebusBrokerWsPort'),
        'broker_wss_port':   auth.get('ebusBrokerWssPort'),
        'ca_path':           str(ca_path),
    }, indent=2) + '\n')
    out.chmod(0o600)

    print(f'wrote credentials to {out}')
    print(f'wrote CA cert to     {ca_path}')
    print(f'broker: {base.split("://", 1)[-1]}:{auth["ebusBrokerMqttsPort"]} '
          f'as {auth["ebusBrokerUsername"]} '
          f'(panel reports {auth["ebusBrokerHost"]})')
    if passphrase and auth.get('hopPassphrase') not in (None, passphrase):
        # Register accepted the passphrase, so a different one back would mean
        # the panel rotated it mid-call. Never seen; say so rather than hide it.
        print('warning: the panel returned a different hopPassphrase than the '
              'one supplied; re-read it from the SPAN app before saving it')
    print('\nnext: python scripts/span-mqtt-recon.py --minutes 15')
    return 0


if __name__ == '__main__':
    sys.exit(main())
