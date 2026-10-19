-- 001-initial.sql — initial schema for span-viewer.
-- Idempotent: every statement uses IF NOT EXISTS or its equivalent.
-- Apply against the `span` database as the `postgres` superuser.

BEGIN;

------------------------------------------------------------------------------
-- 1. Dimension tables
------------------------------------------------------------------------------

-- SPAN spaces (= physical breaker positions w/ a circuit attached).
-- 32 positions in the panel, but only ~22 have circuits attached. The
-- backfill JSON dirs are keyed by SPAN's UUID; this table maps UUID to
-- the human-readable name + room (from SPAN's `getSpace` query).
CREATE TABLE IF NOT EXISTS spaces (
    space_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    room        TEXT,
    -- Free-form metadata bucket (breaker amps, panel position, etc.).
    -- Backfilled from SPAN's API; nullable today.
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ingest run tracking — surfaces lag and failures for the dashboard.
CREATE TABLE IF NOT EXISTS ingest_runs (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at         TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running','success','failed')),
    error_text          TEXT,
    panel_rows_in       INTEGER,
    space_rows_in       INTEGER,
    panel_period_start  TIMESTAMPTZ,
    panel_period_end    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ingest_runs_started_at_idx ON ingest_runs (started_at DESC);

------------------------------------------------------------------------------
-- 2. Fact hypertables
------------------------------------------------------------------------------

-- Panel-level energy readings (solar / grid / battery / buildingConnection).
-- One row per (timestamp × metric). All values are kWh consumed in the
-- bucket; the duration column tracks the bucket width because SPAN
-- occasionally returns 30-min buckets when 10-min isn't available.
--
-- Metric naming convention:
--   'solar'                  — energy_kwh produced by solar
--   'grid_consuming'         — energy_kwh pulled FROM grid (import)
--   'battery_charging'       — energy_kwh INTO battery
--   'battery_discharging'    — energy_kwh OUT of battery
--   'building_total'         — total building consumption (= grid + solar + bat_dis - bat_chg, modulo losses)
--   'building_from_solar'    — portion of building consumption sourced from solar
--   'building_from_battery'  — portion sourced from battery
--   'building_from_grid'     — portion sourced from grid
--
-- Note: the historical backfill (2024-03 → 2026-05) has solar/grid/battery
-- but the buildingConnection sub-metrics are mostly empty (bc-fill ran but
-- the data never landed in the daily files — investigated 2026-05-10).
-- Going-forward ingest will capture all 8 metrics.
CREATE TABLE IF NOT EXISTS panel_readings (
    ts          TIMESTAMPTZ NOT NULL,
    metric      TEXT NOT NULL,
    energy_kwh  DOUBLE PRECISION NOT NULL,
    duration_s  INTEGER NOT NULL DEFAULT 600,
    PRIMARY KEY (ts, metric),
    CHECK (metric IN (
        'solar', 'grid_consuming',
        'battery_charging', 'battery_discharging',
        'building_total', 'building_from_solar',
        'building_from_battery', 'building_from_grid',
        -- 002-solar-to-grid.sql added this to databases made before it; a
        -- new one gets it here, since compression (below) blocks that ALTER.
        'solar_to_grid'
    ))
);

-- Per-space energy readings. One row per (timestamp × space).
CREATE TABLE IF NOT EXISTS space_readings (
    ts          TIMESTAMPTZ NOT NULL,
    space_id    TEXT NOT NULL REFERENCES spaces(space_id),
    energy_kwh  DOUBLE PRECISION NOT NULL,
    duration_s  INTEGER NOT NULL DEFAULT 600,
    PRIMARY KEY (ts, space_id)
);

-- Turn them into hypertables — 1 month per chunk. Justification:
--   panel:   144 rows/day × 8 metrics × 30 days ≈ 35k rows/chunk
--   spaces:  144 rows/day × 22 spaces × 30 days ≈ 95k rows/chunk
-- Way under the recommended ~25M-row chunk ceiling. Could go quarterly
-- chunks later if compression ratios are better, but starting monthly
-- to keep recent-data scans fast and uncompressed.
SELECT create_hypertable('panel_readings', 'ts',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE);

SELECT create_hypertable('space_readings', 'ts',
    chunk_time_interval => INTERVAL '1 month',
    if_not_exists => TRUE);

-- Index on metric so queries that filter by metric don't full-scan.
-- (PK is (ts, metric), so an additional index on metric alone helps for
-- "give me all solar across a year" pattern.)
CREATE INDEX IF NOT EXISTS panel_readings_metric_ts_idx
    ON panel_readings (metric, ts DESC);

CREATE INDEX IF NOT EXISTS space_readings_space_ts_idx
    ON space_readings (space_id, ts DESC);

------------------------------------------------------------------------------
-- 3. Continuous aggregates (hourly / daily / monthly rollups)
------------------------------------------------------------------------------

-- Hourly panel rollup.
CREATE MATERIALIZED VIEW IF NOT EXISTS panel_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts) AS hour,
    metric,
    SUM(energy_kwh) AS energy_kwh
FROM panel_readings
GROUP BY hour, metric
WITH NO DATA;

-- Daily panel rollup.
CREATE MATERIALIZED VIEW IF NOT EXISTS panel_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', ts) AS day,
    metric,
    SUM(energy_kwh) AS energy_kwh
FROM panel_readings
GROUP BY day, metric
WITH NO DATA;

-- Monthly panel rollup — built on top of panel_daily for cheaper refresh.
-- (TimescaleDB supports hierarchical continuous aggregates since 2.10.)
CREATE MATERIALIZED VIEW IF NOT EXISTS panel_monthly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 month', day) AS month,
    metric,
    SUM(energy_kwh) AS energy_kwh
FROM panel_daily
GROUP BY month, metric
WITH NO DATA;

-- Hourly per-space rollup.
CREATE MATERIALIZED VIEW IF NOT EXISTS space_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', ts) AS hour,
    space_id,
    SUM(energy_kwh) AS energy_kwh
FROM space_readings
GROUP BY hour, space_id
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS space_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', ts) AS day,
    space_id,
    SUM(energy_kwh) AS energy_kwh
FROM space_readings
GROUP BY day, space_id
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS space_monthly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 month', day) AS month,
    space_id,
    SUM(energy_kwh) AS energy_kwh
FROM space_daily
GROUP BY month, space_id
WITH NO DATA;

------------------------------------------------------------------------------
-- 4. Refresh policies
------------------------------------------------------------------------------

-- Refresh policies. TimescaleDB requires (start_offset - end_offset) to cover
-- at least 2 buckets of the view.

-- Hourly aggs: refresh every hour, cover the last 4 hours (so a late-arriving
-- bucket gets picked up). Window: 4h - 10m = 3h50m, covers 3 hourly buckets.
SELECT add_continuous_aggregate_policy('panel_hourly',
    start_offset => INTERVAL '4 hours',
    end_offset   => INTERVAL '10 minutes',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('space_hourly',
    start_offset => INTERVAL '4 hours',
    end_offset   => INTERVAL '10 minutes',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

-- Daily aggs: refresh every hour, cover the last 3 days. Window: 3d - 1h,
-- covers ~2 daily buckets.
SELECT add_continuous_aggregate_policy('panel_daily',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('space_daily',
    start_offset => INTERVAL '3 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE);

-- Monthly aggs: refresh once a day, cover the last 3 months. Window
-- needs to cover 2 monthly buckets (worst case 62 days).
SELECT add_continuous_aggregate_policy('panel_monthly',
    start_offset => INTERVAL '3 months',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

SELECT add_continuous_aggregate_policy('space_monthly',
    start_offset => INTERVAL '3 months',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE);

------------------------------------------------------------------------------
-- 5. Compression
------------------------------------------------------------------------------

-- Compress old chunks. Reasonable threshold: 90 days — recent data stays
-- in row-store for fast cell-level updates, older data gets column-store
-- compression (typically 90%+ ratio on time-series with low cardinality).
ALTER TABLE panel_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'metric',
    timescaledb.compress_orderby   = 'ts'
);

ALTER TABLE space_readings SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'space_id',
    timescaledb.compress_orderby   = 'ts'
);

SELECT add_compression_policy('panel_readings', INTERVAL '90 days', if_not_exists => TRUE);
SELECT add_compression_policy('space_readings', INTERVAL '90 days', if_not_exists => TRUE);

------------------------------------------------------------------------------
-- 6. Grants for the app role
------------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO span_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO span_app;
GRANT SELECT, USAGE ON ALL SEQUENCES IN SCHEMA public TO span_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO span_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, USAGE ON SEQUENCES TO span_app;

COMMIT;
