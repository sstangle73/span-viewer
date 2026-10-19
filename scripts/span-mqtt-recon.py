#!/usr/bin/env python3
"""span-mqtt-recon.py — subscribe to the SPAN panel's eBus MQTT broker, log
everything, and answer the questions the SPAN API docs don't.

Nothing in SPAN-API-Client-Docs states a publish cadence, deadband, or throttle
for the Homie property topics. That number decides whether the MQTT interface
can replace the 3-s polling loop in app/live_store.py that fills `space_live`
and underpins every NILM route. This script measures it, plus two other unknowns
that only the live panel can answer:

  1. Publish cadence     per-topic inter-message interval (median / p05 / p95),
                         and whether values repeat (periodic) or only change
                         (deadbanded / on-change). From r202639 the broker
                         stops republishing a payload identical to the last
                         one, so gaps then measure how often a value CHANGES,
                         not how often the panel samples. The useful number is
                         the fastest sustained rate on the busiest topics (the
                         p05 gap): that bounds the panel's sampling period. A
                         3-s writer has to forward-fill, and silence on a
                         topic is not staleness (use $state for liveness).
  2. Is PV commissioned? v1 REST has no solar field at all, which is why
                         app/span_local.py infers production by summing the
                         branches carrying the solar disconnect. If the panel
                         publishes a real `power-flows/pv`, that heuristic dies.
  3. Energy accumulators do the per-circuit imported-energy / exported-energy
                         counters advance usefully? They would replace
                         integrating instantaneous power for session kWh.

Schema-agnostic on purpose: it classifies topics by shape, so it works on both
the archived flat model (r202603-r202627) and the parent/child model (r202633+),
and reports which one the panel is publishing.

Reads /etc/span-viewer/span-api-credentials.json (see register-span-api-v2.py).

    python scripts/span-mqtt-recon.py --minutes 15
    python scripts/span-mqtt-recon.py --minutes 60 --jsonl /tmp/ebus.jsonl

The JSONL log is the durable artifact — the summary is just a first pass over
it, and every claim in the summary can be re-derived from the log later.
"""

import argparse
import collections
import json
import ssl
import statistics
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

CREDS = '/etc/span-viewer/span-api-credentials.json'

# Property suffixes worth calling out individually in the summary.
METERING = ('active-power', 'current', 'current-a', 'current-b',
            'imported-energy', 'exported-energy', 'voltage-a', 'voltage-b')


class Recon:
    def __init__(self, jsonl_path):
        self.msgs = collections.defaultdict(list)   # topic -> [(ts, payload)]
        self.retained = collections.Counter()
        self.descriptions = {}                      # device id -> parsed JSON
        self.states = {}
        self.started = time.time()
        self.log = open(jsonl_path, 'w', encoding='utf-8') if jsonl_path else None
        self.total = 0

    def on_message(self, _client, _userdata, msg):
        ts = time.time()
        try:
            payload = msg.payload.decode('utf-8')
        except UnicodeDecodeError:
            payload = repr(msg.payload)
        self.total += 1

        if self.log:
            self.log.write(json.dumps({
                'ts': ts, 'topic': msg.topic,
                'payload': payload, 'retain': bool(msg.retain),
            }) + '\n')

        if msg.retain:
            # Retained messages are the broker replaying last-known state on
            # subscribe, not live publishes — they would poison cadence stats.
            self.retained[msg.topic] += 1

        if msg.topic.endswith('/$description'):
            device = msg.topic.split('/')[2]
            try:
                self.descriptions[device] = json.loads(payload)
            except json.JSONDecodeError:
                pass
            return
        if msg.topic.endswith('/$state'):
            self.states[msg.topic.split('/')[2]] = payload
            return
        if msg.topic.startswith('ebus/') and '$' not in msg.topic:
            self.msgs[msg.topic].append((ts, payload, bool(msg.retain)))

    def close(self):
        if self.log:
            self.log.close()


def _intervals(samples):
    """Inter-message gaps in seconds, live (non-retained) messages only."""
    live = [ts for ts, _p, retain in samples if not retain]
    return [b - a for a, b in zip(live, live[1:])]


def _fmt_stats(gaps):
    if not gaps:
        return 'no live updates'
    gaps = sorted(gaps)
    med = statistics.median(gaps)
    p05 = gaps[int(len(gaps) * 0.05)]
    p95 = gaps[int(len(gaps) * 0.95)]
    return f'median {med:6.2f}s  p05 {p05:6.2f}s  p95 {p95:6.2f}s  n={len(gaps)}'


