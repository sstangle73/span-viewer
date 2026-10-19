-- 016-reading-patches.sql — a record of every reading span-viewer corrected
-- by hand, with its old value, so a refill can be audited or undone.
--
-- First use (2026-10-18 audit): SPAN's cloud lost parts of 06-15 to 07-17;
-- 10-minute buckets where VictoriaMetrics' 30-s SPAN data was live (at least
-- 18 of its 20 samples changing) were refilled from it. Buckets where VM's
-- samples were frozen (the panel unreachable, the exporter repeating its last
-- value) were left as they were: no source on the network has them.

BEGIN;

CREATE TABLE IF NOT EXISTS reading_patches (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('panel', 'space')),
    key         TEXT NOT NULL,              -- panel metric, or space_id
    old_kwh     DOUBLE PRECISION,           -- NULL = the row didn't exist
    new_kwh     DOUBLE PRECISION NOT NULL,
    source      TEXT NOT NULL,
    patched_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS reading_patches_ts_idx ON reading_patches (ts);

GRANT SELECT, INSERT ON reading_patches TO span_app;
GRANT USAGE ON SEQUENCE reading_patches_id_seq TO span_app;

COMMIT;
