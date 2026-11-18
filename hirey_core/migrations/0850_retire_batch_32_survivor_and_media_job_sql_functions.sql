-- ENG-369 Phase 3 (D0.25): retire the Batch 32 merged-survivor finality and
-- media-job retention guard SQL functions with zero remaining SQL or runtime
-- callers across hi-secretary, hi-platform, and hi-search.
-- Rollback artifact: hirey_core/migrations_restore/0850_retire_batch_32_survivor_and_media_job_sql_functions_restore.sql

DROP FUNCTION IF EXISTS core.assert_survivor_is_final(text,text) RESTRICT;
DROP FUNCTION IF EXISTS core.guard_media_job_retention(text,text,text,text) RESTRICT;
