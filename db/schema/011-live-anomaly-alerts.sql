-- 011-live-anomaly-alerts.sql — state table for the live (intra-day) anomaly
-- alerter (`ingester/live_anomaly_alerter.py`).
--
-- Separate from `anomaly_alerts` (which tracks the daily, end-of-day version)
-- so the two alerters don't accidentally suppress each other. Dedup is per
-- (space_id, date): a circuit that goes anomalous mid-day fires once that
-- day, then stays quiet until the next day rolls over even if it keeps
-- running hot.

CREATE TABLE IF NOT EXISTS live_anomaly_alerts (
    space_id        TEXT             NOT NULL,
    date            DATE             NOT NULL,
    ratio           DOUBLE PRECISION,
    now_kwh         DOUBLE PRECISION,
    now_kw          DOUBLE PRECISION,
    baseline_kwh    DOUBLE PRECISION,
    detected_at     TIMESTAMPTZ      NOT NULL,
    alerted_at      TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (space_id, date)
);

CREATE INDEX IF NOT EXISTS live_anomaly_alerts_alerted_at_idx
    ON live_anomaly_alerts (alerted_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON live_anomaly_alerts TO span_app;
