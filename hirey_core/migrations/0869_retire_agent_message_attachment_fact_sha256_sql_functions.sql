-- ENG-369 (D0.25): Retire core.agent_message_attachment_fact_sha256(core.workspace_agent_message_attachments)
-- and its sole SQL callee core.attachment_digest_field(text) after moving callers
-- to application-layer helpers in hi-platform and hirey_core/batch14_helpers.py.

DROP FUNCTION IF EXISTS core.agent_message_attachment_fact_sha256(core.workspace_agent_message_attachments) RESTRICT;
DROP FUNCTION IF EXISTS core.attachment_digest_field(text) RESTRICT;
