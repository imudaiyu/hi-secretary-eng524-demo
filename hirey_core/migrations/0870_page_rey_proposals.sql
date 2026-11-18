-- Rey proposes a change to its owner's own Page; it goes live only after the
-- owner approves that exact text (ENG-520, stage 2; Dong 2026-11-15, design
-- on the issue). hirey_core/page_rey_proposals.py; specs/core-page-rey-proposals.md.
--
-- One row is one proposal: the exact new value of each part it changes
-- (parts, keyed as page.subject.edit_live's lines and parts, ENG-495 / 0853)
-- and the live value of each when it was proposed (base_values, same keys).
-- The owner saw it as one outgoing Rey card in their own Rey conversation
-- (proposal_agent_message_id, a core.workspace_agent_messages agent turn whose
-- body_json.page_rey_proposal seals the same facts); the owner instruction
-- turn it answered is source_agent_message_id.
--
-- There is no window, default or expiry: a proposal is pending until the
-- owner accepts or declines it (decided_by is always the subject), a later
-- proposal supersedes it (superseded_by), or an accept finds every part
-- changed since it was proposed (outdated). An owner has at most one pending
-- proposal (Dong 2026-11-16: the newest replaces the one waiting, so a yes
-- always means the latest; one yes accepts all of that proposal's parts). Nothing applies
-- it by itself; no worker reads this table. Connector suggestions
-- (core.page_suggestions, their windows) are a separate object and unchanged.
--
-- Additive: older images never read or write it. Rollback: drop the table
-- (migrations_restore/0870_page_rey_proposals_restore.sql).
-- Number is provisional: max(main)+1 at merge time (after 0853, ENG-495).

CREATE TABLE core.page_rey_proposals (
    proposal_id text PRIMARY KEY CHECK (proposal_id ~ '^prp_[0-9a-f]{24}$'),
    subject_person_id text NOT NULL REFERENCES core.persons(person_id) ON DELETE RESTRICT,
    workspace_id text NOT NULL REFERENCES core.workspaces(workspace_id) ON DELETE RESTRICT,
    agent_conversation_id text NOT NULL,
    proposal_agent_message_id text NOT NULL UNIQUE,
    source_agent_message_id text NOT NULL,
    proposed_via_agent_id text NOT NULL REFERENCES core.agents(agent_id) ON DELETE RESTRICT,
    parts jsonb NOT NULL CHECK (jsonb_typeof(parts)='object' AND parts<>'{}'::jsonb
        AND parts - ARRAY['display_name','headline','bio_markdown','location_text','website_url',
                          'links','experience','education','knownFor','askAbout','story',
                          'quotes','facts','languages']::text[] = '{}'::jsonb),
    base_values jsonb NOT NULL CHECK (jsonb_typeof(base_values)='object' AND base_values<>'{}'::jsonb),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','accepted','declined','outdated','superseded')),
    decided_at timestamptz,
    decided_by text REFERENCES core.persons(person_id) ON DELETE RESTRICT,
    -- The owner's later message in the same conversation (a Rey turn), or
    -- their own signed-in Client session (the card's button): exactly one for
    -- an owner decision, neither otherwise.
    decided_via_agent_message_id text,
    decided_via_client_session_id text,
    superseded_by text REFERENCES core.page_rey_proposals(proposal_id) ON DELETE RESTRICT,
    applied_parts jsonb,
    -- The provenance of what a research run proposed (ENG-525): its claim map
    -- entries (pages_claims._claim_sources shape) and the source material
    -- ledger entries (draft source_materials shape). Core writes them at seal;
    -- accept carries them into the owner's draft with the accepted parts. A
    -- proposal from a Rey turn has none.
    claim_sources jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(claim_sources)='array'),
    source_materials jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(source_materials)='array'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((status='pending' AND decided_at IS NULL AND decided_by IS NULL)
        OR (status IS NOT NULL AND status IN ('accepted','declined','outdated')
            AND decided_at IS NOT NULL AND decided_by IS NOT NULL AND decided_by=subject_person_id)
        OR (status='superseded' AND decided_at IS NOT NULL AND decided_by IS NULL)),
    CHECK ((status='superseded') = (superseded_by IS NOT NULL)),
    CHECK (superseded_by IS NULL OR superseded_by<>proposal_id),
    CHECK (CASE WHEN status IS NOT NULL AND status IN ('accepted','declined','outdated')
                THEN num_nonnulls(decided_via_agent_message_id,decided_via_client_session_id)=1
                ELSE decided_via_agent_message_id IS NULL AND decided_via_client_session_id IS NULL END),
    CHECK ((status='accepted') = (applied_parts IS NOT NULL)),
    CHECK (applied_parts IS NULL OR (applied_parts IS NOT NULL AND jsonb_typeof(applied_parts)='array'
        AND jsonb_array_length(applied_parts)>0)),
    FOREIGN KEY (workspace_id,subject_person_id)
        REFERENCES core.workspace_memberships(workspace_id,person_id),
    -- One pending proposal per owner. Deferred: a new proposal is inserted
    -- before the one it replaces is marked superseded (superseded_by points at it).
    CONSTRAINT page_rey_proposals_one_pending EXCLUDE USING btree (subject_person_id WITH =)
        WHERE (status='pending') DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX page_rey_proposals_owner_pending_idx
    ON core.page_rey_proposals(subject_person_id,created_at DESC,proposal_id) WHERE status='pending';
CREATE INDEX page_rey_proposals_owner_idx
    ON core.page_rey_proposals(subject_person_id,created_at DESC,proposal_id);
CREATE INDEX page_rey_proposals_superseded_by_idx
    ON core.page_rey_proposals(superseded_by) WHERE superseded_by IS NOT NULL;
REVOKE ALL ON core.page_rey_proposals FROM PUBLIC;

COMMENT ON TABLE core.page_rey_proposals IS
 'ENG-520: a change Rey proposed to its owner''s own Page; applies only on the owner''s accept of this exact text. No window, no default, no expiry.';

DO $$ BEGIN IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='hi_core_runtime') THEN
 -- Written at prepare, decided once; retained history (no DELETE).
 GRANT SELECT,INSERT,UPDATE ON core.page_rey_proposals TO hi_core_runtime;
END IF; END; $$;
