-- Planned session mix per week, e.g. {"endurance": 3, "sst": 1, "vo2": 0, "run": 3, "weights": 4}.
-- Autumn-spring template: 3 runs, 3-4 rides and 3-4 weights sessions a week, with
-- ride intensity following the 3-week Build + 1-week Recovery cycle (same numbers as
-- SESSION_TEMPLATE in scripts/generate_rolling_plan.py). Hand-edit any week freely.
ALTER TABLE training_blocks ADD COLUMN IF NOT EXISTS sessions jsonb;

UPDATE training_blocks SET sessions = CASE
    WHEN is_deload                          THEN '{"endurance": 3, "sst": 0, "vo2": 0, "run": 3, "weights": 3}'
    WHEN primary_focus LIKE 'Aerobic%'      THEN '{"endurance": 3, "sst": 1, "vo2": 0, "run": 3, "weights": 4}'
    WHEN primary_focus LIKE 'Threshold%'    THEN '{"endurance": 2, "sst": 2, "vo2": 0, "run": 3, "weights": 4}'
    WHEN primary_focus LIKE 'Peak load%'    THEN '{"endurance": 2, "sst": 1, "vo2": 1, "run": 3, "weights": 3}'
END::jsonb
WHERE sessions IS NULL;
