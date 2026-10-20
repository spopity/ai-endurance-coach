-- Adaptive periodization (core/periodization.py): training_blocks is now rewritten
-- from the training actually done instead of a fixed calendar. status is the verdict
-- on a closed week ('completed' | 'partial' | 'missed' | 'recovery'; NULL for this
-- week and projected weeks); notes holds why the week got its phase.
ALTER TABLE training_blocks ADD COLUMN IF NOT EXISTS status TEXT;
