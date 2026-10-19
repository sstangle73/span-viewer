-- 012-space-live.sql — high-cadence (every 3 s) per-circuit power snapshots,
-- the substrate for NILM (non-intrusive load monitoring) and circuit-level
-- edge detection.
--
-- Filled by app.live_store's poller in the same tick that writes panel_live.
-- Where panel_live captures only panel aggregates, this hypertable captures
-- every circuit (plus a synthetic '__feedthrough__' row for the sub-panel)
-- so we can find load signatures on mixed circuits (e.g. a window AC sharing
-- an outlet breaker).
--
-- Volume sanity check at 3-s cadence × ~22 circuits:
--   ~22 × 28,800 = ~634k rows/day × ~30 bytes/row ≈ 19 MB/day raw.
--   30-day retention × ~10× compression = ~57 MB total. Easy.

BEGIN;

CREATE TABLE IF NOT EXISTS space_live (
    ts        TIMESTAMPTZ NOT NULL,
    space_id  TEXT        NOT NULL,
    power_w   REAL,
    PRIMARY KEY (ts, space_id)
);

-- 4-h chunks → ~106k rows/chunk, a comfortable target for compression
-- without making the retention policy's drop boundary too coarse.
SELECT create_hypertable('space_live', 'ts',
    chunk_time_interval => INTERVAL '4 hours',
    if_not_exists => TRUE);

-- Compress chunks older than 1 day, segmented by space_id so each
-- circuit's time series is its own compressed run. Typical compression
-- ratio for sparse per-circuit data is 10-20x.
ALTER TABLE space_live SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'space_id'
);

SELECT add_compression_policy('space_live', INTERVAL '1 day',
    if_not_exists => TRUE);

-- 30 days of high-res data is plenty for finding any repeating appliance
-- signature (window ACs cycle 8-12× per heat wave; dishwashers run daily;
-- thermostat cycles regularly). Older context lives in 10-min space_readings.
SELECT add_retention_policy('space_live', INTERVAL '30 days',
    if_not_exists => TRUE);

GRANT SELECT, INSERT, UPDATE, DELETE ON space_live TO span_app;

COMMIT;
