-- 004-anomaly-alerts.sql — state table for the anomaly→ntfy alerter.
--
-- Records every (space_id, date) anomaly that has been pushed to ntfy
-- so we don't re-alert on the same event each time the systemd timer
-- fires. Held in the DB rather than a JSON file so the data + dedupe
-- state stay co-located.

CREATE TABLE IF NOT EXISTS anomaly_alerts (
    space_id     TEXT             NOT NULL,
    date         DATE             NOT NULL,
    ratio        DOUBLE PRECISION,
    kwh          DOUBLE PRECISION,
    baseline     DOUBLE PRECISION,
    alerted_at   TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    PRIMARY KEY (space_id, date)
);

CREATE INDEX IF NOT EXISTS anomaly_alerts_alerted_at_idx
    ON anomaly_alerts (alerted_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON anomaly_alerts TO span_app;
