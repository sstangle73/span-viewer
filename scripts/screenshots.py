#!/usr/bin/env python3
"""screenshots.py — retake the README's screenshots from the sample house.

Start the sample stack first (`docker compose up`), then:

    pip install playwright psycopg[binary] && playwright install chromium
    DATABASE_URL=postgresql://span_app:span@localhost:5432/span \\
        python scripts/screenshots.py --url http://localhost:8000

The sample stack has no panel, so /api/live has nothing to serve. The script
answers it in the browser with a snapshot built from the sample's own 3-s rows
(`panel_live` and `space_live`) at the sunniest moment of the last 24 h, so
/live looks like a real afternoon. Every other page is the app as it is.

Writes docs/screenshots/{live,yoy,solar,duty,discover}.png (or --out).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent

PAGES = [     # (file name, path)
    ('live', '/live'),
    ('yoy', '/yoy'),
    ('solar', '/solar'),
    ('duty', '/duty'),
    ('discover', '/discover?circuit=sample0000000000000000000kitchen&days=1'),
]


def live_snapshot(dsn: str) -> dict:
    """An /api/live-shaped snapshot from the sample's sunniest 3-s tick."""
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute('SELECT ts, grid_power_w, feedthrough_power_w, solar_power_w, load_power_w,'
                    ' phase_a_w, phase_b_w FROM panel_live ORDER BY solar_power_w DESC NULLS LAST LIMIT 1')
        row = cur.fetchone()
        if row is None:
            sys.exit('screenshots: panel_live is empty; load the sample data first')
        ts, grid, feed, solar, load, pa, pb = row
        cur.execute('SELECT l.space_id, COALESCE(s.name, l.space_id), l.power_w FROM space_live l'
                    ' LEFT JOIN spaces s USING (space_id) WHERE l.ts = %s ORDER BY l.power_w DESC', (ts,))
        circuits = cur.fetchall()
    out = []
    for i, (sid, name, power) in enumerate(circuits, start=1):
        if sid == '__feedthrough__':
            name = 'Sub-panel'
        out.append({'id': sid, 'name': name, 'power_w': round(power or 0.0, 1), 'relay': 'CLOSED',
                    'priority': 'NICE_TO_HAVE', 'tabs': [] if sid == '__feedthrough__' else [i]})
    imbalance = abs(pa - pb) / max(pa + pb, 1) * 100
    panel = {'grid_power_w': grid, 'feedthrough_power_w': feed, 'solar_power_w': solar,
             'load_power_w': load, 'solar_share_pct': min(100.0, solar / max(load, 1) * 100),
             'phase_a_w': pa, 'phase_b_w': pb, 'phase_imbalance_pct': imbalance,
             'main_relay': 'CLOSED', 'grid_state': 'DSM_GRID_UP', 'run_config': None}
    return {'panel': panel, 'circuits': out, 'error': None, 'raw': {'source': 'sample'},
            'cached_at_s': 1.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--url', default='http://localhost:8000')
    ap.add_argument('--out', default=str(ROOT / 'docs' / 'screenshots'))
    ap.add_argument('--width', type=int, default=1280)
    ap.add_argument('--height', type=int, default=900)
    args = ap.parse_args()
    dsn = os.environ.get('DATABASE_URL')
    if not dsn:
        sys.exit('screenshots: set DATABASE_URL to the sample database')

    from playwright.sync_api import sync_playwright

    snap = json.dumps(live_snapshot(dsn))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width': args.width, 'height': args.height},
                                color_scheme='dark')
        page.route('**/api/live', lambda route: route.fulfill(
            status=200, content_type='application/json', body=snap))
        for name, path in PAGES:
            page.goto(args.url + path, wait_until='networkidle')
            if name == 'live':
                page.click('.pickers .pill[data-window="24h"]')
                page.wait_for_load_state('networkidle')
            page.wait_for_timeout(1500)     # ECharts' entry animation
            page.screenshot(path=str(out / f'{name}.png'))
            print(f'screenshots: {out / name}.png')
        browser.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
