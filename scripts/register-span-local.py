#!/usr/bin/env python3
"""register-span-local.py — register this LXC as a client of the SPAN
panel's local HTTP API and write the resulting bearer token to
/etc/span-viewer/span-local-credentials.json.

One-time setup. Two paths:

  --hop-passphrase PASS    From SPAN consumer app → Settings → On-Prem
                           Settings (the "hopPassphrase" shown there).

  --door-bypass            Physically open the panel door within ~30 s of
                           running this script. Use when you don't have
                           the hopPassphrase handy.

Run on the span-viewer LXC (needs root to write into /etc/):

    sudo python3 scripts/register-span-local.py \\
        --hop-passphrase 'PASS-FROM-APP' --name span-viewer

The token survives panel reboots; re-running with the same --name is
idempotent (SPAN keeps one client entry per name).
"""

import argparse
import json
import sys
from pathlib import Path

import requests

# SPAN_HOST from the environment or /etc/span-viewer/span-viewer.env (app/config.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--base-url', default=config.span_base_url(),
                    required=config.span_base_url() is None,
                    help='Panel base URL (default: %(default)s)')
    ap.add_argument('--hop-passphrase',
                    help='From SPAN app -> Settings -> On-Prem Settings')
    ap.add_argument('--door-bypass', action='store_true',
                    help='Use door-bypass; open panel door within 30s')
    ap.add_argument('--name', default='span-viewer',
                    help='Client name on the panel (default: %(default)s)')
    ap.add_argument('--out',
                    default='/etc/span-viewer/span-local-credentials.json',
                    help='Where to write the credentials (default: %(default)s)')
    args = ap.parse_args()

    if not args.hop_passphrase and not args.door_bypass:
        print('error: pass --hop-passphrase or --door-bypass', file=sys.stderr)
        return 2

    body = {
        'name': args.name,
        'description': 'span-viewer real-time dashboard',
    }
    if args.hop_passphrase:
        body['hopPassphrase'] = args.hop_passphrase

    url = f'{args.base_url.rstrip("/")}/api/v1/auth/register'
    if args.door_bypass:
        print('Door-bypass mode: open the panel door now; '
              'registration accepts within ~30 s.')

    r = requests.post(url, json=body, timeout=45)

    if r.status_code != 200:
        print(f'registration failed: HTTP {r.status_code}: {r.text}',
              file=sys.stderr)
        return 1

    token = r.json().get('accessToken')
    if not token:
        print(f'no accessToken in response: {r.text}', file=sys.stderr)
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        'base_url': args.base_url,
        'access_token': token,
    }) + '\n')
    out_path.chmod(0o600)
    print(f'wrote credentials to {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
