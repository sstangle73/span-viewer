#!/usr/bin/env python3
"""ongoing.py — single-shot ingest of today's 10-min buckets.

Fired by the span-viewer-ingest systemd timer every 10 minutes. Pulls the
current day + previous day at 10-min resolution from SPAN cloud, upserts
into the same hypertables that `backfill.py` populates.

Two days, not one, because SPAN's panel-to-cloud lag is ~10-15 minutes —
near a day boundary (e.g. 23:55 UTC) we'd otherwise miss the prior day's
last buckets until the next day's run picked them up. Also catches late
arrivals from buckets that got revised on SPAN's side.

Idempotent via the same DISTINCT ON + ON CONFLICT UPDATE pattern used in
backfill.py.
"""

import sys
import time

from .backfill import (
    SpanCloud,
    bulk_upsert_panel,
    bulk_upsert_space_readings,
    bulk_upsert_spaces_dim,
    close_run,
    db_connect,
    fetch_panel_window,
    fetch_spaces_window,
    open_run,
)

# How many recent days to refetch at 10-min granularity each cycle. 2 covers
# the prior-day boundary edge case described above. Cheap — each "day" is
# one GraphQL call.
RECENT_DAYS = 2


def main() -> int:
    started = time.time()
    conn = db_connect()
    run_id = open_run(conn)
    conn.commit()

    panel_rows: list[tuple] = []
    space_rows: list[tuple] = []
    meta_by_sid: dict = {}

    try:
        cloud = SpanCloud()

        for wb in range(0, RECENT_DAYS):
            panel_rows.extend(fetch_panel_window(cloud, 'DAY', 'PHONE_PORTRAIT', wb))
            srows, meta = fetch_spaces_window(cloud, 'DAY', 'PHONE_PORTRAIT', wb)
            space_rows.extend(srows)
            for sid, name, room in meta:
                meta_by_sid.setdefault(sid, (name, room))

        space_meta = [(sid, n, r) for sid, (n, r) in meta_by_sid.items()]

        if panel_rows:
            bulk_upsert_panel(conn, panel_rows)
        if space_meta:
            bulk_upsert_spaces_dim(conn, space_meta)
        if space_rows:
            bulk_upsert_space_readings(conn, space_rows)
        conn.commit()

        panel_ts = [r[0] for r in panel_rows] if panel_rows else []
        close_run(
            conn, run_id, 'success',
            panel_rows_in=len(panel_rows),
            space_rows_in=len(space_rows),
            panel_period_start=min(panel_ts) if panel_ts else None,
            panel_period_end=max(panel_ts) if panel_ts else None,
        )
        conn.commit()
    except Exception as e:
        try:
            close_run(conn, run_id, 'failed', error_text=str(e)[:1000])
            conn.commit()
        except Exception:
            pass
        raise
    finally:
        conn.close()

    elapsed = time.time() - started
    print(f'ongoing ingest done in {elapsed:.1f}s — '
          f'panel={len(panel_rows)} space={len(space_rows)}',
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
