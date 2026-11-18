-- Relational columns and partial B-tree indexes for
-- core.workspace_agent_messages correlation keys and message attributes.
-- Acquire ACCESS EXCLUSIVE with NOWAIT retries (0271 pattern) before Alter so
-- lock contention never queues concurrent readers/writers behind an active query.

DO $$
DECLARE tries integer := 0;
BEGIN
    LOOP
        BEGIN
            LOCK TABLE core.workspace_agent_messages IN ACCESS EXCLUSIVE MODE NOWAIT;
            EXIT;
        EXCEPTION WHEN lock_not_available THEN
            tries := tries + 1;
            IF tries >= 40 THEN RAISE; END IF;
            PERFORM pg_sleep(0.5);
        END;
    END LOOP;
END $$;

SET LOCAL statement_timeout = '120s';

ALTER TABLE core.workspace_agent_messages
    ADD COLUMN IF NOT EXISTS message_kind text,
    ADD COLUMN IF NOT EXISTS channel text,
    ADD COLUMN IF NOT EXISTS in_reply_to_agent_message_id text,
    ADD COLUMN IF NOT EXISTS target_message_id text,
    ADD COLUMN IF NOT EXISTS entry_surface text,
    ADD COLUMN IF NOT EXISTS entry_action text,
    ADD COLUMN IF NOT EXISTS entry_object_type text,
    ADD COLUMN IF NOT EXISTS entry_object_id text,
    ADD COLUMN IF NOT EXISTS entry_work_revision text,
    ADD COLUMN IF NOT EXISTS background_work_id text,
    ADD COLUMN IF NOT EXISTS background_attempt_id text,
    ADD COLUMN IF NOT EXISTS relayed_notice_id text,
    ADD COLUMN IF NOT EXISTS relayed_notice_kind text,
    ADD COLUMN IF NOT EXISTS relayed_message_notice_id text,
    ADD COLUMN IF NOT EXISTS relayed_message_id text,
    ADD COLUMN IF NOT EXISTS meeting_card_share_id text,
    ADD COLUMN IF NOT EXISTS meeting_card_subject_person_id text;

UPDATE core.workspace_agent_messages
   SET message_kind = COALESCE(message_kind, NULLIF(body_json->>'kind', '')),
       channel = COALESCE(channel, NULLIF(body_json->>'channel', '')),
       in_reply_to_agent_message_id = COALESCE(in_reply_to_agent_message_id, NULLIF(body_json->>'in_reply_to_agent_message_id', '')),
       target_message_id = COALESCE(target_message_id, NULLIF(body_json->>'target_message_id', '')),
       entry_surface = COALESCE(entry_surface, NULLIF(body_json->'entry'->>'surface', '')),
       entry_action = COALESCE(entry_action, NULLIF(body_json->'entry'->>'action', '')),
       entry_object_type = COALESCE(entry_object_type, NULLIF(body_json->'entry'->'object'->>'type', '')),
       entry_object_id = COALESCE(entry_object_id, NULLIF(body_json->'entry'->'object'->>'id', '')),
       entry_work_revision = COALESCE(entry_work_revision, NULLIF(body_json->'entry'->>'work_revision', '')),
       background_work_id = COALESCE(background_work_id, NULLIF(body_json->'background_work'->>'work_id', '')),
       background_attempt_id = COALESCE(background_attempt_id, NULLIF(body_json->'background_work'->>'attempt_id', '')),
       relayed_notice_id = COALESCE(relayed_notice_id, NULLIF(body_json->'relayed_notice'->>'notification_id', '')),
       relayed_notice_kind = COALESCE(relayed_notice_kind, NULLIF(body_json->'relayed_notice'->>'notification_kind', '')),
       relayed_message_notice_id = COALESCE(relayed_message_notice_id, NULLIF(body_json->'relayed_message'->>'notification_id', '')),
       relayed_message_id = COALESCE(relayed_message_id, NULLIF(body_json->'relayed_message'->>'message_id', '')),
       meeting_card_share_id = COALESCE(meeting_card_share_id, NULLIF(body_json->'meeting_card'->>'share_id', '')),
       meeting_card_subject_person_id = COALESCE(meeting_card_subject_person_id, NULLIF(body_json->'meeting_card'->>'subject_person_id', ''))
 WHERE body_json IS NOT NULL
   AND body_json <> '{}'::jsonb;

CREATE INDEX IF NOT EXISTS workspace_agent_messages_in_reply_to_idx
    ON core.workspace_agent_messages (workspace_id, agent_conversation_id, in_reply_to_agent_message_id)
    WHERE in_reply_to_agent_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS workspace_agent_messages_entry_surface_idx
    ON core.workspace_agent_messages (workspace_id, sender_person_id, entry_surface, created_at DESC)
    WHERE entry_surface IS NOT NULL;

CREATE INDEX IF NOT EXISTS workspace_agent_messages_background_attempt_idx
    ON core.workspace_agent_messages (workspace_id, background_attempt_id)
    WHERE background_attempt_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS workspace_agent_messages_relayed_message_idx
    ON core.workspace_agent_messages (relayed_message_id)
    WHERE sender_kind = 'agent' AND relayed_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS workspace_agent_messages_relayed_notice_idx
    ON core.workspace_agent_messages (relayed_notice_id)
    WHERE sender_kind = 'agent' AND relayed_notice_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS workspace_agent_messages_meeting_card_subject_idx
    ON core.workspace_agent_messages (workspace_id, agent_conversation_id, meeting_card_subject_person_id)
    WHERE sender_kind = 'agent' AND meeting_card_subject_person_id IS NOT NULL;

SET LOCAL statement_timeout = '30s';
