-- PLACEHOLDER. Version 0015 is reserved for a task running in parallel with A2, and the
-- migrator refuses a gap, so 0016 cannot apply on this branch without something here.
-- It changes nothing. When the branches merge, this file and its down file are dropped
-- and that task's 0015 takes the version. Never apply it to a database that outlives a
-- test run: the real 0015's checksum will differ.
SELECT 1;
