-- The category of a public Need, picked by the model once per wording
-- (D11.15 ②, ENG-344; hirey_core/need_category.py).
--
-- hirey.ai/needs shows public Needs by category. The model picks one id of
-- need_category.CATEGORIES for a public Need; the public read serves it only
-- while the Need still says exactly the words it was picked from
-- (source_summary, source_details), so an edit drops the old category at once
-- and the worker picks again.
--
-- Not core.needs.need_type: need_type is part of content_digest, the key of
-- every stored Need match and Need-people judgement, and rewriting it would
-- hide them. This table touches no column of core.needs.
--
-- category NULL means the pick stored nothing: refusal says why (unusable: the
-- model gave no answer in the list twice). The id list lives in code (it may
-- grow); the check here only keeps an id an id. Deleted with its Need
-- (ON DELETE CASCADE): no Person or Workspace column, so an account merge
-- moves nothing here.
--
-- Additive: older images never read or write it. Rollback: drop the table.

CREATE TABLE core.need_categories (
    need_id text PRIMARY KEY REFERENCES core.needs(need_id) ON DELETE CASCADE,
    category text CHECK (category IS NULL OR (category IS NOT NULL AND category ~ '^[a-z][a-z_]{0,39}$')),
    refusal text CHECK (refusal IS NULL OR (refusal IS NOT NULL AND refusal IN ('unusable'))),
    source_summary text NOT NULL,
    source_details text,
    model text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT need_categories_category_or_refusal CHECK ((category IS NULL) <> (refusal IS NULL))
);

COMMENT ON TABLE core.need_categories IS
 'D11.15 ②: the category the model picked for a public Need, served only while the Need says the exact words it was picked from (source_summary, source_details).';
