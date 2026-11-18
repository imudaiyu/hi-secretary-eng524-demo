-- ENG-517 Stage 3: idempotent backfill of workspace_agent_messages relational
-- columns for any rows inserted during the Stage 1 -> Stage 2 rollout window
-- before switching Core & Platform SQL readers off body_json.

DO $$
DECLARE tries integer := 0;
BEGIN
    LOOP
        BEGIN
            LOCK TABLE core.workspace_agent_messages IN SHARE UPDATE EXCLUSIVE MODE NOWAIT;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            tries := tries + 1;
            IF tries >= 60 THEN RAISE; END IF;
            PERFORM pg_sleep(0.5);
        END;
    END LOOP;
END $$;

SET LOCAL statement_timeout = '120s';

UPDATE core.workspace_agent_messages
   SET message_kind = NULLIF(body_json->>'kind', ''),
       channel = NULLIF(body_json->>'channel', ''),
       in_reply_to_agent_message_id = NULLIF(body_json->>'in_reply_to_agent_message_id', ''),
       target_message_id = NULLIF(body_json->>'target_message_id', ''),
       entry_surface = NULLIF(body_json->'entry'->>'surface', ''),
       entry_action = NULLIF(body_json->'entry'->>'action', ''),
       entry_object_type = NULLIF(body_json->'entry'->'object'->>'type', ''),
       entry_object_id = NULLIF(body_json->'entry'->'object'->>'id', ''),
       entry_work_revision = NULLIF(body_json->'entry'->>'work_revision', ''),
       background_work_id = NULLIF(body_json->'background_work'->>'work_id', ''),
       background_attempt_id = NULLIF(body_json->'background_work'->>'attempt_id', ''),
       relayed_notice_id = NULLIF(body_json->'relayed_notice'->>'notification_id', ''),
       relayed_notice_kind = NULLIF(body_json->'relayed_notice'->>'notification_kind', ''),
       relayed_message_notice_id = NULLIF(body_json->'relayed_message'->>'notification_id', ''),
       relayed_message_id = NULLIF(body_json->'relayed_message'->>'message_id', ''),
       meeting_card_share_id = NULLIF(body_json->'meeting_card'->>'share_id', ''),
       meeting_card_subject_person_id = NULLIF(body_json->'meeting_card'->>'subject_person_id', '')
 WHERE body_json IS NOT NULL
   AND (
     message_kind IS DISTINCT FROM NULLIF(body_json->>'kind', '')
     OR channel IS DISTINCT FROM NULLIF(body_json->>'channel', '')
     OR in_reply_to_agent_message_id IS DISTINCT FROM NULLIF(body_json->>'in_reply_to_agent_message_id', '')
     OR target_message_id IS DISTINCT FROM NULLIF(body_json->>'target_message_id', '')
     OR entry_surface IS DISTINCT FROM NULLIF(body_json->'entry'->>'surface', '')
     OR entry_action IS DISTINCT FROM NULLIF(body_json->'entry'->>'action', '')
     OR entry_object_type IS DISTINCT FROM NULLIF(body_json->'entry'->'object'->>'type', '')
     OR entry_object_id IS DISTINCT FROM NULLIF(body_json->'entry'->'object'->>'id', '')
     OR entry_work_revision IS DISTINCT FROM NULLIF(body_json->'entry'->>'work_revision', '')
     OR background_work_id IS DISTINCT FROM NULLIF(body_json->'background_work'->>'work_id', '')
     OR background_attempt_id IS DISTINCT FROM NULLIF(body_json->'background_work'->>'attempt_id', '')
     OR relayed_notice_id IS DISTINCT FROM NULLIF(body_json->'relayed_notice'->>'notification_id', '')
     OR relayed_notice_kind IS DISTINCT FROM NULLIF(body_json->'relayed_notice'->>'notification_kind', '')
     OR relayed_message_notice_id IS DISTINCT FROM NULLIF(body_json->'relayed_message'->>'notification_id', '')
     OR relayed_message_id IS DISTINCT FROM NULLIF(body_json->'relayed_message'->>'message_id', '')
     OR meeting_card_share_id IS DISTINCT FROM NULLIF(body_json->'meeting_card'->>'share_id', '')
     OR meeting_card_subject_person_id IS DISTINCT FROM NULLIF(body_json->'meeting_card'->>'subject_person_id', '')
   );

SET LOCAL statement_timeout = '30s';