def report(r: Recon, minutes: float) -> None:
    elapsed = time.time() - r.started
    print(f'\n{"=" * 78}')
    print(f'== recon summary — {elapsed / 60:.1f} min, {r.total} messages, '
          f'{len(r.msgs)} property topics')
    print('=' * 78)

    # --- which data model? ----------------------------------------------
    parents = [d for d, desc in r.descriptions.items() if desc.get('children')]
    print('\n-- data model --')
    print(f'devices publishing $description: {len(r.descriptions)}')
    if parents:
        kids = sum(len(r.descriptions[d].get('children', [])) for d in parents)
        print(f'PARENT/CHILD model (r202633+): {len(parents)} parent(s), '
              f'{kids} child device(s)')
    elif r.descriptions:
        print('FLAT model (r202603-r202627): single device, nodes as children')
        for d, desc in r.descriptions.items():
            print(f'  {d}: nodes = {sorted(desc.get("nodes", {}))}')
    for d, state in sorted(r.states.items()):
        print(f'  $state {d} = {state}')

    # --- cadence ---------------------------------------------------------
    print('\n-- publish cadence, metering topics (the NILM question) --')
    metering = {t: s for t, s in r.msgs.items()
                if t.rsplit('/', 1)[-1] in METERING}
    if not metering:
        print('  none seen — check the subscription filter or wait longer')
    for topic in sorted(metering)[:40]:
        samples = metering[topic]
        gaps = _intervals(samples)
        vals = [p for _t, p, retain in samples if not retain]
        distinct = len(set(vals))
        repeats = sum(1 for a, b in zip(vals, vals[1:]) if a == b)
        shape = (f'{repeats} back-to-back repeats → periodic' if repeats
                 else 'no repeats → on-change')
        print(f'  {topic.split("/", 2)[-1]:<52} {_fmt_stats(gaps)}  '
              f'{distinct}/{len(vals)} distinct, {shape}')
    if len(metering) > 40:
        print(f'  ... and {len(metering) - 40} more metering topics '
              f'(full detail in the JSONL log)')

    all_gaps = [g for t, s in metering.items() for g in _intervals(s)]
    if all_gaps:
        print(f'\n  ACROSS ALL METERING TOPICS: {_fmt_stats(all_gaps)}')
        print('    (with on-change publishing this mostly measures how often '
              'loads change, not resolution)')
        # Busy topics — grid lugs, anything with a motor or a compressor —
        # change on nearly every sample, so their fastest sustained gap is the
        # best available estimate of the panel's sampling period.
        busiest = sorted(metering, key=lambda t: -len(_intervals(metering[t])))[:5]
        fast = sorted(g for t in busiest for g in _intervals(metering[t]))
        if fast:
            period = fast[int(len(fast) * 0.05)]
            print(f'  BUSIEST 5 TOPICS, p05 gap: {period:.2f}s '
                  f'≈ the panel\'s sampling period')
            verdict = ('MQTT can feed space_live at the current 3-s resolution '
                       '(forward-fill between messages)'
                       if period <= 3.5 else
                       f'the panel samples slower than the 3-s poll — space_live '
                       f'resolution would drop to ~{period:.0f}s')
            print(f'  → {verdict}')

    # --- is PV commissioned? --------------------------------------------
    print('\n-- power-flows (does the panel report solar directly?) --')
    flows = {t: s for t, s in r.msgs.items() if '/power-flows/' in t}
    if not flows:
        print('  no power-flows topics seen — panel may not publish this '
              'capability, keep inferring solar from the branch sum')
    for topic in sorted(flows):
        vals = []
        for _t, p, _r in flows[topic]:
            try:
                vals.append(float(p))
            except ValueError:
                pass
        prop = topic.rsplit('/', 1)[-1]
        if not vals:
            print(f'  {prop:<10} published but non-numeric')
            continue
        nonzero = sum(1 for v in vals if abs(v) > 1.0)
        print(f'  {prop:<10} n={len(vals):<5} min={min(vals):10.1f} '
              f'max={max(vals):10.1f}  nonzero={nonzero}/{len(vals)}')
        if prop == 'pv':
            print('    → PV IS COMMISSIONED, drop the branch-sum heuristic'
                  if nonzero else
                  '    → always ~0: either night, or PV is not commissioned as '
                  'a device. Re-run in daylight before concluding.')

    # --- energy accumulators --------------------------------------------
    print('\n-- energy accumulators (would replace integrating power) --')
    energy = {t: s for t, s in r.msgs.items()
              if t.endswith(('imported-energy', 'exported-energy'))}
    advanced = stuck = 0
    for topic, samples in energy.items():
        vals = []
        for _t, p, _r in samples:
            try:
                vals.append(float(p))
            except ValueError:
                pass
        if len(vals) >= 2 and vals[-1] > vals[0]:
            advanced += 1
        elif vals:
            stuck += 1
    print(f'  {len(energy)} accumulator topics: {advanced} advanced during '
          f'the window, {stuck} flat')
    if energy and not advanced:
        print('  → nothing moved; either the window was too short or these '
              'update on a slow cycle. Re-run with a longer --minutes.')

    print(f'\n-- retained burst on subscribe: {sum(r.retained.values())} '
          f'messages across {len(r.retained)} topics --')
    print('   (full state on reconnect, so a restart needs no warm-up poll)')


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('--credentials', default=CREDS,
                    help='SPAN API credentials JSON (default: %(default)s)')
    ap.add_argument('--minutes', type=float, default=15.0,
                    help='How long to listen (default: %(default)s)')
    ap.add_argument('--jsonl', default='/tmp/span-ebus-recon.jsonl',
                    help='Raw message log (default: %(default)s)')
    ap.add_argument('--topic', default='ebus/#',
                    help='Subscription filter (default: %(default)s)')
    ap.add_argument('--insecure', action='store_true',
                    help='Skip TLS verification. Should never be needed: the '
                         'broker cert covers the panel IP.')
    args = ap.parse_args()

    try:
        cfg = json.loads(Path(args.credentials).read_text())
    except OSError as e:
        print(f'error: {e}\nrun scripts/register-span-api-v2.py first',
              file=sys.stderr)
        return 2

    host = cfg['broker_host']
    if host.endswith('.local'):
        # See register-span-api-v2.py: without mDNS, .local falls through
        # the DNS search domain to whatever a wildcard record points at.
        print(f'error: refusing to connect to {host} — .local does not resolve '
              f'to the panel here. Re-run register-span-api-v2.py.',
              file=sys.stderr)
        return 2
    port = int(cfg.get('broker_mqtts_port', 8883))
    r = Recon(args.jsonl)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f'span-viewer-recon-{int(time.time())}')
    client.username_pw_set(cfg['broker_username'], cfg['broker_password'])

    ca = cfg.get('ca_path')
    if args.insecure:
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.tls_insecure_set(True)
        print('TLS: UNVERIFIED (--insecure)')
    elif not ca or not Path(ca).exists():
        print(f'error: CA cert {ca!r} missing — re-run register-span-api-v2.py '
              f'(or pass --insecure to connect unverified)', file=sys.stderr)
        return 2
    else:
        # SPAN's CA and broker certs lack an Authority Key Identifier, which
        # Python 3.13+ rejects under VERIFY_X509_STRICT (on by default in
        # create_default_context). Clear only that flag: chain verification
        # and the IP-SAN hostname check stay on. Verified against the panel
        # 2026-09-13, including a wrong-IP negative control.
        ctx = ssl.create_default_context(cafile=ca)
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        client.tls_set_context(ctx)
        print(f'TLS: verifying against {ca} (X509 strict relaxed: no AKI)')

    # No LWT: the broker discards client wills on SPAN device topics, and we
    # have no device tree of our own to announce.
    client.on_message = r.on_message
    client.on_connect = lambda c, *_a: (
        print(f'connected to {host}:{port}, subscribing to {args.topic}'),
        c.subscribe(args.topic, qos=0),
    )

    try:
        client.connect(host, port, keepalive=60)
    except Exception as e:
        print(f'error: connect to {host}:{port} failed: {e}', file=sys.stderr)
        return 1

    client.loop_start()
    deadline = time.time() + args.minutes * 60
    try:
        while time.time() < deadline:
            time.sleep(5)
            print(f'  ... {r.total} messages, {len(r.msgs)} topics '
                  f'({(deadline - time.time()) / 60:.1f} min left)',
                  end='\r', flush=True)
    except KeyboardInterrupt:
        print('\ninterrupted — reporting on what we have')
    finally:
        client.loop_stop()
        client.disconnect()
        r.close()

    report(r, args.minutes)
    print(f'\nraw log: {args.jsonl}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
