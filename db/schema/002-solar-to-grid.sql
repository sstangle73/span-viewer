-- 002-solar-to-grid.sql — add the `solar_to_grid` metric to panel_readings.
--
-- Discovered 2026-05-11: SPAN exposes `panel.solar.measurementHistory(subtype: GRID)`,
-- which represents solar production that went to grid (i.e., net-metering
-- export). Math validates: solar.ALL = solar.BUILDING_CONNECTION
--                                    + solar.GRID + solar.BATTERY exactly.
--
-- For this panel: BATTERY=0 (no SPAN-integrated battery; EcoFlow is portable),
-- BUILDING_CONNECTION == bc.SOLAR (= building_from_solar that we already
-- ingest), and GRID is the missing piece worth capturing for export $$.
--
-- Idempotent: drops + re-adds the CHECK constraint so re-applying just
-- updates the allowed set.

-- Only where the CHECK predates solar_to_grid: a database made from today's
-- 001 already has it, and there compression (enabled in 001) would refuse
-- the ALTER.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'panel_readings_metric_check'
          AND pg_get_constraintdef(oid) LIKE '%solar_to_grid%'
    ) THEN
        ALTER TABLE panel_readings DROP CONSTRAINT IF EXISTS panel_readings_metric_check;
        ALTER TABLE panel_readings ADD CONSTRAINT panel_readings_metric_check
            CHECK (metric IN (
                'solar', 'grid_consuming',
                'battery_charging', 'battery_discharging',
                'building_total', 'building_from_solar',
                'building_from_battery', 'building_from_grid',
                'solar_to_grid'
            ));
    END IF;
END $$;
