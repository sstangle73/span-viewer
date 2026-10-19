-- 010-panel-live.sql — high-cadence (every 3 s) panel-level snapshots for
-- the /live page. Backs the zoomable strip chart so refreshing the page
-- doesn't lose the rolling history.
--
-- Filled by an asyncio task inside the FastAPI app (`app.live_store`),
-- not by the systemd-fired ingester. Only panel-level data lives here;
-- per-circuit power is still client-only since /live's per-circuit view
-- is a snapshot and the top-mover ring buffer is short-lived.
--
-- Volume sanity check at 3-s cadence:
--   28,800 rows/day × ~50 bytes/row ≈ 1.5 MB/day raw (~10× compressed)
-- so 25 h of retention is well under a typical hypertable's working set.

BEGIN;

CREATE TABLE IF NOT EXISTS panel_live (
    ts                  TIMESTAMPTZ      NOT NULL PRIMARY KEY,
    grid_power_w        REAL,
    feedthrough_power_w REAL,
    solar_power_w       REAL,
    load_power_w        REAL,
    phase_a_w           REAL,
    phase_b_w           REAL
);

-- 1-hour chunks so the retention policy's drop boundary stays tight
-- (chunks drop only when entirely older than the policy's age, so big
-- chunks would keep too-old data alive). At 1 h × 1200 rows that's
-- a tiny chunk by Timescale's recommendations but the retention math
-- matters more than chunk efficiency here.
SELECT create_hypertable('panel_live', 'ts',
    chunk_time_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

-- 25 h retention — covers the UI's longest zoom window (24 h) plus an
-- hour of headroom so a zoom-out at xx:59 doesn't return a half-empty
-- chart. Add-policy is idempotent in TimescaleDB ≥2.x.
SELECT add_retention_policy('panel_live', INTERVAL '25 hours',
    if_not_exists => TRUE);

GRANT SELECT, INSERT, UPDATE, DELETE ON panel_live TO span_app;

COMMIT;
