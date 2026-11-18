#!/usr/bin/env python3
"""Tests for scripts/migration_safety_gate.py (ENG-537)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migration_safety_gate.py"
sys.path.insert(0, str(ROOT / "scripts"))
import migration_safety_gate as gate  # noqa: E402


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


class MigrationSafetyGateUnitTests(unittest.TestCase):
    def _empty_index(self) -> dict[str, gate.ConsumerFileIndex]:
        return gate.build_consumer_index({})

    # --- Safe / expansion patterns pass -------------------------------------

    def test_pure_expansion_and_new_table_indexes_pass(self) -> None:
        sql = """
        -- Pure expansion: new table + indexes + backfill + metadata-only column add
        CREATE TABLE core.new_items (
            item_id text PRIMARY KEY,
            kind text NOT NULL
        );
        CREATE INDEX IF NOT EXISTS new_items_kind_idx ON core.new_items (kind);
        UPDATE core.new_items SET kind = 'default';

        CREATE TEMP TABLE _tmp_work ON COMMIT DROP AS SELECT 1 AS id;
        UPDATE _tmp_work SET id = 2;

        ALTER TABLE core.existing_items
            ADD COLUMN IF NOT EXISTS label text,
            ADD COLUMN IF NOT EXISTS score integer CHECK (score >= 0);

        ALTER TABLE core.existing_items
            ALTER COLUMN required_old DROP NOT NULL;

        DROP FUNCTION IF EXISTS core.legacy_helper(text) RESTRICT;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0900_expand.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(violations, [])

    def test_locked_table_allows_subsequent_add_column_only(self) -> None:
        sql = """
        LOCK TABLE core.existing_items IN ACCESS EXCLUSIVE MODE NOWAIT;
        ALTER TABLE core.existing_items ADD COLUMN IF NOT EXISTS flag_a text;
        ALTER TABLE core.existing_items ADD COLUMN IF NOT EXISTS flag_b text;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0901_locked_add_cols.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(violations, [])

    def test_standalone_index_without_lock_passes(self) -> None:
        sql = """
        CREATE INDEX IF NOT EXISTS existing_items_new_col_idx
            ON core.existing_items (new_col)
            WHERE new_col IS NOT NULL;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0902_index_only.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(violations, [])

    def test_comments_literals_and_function_bodies_do_not_trigger(self) -> None:
        sql = """
        -- LOCK TABLE core.existing_items IN ACCESS EXCLUSIVE MODE;
        /* DROP TABLE core.existing_items; /* nested DROP INDEX old_idx; */ */
        ALTER TABLE core.existing_items ADD COLUMN IF NOT EXISTS c text;
        COMMENT ON TABLE core.existing_items IS 'UPDATE core.existing_items; DROP TABLE core.existing_items;';
        CREATE OR REPLACE FUNCTION core.demo_fn() RETURNS void LANGUAGE plpgsql AS $$
        BEGIN
            UPDATE core.existing_items SET c = 'inside_fn';
        END;
        $$;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0903_comments_and_fn.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(violations, [])

    def test_self_created_temp_or_staging_table_drop_passes(self) -> None:
        sql = """
        CREATE TABLE core.temp_stage (id text);
        DROP TABLE core.temp_stage;
        CREATE TEMP TABLE scratch_rows (id text);
        DROP TABLE scratch_rows;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0904_self_drop.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(violations, [])

    # --- Rule 1: Post-lock no-index / no-backfill ----------------------------

    def test_post_lock_create_index_after_alter_table_fails(self) -> None:
        sql = """
        ALTER TABLE core.existing_items ADD COLUMN IF NOT EXISTS tag text;
        CREATE INDEX IF NOT EXISTS existing_items_tag_idx ON core.existing_items (tag);
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0905_bad_idx.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("post-lock CREATE INDEX", violations[0])
        self.assertIn("core.existing_items", violations[0])

    def test_post_lock_update_and_index_inside_do_lock_block_fail(self) -> None:
        sql = """
        DO $$
        BEGIN
            LOCK TABLE core.workspace_agent_messages IN ACCESS EXCLUSIVE MODE NOWAIT;
        END $$;
        ALTER TABLE core.workspace_agent_messages ADD COLUMN IF NOT EXISTS kind text;
        WITH src AS (SELECT 'a' AS k)
        UPDATE core.workspace_agent_messages SET kind = src.k FROM src;
        CREATE UNIQUE INDEX msg_kind_idx ON core.workspace_agent_messages (kind);
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0906_bad_do_lock.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(len(violations), 2)
        self.assertIn("post-lock UPDATE backfill", violations[0])
        self.assertIn("post-lock CREATE INDEX", violations[1])

    def test_multi_table_lock_tracks_all_locked_tables(self) -> None:
        sql = """
        LOCK TABLE core.table_a, ONLY core.table_b IN SHARE ROW EXCLUSIVE MODE NOWAIT;
        CREATE INDEX idx_b ON core.table_b (id);
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0907_multi_lock.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("core.table_b", violations[0])

    def test_data_modifying_cte_update_backfill_fails_under_lock(self) -> None:
        sql = """
        LOCK TABLE core.items IN ACCESS EXCLUSIVE MODE;
        WITH changed AS (
            UPDATE core.items SET label = chr(120) RETURNING id
        )
        SELECT count(*) FROM changed;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0908_wcte_backfill.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("post-lock UPDATE backfill", violations[0])
        self.assertIn("core.items", violations[0])

    def test_recursive_cte_search_clause_before_update_blocked(self) -> None:
        sql = """
        LOCK TABLE core.items;
        WITH RECURSIVE walk(id) AS (
            SELECT 1
            UNION ALL SELECT id + 1 FROM walk WHERE id < 2
        )
        SEARCH DEPTH FIRST BY id SET ordercol
        UPDATE core.items SET label = chr(120);
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0908b_rcte_search.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("post-lock UPDATE backfill", violations[0])
        self.assertIn("core.items", violations[0])

    def test_do_block_execute_static_and_dynamic_sql_checked(self) -> None:
        bad_exec_update = """
        DO $$
        BEGIN
            EXECUTE 'LOCK TABLE core.items IN ACCESS EXCLUSIVE MODE';
            EXECUTE 'UPDATE core.items SET label = chr(120)';
        END $$;
        """
        v_update = gate.evaluate_migrations(
            [("hirey_core/migrations/0909_exec_update.sql", bad_exec_update)],
            self._empty_index(),
        )
        self.assertEqual(len(v_update), 1)
        self.assertIn("post-lock UPDATE backfill", v_update[0])

        bad_exec_drop = """
        DO $$
        BEGIN
            EXECUTE 'DROP TABLE core.legacy_items';
        END $$;
        """
        v_drop = gate.evaluate_migrations(
            [("hirey_core/migrations/0909b_exec_drop.sql", bad_exec_drop)],
            self._empty_index(),
        )
        self.assertEqual(len(v_drop), 1)
        self.assertIn("Contraction-Of:", v_drop[0])

        bad_dynamic = """
        DO $$
        BEGIN
            EXECUTE format('UPDATE %I SET x = 1', 'core.items');
        END $$;
        """
        v_dyn = gate.evaluate_migrations(
            [("hirey_core/migrations/0909c_exec_dyn.sql", bad_dynamic)],
            self._empty_index(),
        )
        self.assertEqual(len(v_dyn), 1)
        self.assertIn("dynamic EXECUTE", v_dyn[0])

        ok_static_select = """
        DO $$
        BEGIN
            EXECUTE 'SELECT 1';
        END $$;
        """
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0909d_exec_ok.sql", ok_static_select)],
                self._empty_index(),
            ),
            [],
        )

    def test_create_table_if_not_exists_and_replace_view_require_absent_prior_object(self) -> None:
        table_bypass = """
        CREATE TABLE IF NOT EXISTS core.items (id text);
        ALTER TABLE core.items ADD COLUMN label text;
        UPDATE core.items SET label = chr(120);
        CREATE INDEX items_label_idx ON core.items(label);
        """
        # Blocked when schema history is unknown or table already exists.
        v_unknown = gate.evaluate_migrations(
            [("hirey_core/migrations/0909e_ifne.sql", table_bypass)],
            self._empty_index(),
        )
        self.assertEqual(len(v_unknown), 2)

        prior = [("hirey_core/migrations/0001_init.sql", "CREATE TABLE core.items (id text); CREATE VIEW core.v AS SELECT 1;")]
        v_existing = gate.evaluate_migrations(
            [("hirey_core/migrations/0909e_ifne.sql", table_bypass)],
            self._empty_index(),
            prior_migrations=prior,
        )
        self.assertEqual(len(v_existing), 2)

        # Passes when proven genuinely new by prior_migrations.
        genuine_new = """
        CREATE TABLE IF NOT EXISTS core.brand_new (id text PRIMARY KEY);
        CREATE INDEX IF NOT EXISTS brand_new_id_idx ON core.brand_new (id);
        """
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0909f_new.sql", genuine_new)],
                self._empty_index(),
                prior_migrations=prior,
            ),
            [],
        )

        view_bypass = """
        CREATE OR REPLACE VIEW core.v AS SELECT 1;
        ALTER VIEW core.v RENAME TO v2;
        """
        v_view_unknown = gate.evaluate_migrations(
            [("hirey_core/migrations/0909g_view.sql", view_bypass)],
            self._empty_index(),
        )
        self.assertEqual(len(v_view_unknown), 1)
        self.assertIn("Contraction-Of:", v_view_unknown[0])

        v_view_existing = gate.evaluate_migrations(
            [("hirey_core/migrations/0909g_view.sql", view_bypass)],
            self._empty_index(),
            prior_migrations=prior,
        )
        self.assertEqual(len(v_view_existing), 1)
        self.assertIn("Contraction-Of:", v_view_existing[0])

    def test_table_rename_carries_lock_set_schema_tracked_and_escape_execute_decoded(self) -> None:
        # 1. ALTER TABLE ... RENAME TO carries table lock to the new name.
        rename_lock_sql = """
        -- Contraction-Of: rename items
        -- Consumers-Cleared: verified
        ALTER TABLE core.items RENAME TO items2;
        UPDATE core.items2 SET label = chr(120);
        CREATE INDEX items2_label ON core.items2(label);
        """
        v_rename = gate.evaluate_migrations(
            [("hirey_core/migrations/0909h_rename_lock.sql", rename_lock_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_rename), 2)
        self.assertIn("post-lock UPDATE backfill", v_rename[0])
        self.assertIn("post-lock CREATE INDEX", v_rename[1])

        # 2. Prior ALTER TABLE ... SET SCHEMA keeps destination table marked as existing.
        prior_moved = [(
            "hirey_core/migrations/0001_init.sql",
            "CREATE TABLE core.items (id text);\nALTER TABLE core.items SET SCHEMA archive;\n",
        )]
        moved_bypass = """
        CREATE TABLE IF NOT EXISTS archive.items (id text);
        ALTER TABLE archive.items ADD COLUMN label text;
        UPDATE archive.items SET label = chr(120);
        CREATE INDEX items_label ON archive.items(label);
        """
        v_moved = gate.evaluate_migrations(
            [("hirey_core/migrations/0909i_set_schema.sql", moved_bypass)],
            self._empty_index(),
            prior_migrations=prior_moved,
        )
        self.assertEqual(len(v_moved), 2)

        # 3. E'...' hex/octal/unicode escapes in static EXECUTE are decoded accurately.
        escape_sql = r"""
        LOCK TABLE core.items;
        DO $$ BEGIN
            EXECUTE E'\x55PDATE core.items SET label = chr(120)';
            EXECUTE E'\104ROP TABLE core.items';
        END $$;
        """
        v_esc = gate.evaluate_migrations(
            [("hirey_core/migrations/0909j_escape.sql", escape_sql)],
            self._empty_index(),
        )
        self.assertTrue(any("post-lock UPDATE backfill" in m for m in v_esc), v_esc)
        self.assertTrue(any("Contraction-Of:" in m for m in v_esc), v_esc)

    def test_rename_vacated_name_unicode_identifiers_and_byte_octal_escapes(self) -> None:
        prior_items = [(
            "hirey_core/migrations/0001_init.sql",
            "CREATE TABLE core.items (id text, label text);\n",
        )]

        # 1. Renaming a newly created table vacates its old name; reusing it for an existing table is tracked.
        vacate_reuse_sql = """
        -- Contraction-Of: rename items to staging
        -- Consumers-Cleared: verified
        CREATE TABLE core.staging (id text);
        ALTER TABLE core.staging RENAME TO staging2;
        ALTER TABLE core.items RENAME TO staging;
        UPDATE core.staging SET id = chr(120);
        CREATE INDEX staging_id ON core.staging(id);
        """
        v_vacate = gate.evaluate_migrations(
            [("hirey_core/migrations/0909k_vacate_reuse.sql", vacate_reuse_sql)],
            self._empty_index(),
            prior_migrations=prior_items,
        )
        self.assertEqual(len(v_vacate), 2, v_vacate)
        self.assertIn("post-lock UPDATE backfill", v_vacate[0])
        self.assertIn("post-lock CREATE INDEX", v_vacate[1])

        # 2. CREATE TABLE IF NOT EXISTS after renaming an existing table does not grant new-table exemption.
        rename_if_not_exists_sql = """
        -- Contraction-Of: rename items to staging
        -- Consumers-Cleared: verified
        ALTER TABLE core.items RENAME TO staging;
        CREATE TABLE IF NOT EXISTS core.staging (id text);
        UPDATE core.staging SET label = chr(120);
        CREATE INDEX staging_label ON core.staging(label);
        """
        v_ine = gate.evaluate_migrations(
            [("hirey_core/migrations/0909l_rename_if_not_exists.sql", rename_if_not_exists_sql)],
            self._empty_index(),
            prior_migrations=prior_items,
        )
        self.assertEqual(len(v_ine), 2, v_ine)

        # 3. Unicode-escaped identifiers (with and without UESCAPE) match locked table keys.
        uident_sql = r"""
        LOCK TABLE core.items;
        UPDATE core.U&"it\0065ms" SET label = chr(120);
        CREATE INDEX items_label ON core.U&"it!0065ms" UESCAPE '!' (label);
        """
        v_uid = gate.evaluate_migrations(
            [("hirey_core/migrations/0909m_uident.sql", uident_sql)],
            self._empty_index(),
            prior_migrations=prior_items,
        )
        self.assertEqual(len(v_uid), 2, v_uid)
        self.assertIn("post-lock UPDATE backfill", v_uid[0])
        self.assertIn("post-lock CREATE INDEX", v_uid[1])

        # 4. PostgreSQL octal byte truncation (\525 -> 0x55 = 'U') and U&'...' UESCAPE inside EXECUTE.
        octal_wrap_sql = r"""
        LOCK TABLE core.items;
        DO $$ BEGIN
            EXECUTE E'\525PDATE core.items SET label = chr(120)';
            EXECUTE U&'!0043REATE INDEX items_lbl ON core.items(label)' UESCAPE '!';
        END $$;
        """
        v_oct = gate.evaluate_migrations(
            [("hirey_core/migrations/0909n_octal_wrap.sql", octal_wrap_sql)],
            self._empty_index(),
            prior_migrations=prior_items,
        )
        self.assertEqual(len(v_oct), 2, v_oct)
        self.assertIn("post-lock UPDATE backfill", v_oct[0])
        self.assertIn("post-lock CREATE INDEX", v_oct[1])

        # 5. Invalid UTF-8 bytes or invalid Unicode escapes fail closed instead of silently decoding.
        invalid_escape_sql = r"""
        DO $$ BEGIN
            EXECUTE E'\x80PDATE core.items SET label = chr(120)';
        END $$;
        UPDATE core.U&"bad\xyz" SET label = chr(120);
        """
        v_bad = gate.evaluate_migrations(
            [("hirey_core/migrations/0909o_bad_escape.sql", invalid_escape_sql)],
            self._empty_index(),
            prior_migrations=prior_items,
        )
        self.assertEqual(len(v_bad), 2, v_bad)
        self.assertIn("dynamic EXECUTE", v_bad[0])
        self.assertIn("invalid Unicode escape", v_bad[1])

        # 6. Mixed-case quoted identifier stays distinct from lowercase unquoted table name.
        quoted_distinct_sql = """
        CREATE TABLE core."Items" (id text, label text);
        ALTER TABLE core."Items" ADD COLUMN extra text;
        UPDATE core."Items" SET extra = chr(120);
        CREATE INDEX items_mixed_extra ON core."Items"(extra);
        """
        v_quoted = gate.evaluate_migrations(
            [("hirey_core/migrations/0909p_quoted_case.sql", quoted_distinct_sql)],
            self._empty_index(),
            prior_migrations=prior_items,
        )
        self.assertEqual(v_quoted, [], v_quoted)

    def test_parenthesized_only_targets_and_renamed_existing_view_tracking(self) -> None:
        prior = [(
            "hirey_core/migrations/0001_init.sql",
            (
                "CREATE TABLE core.items (id text, label text);\n"
                "CREATE VIEW core.v AS SELECT id FROM core.items;\n"
                "CREATE INDEX idx_items ON core.items(id);\n"
            ),
        )]

        # 1. Parenthesized ONLY (relation) in UPDATE, ALTER TABLE, and LOCK TABLE is tracked.
        only_update_sql = """
        LOCK TABLE ONLY (core.items);
        UPDATE ONLY (core.items) SET label = chr(120);
        """
        v_only_upd = gate.evaluate_migrations(
            [("hirey_core/migrations/0909q_only_upd.sql", only_update_sql)],
            self._empty_index(),
            prior_migrations=prior,
        )
        self.assertEqual(len(v_only_upd), 1, v_only_upd)
        self.assertIn("post-lock UPDATE backfill", v_only_upd[0])

        only_alter_sql = """
        ALTER TABLE ONLY (core.items) ADD COLUMN extra text;
        CREATE INDEX j_items_label ON ONLY (core.items)(label);
        """
        v_only_alt = gate.evaluate_migrations(
            [("hirey_core/migrations/0909r_only_alt.sql", only_alter_sql)],
            self._empty_index(),
            prior_migrations=prior,
        )
        self.assertEqual(len(v_only_alt), 1, v_only_alt)
        self.assertIn("post-lock CREATE INDEX", v_only_alt[0])

        # 2. Existing view renamed or schema-moved stays tracked across CREATE OR REPLACE VIEW.
        index_vr = gate.build_consumer_index({
            "hirey_core/readers.py": 'Q = "SELECT id FROM core.v2"\n',
        })
        view_rename_bypass = """
        -- Contraction-Of: rename view v
        -- Consumers-Cleared: verified v
        ALTER VIEW core.v RENAME TO v2;
        CREATE OR REPLACE VIEW core.v2 AS SELECT 1 AS id;
        DROP VIEW core.v2;
        """
        v_vr = gate.evaluate_migrations(
            [("hirey_core/migrations/0909s_view_rename.sql", view_rename_bypass)],
            index_vr,
            prior_migrations=prior,
        )
        self.assertEqual(len(v_vr), 1, v_vr)
        self.assertIn("DROP VIEW core.v2", v_vr[0])
        self.assertIn("unresolved runtime reference", v_vr[0])

        index_vm = gate.build_consumer_index({
            "hirey_core/readers.py": 'Q = "SELECT id FROM archive.v"\n',
        })
        view_move_bypass = """
        -- Contraction-Of: move view v
        -- Consumers-Cleared: verified v
        ALTER VIEW core.v SET SCHEMA archive;
        CREATE OR REPLACE VIEW archive.v AS SELECT 1 AS id;
        DROP VIEW archive.v;
        """
        v_vm = gate.evaluate_migrations(
            [("hirey_core/migrations/0909t_view_move.sql", view_move_bypass)],
            index_vm,
            prior_migrations=prior,
        )
        self.assertTrue(any("DROP VIEW archive.v" in m and "unresolved runtime reference" in m for m in v_vm), v_vm)

        # 3. Existing index renamed stays tracked across CREATE INDEX IF NOT EXISTS + DROP INDEX.
        idx_index = gate.build_consumer_index({
            "hirey_core/queries.sql": "--+ idx_items_v2\nSELECT * FROM core.items;\n",
        })
        idx_rename_bypass = """
        -- Contraction-Of: rename index
        -- Consumers-Cleared: verified old index
        ALTER INDEX core.idx_items RENAME TO idx_items_v2;
        CREATE INDEX IF NOT EXISTS idx_items_v2 ON core.items(id);
        DROP INDEX core.idx_items_v2;
        """
        v_idx = gate.evaluate_migrations(
            [("hirey_core/migrations/0909u_idx_rename.sql", idx_rename_bypass)],
            idx_index,
            prior_migrations=prior,
        )
        self.assertEqual(len(v_idx), 1, v_idx)
        self.assertIn("unresolved runtime reference", v_idx[0])

    def test_plpgsql_case_exception_foreach_and_unverified_fail_closed(self) -> None:
        # 1. LOCK TABLE + DO $$ CASE WHEN ... THEN UPDATE ... END CASE $$ triggers post-lock violation.
        case_update_sql = """
        LOCK TABLE core.items;
        DO $$ BEGIN
            CASE WHEN true THEN
                UPDATE core.items SET id = 1;
            END CASE;
        END $$;
        """
        v_case_upd = gate.evaluate_migrations(
            [("hirey_core/migrations/0909v_case_update.sql", case_update_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_case_upd), 1, v_case_upd)
        self.assertIn("post-lock UPDATE backfill", v_case_upd[0])

        # 2. DO $$ CASE WHEN ... THEN DROP TABLE ... END CASE $$ triggers contraction gate.
        case_drop_sql = """
        DO $$ BEGIN
            CASE WHEN true THEN
                DROP TABLE core.items;
            END CASE;
        END $$;
        """
        v_case_drop = gate.evaluate_migrations(
            [("hirey_core/migrations/0909w_case_drop.sql", case_drop_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_case_drop), 1, v_case_drop)
        self.assertIn("Contraction-Of:", v_case_drop[0])

        # 3. Inner SQL CASE...END condition inside CASE/IF, subsequent WHEN in CASE/EXCEPTION, and FOREACH.
        complex_plpgsql_sql = """
        DO $$
        DECLARE
            x int := 1;
            r text;
        BEGIN
            CASE WHEN CASE WHEN true THEN true ELSE false END THEN
                LOCK TABLE core.items;
            WHEN false THEN
                PERFORM 1;
            END CASE;
            BEGIN
                PERFORM 1;
            EXCEPTION
                WHEN unique_violation THEN
                    PERFORM 2;
                WHEN others THEN
                    UPDATE core.items SET label = chr(120);
            END;
            FOREACH r IN ARRAY ARRAY['a'] LOOP
                CREATE INDEX items_r_idx ON core.items(label);
            END LOOP;
        END $$;
        """
        v_complex = gate.evaluate_migrations(
            [("hirey_core/migrations/0909x_complex_plpgsql.sql", complex_plpgsql_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_complex), 2, v_complex)
        self.assertIn("post-lock UPDATE backfill", v_complex[0])
        self.assertIn("post-lock CREATE INDEX", v_complex[1])

        # 4. Unrecognized executable PL/pgSQL construct inside DO fails closed explicitly.
        unknown_plpgsql_sql = """
        DO $$ BEGIN
            UNSUPPORTED_BLOCK_HEADER UPDATE core.items SET id = 1;
        END $$;
        """
        v_unknown = gate.evaluate_migrations(
            [("hirey_core/migrations/0909y_unknown_plpgsql.sql", unknown_plpgsql_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_unknown), 1, v_unknown)
        self.assertIn("unverified PL/pgSQL construct", v_unknown[0])

        # 5. FOR ... IN <UPDATE / WITH / EXECUTE> LOOP and OPEN ... FOR EXECUTE are audited.
        for_query_sql = """
        LOCK TABLE core.items;
        DO $$
        DECLARE
            r record;
            c refcursor;
        BEGIN
            FOR r IN
                UPDATE core.items SET id = 1 RETURNING *
            LOOP
                NULL;
            END LOOP;
            FOR r IN (WITH moved AS (UPDATE core.items SET label = chr(120) RETURNING id) SELECT * FROM moved) LOOP
                NULL;
            END LOOP;
            FOR r IN EXECUTE 'UPDATE core.items SET extra = 1 RETURNING *' LOOP
                NULL;
            END LOOP;
            OPEN c FOR EXECUTE 'UPDATE core.items SET other = 2 RETURNING *';
            FOR r IN EXECUTE format('UPDATE %I SET x = 1', 'core.items') LOOP
                NULL;
            END LOOP;
        END $$;
        """
        v_for = gate.evaluate_migrations(
            [("hirey_core/migrations/0909z_for_query.sql", for_query_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_for), 5, v_for)
        for idx_v in range(4):
            self.assertIn("post-lock UPDATE backfill", v_for[idx_v])
        self.assertIn("dynamic EXECUTE", v_for[4])

        benign_for_sql = """
        LOCK TABLE core.items;
        DO $$
        DECLARE
            r record;
            c refcursor;
        BEGIN
            FOR r IN SELECT CASE WHEN true THEN 1 ELSE 2 END FROM pg_class LIMIT 1 LOOP
                NULL;
            END LOOP;
            OPEN c FOR SELECT 1;
            CLOSE c;
        END $$;
        """
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0909za_for_select_ok.sql", benign_for_sql)],
                self._empty_index(),
            ),
            [],
        )

    def test_prepared_statements_copy_and_search_path_tracking(self) -> None:
        # 1. PREPARE + EXECUTE under lock is caught (including parameterized PREPARE).
        prep_sql = """
        PREPARE p_before (int) AS UPDATE core.items SET x = $1;
        LOCK TABLE core.items;
        PREPARE backfill AS UPDATE core.items SET x = 1;
        EXECUTE backfill;
        EXECUTE p_before(2);
        DEALLOCATE backfill;
        """
        v_prep = gate.evaluate_migrations(
            [("hirey_core/migrations/0909zb_prepare.sql", prep_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_prep), 2, v_prep)
        self.assertIn("post-lock UPDATE backfill", v_prep[0])
        self.assertIn("post-lock UPDATE backfill", v_prep[1])

        # 2. Executing after DEALLOCATE or unknown prepared statement fails closed.
        unknown_exec_sql = """
        PREPARE p AS SELECT 1;
        DEALLOCATE p;
        EXECUTE p;
        """
        v_unk = gate.evaluate_migrations(
            [("hirey_core/migrations/0909zc_exec_unknown.sql", unknown_exec_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_unk), 1, v_unk)
        self.assertIn("prepared statement", v_unk[0])

        # 3. COPY (UPDATE ... RETURNING *) under lock is caught.
        copy_sql = """
        LOCK TABLE core.items;
        COPY (UPDATE core.items SET x = 1 RETURNING *) TO STDOUT;
        """
        v_copy = gate.evaluate_migrations(
            [("hirey_core/migrations/0909zd_copy_update.sql", copy_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_copy), 1, v_copy)
        self.assertIn("post-lock UPDATE backfill", v_copy[0])

        # 4. Single-schema SET LOCAL search_path & set_config resolve unqualified objects accurately.
        sp_set_sql = """
        SET LOCAL search_path = archive;
        LOCK TABLE archive.items;
        UPDATE items SET x = 1;
        """
        v_sp = gate.evaluate_migrations(
            [("hirey_core/migrations/0909ze_search_path.sql", sp_set_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_sp), 1, v_sp)
        self.assertIn("'archive.items'", v_sp[0])

        sp_cfg_sql = """
        SELECT set_config('search_path', 'archive', true);
        LOCK TABLE archive.items;
        CREATE INDEX items_x_idx ON items(x);
        """
        v_cfg = gate.evaluate_migrations(
            [("hirey_core/migrations/0909zf_set_config_sp.sql", sp_cfg_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_cfg), 1, v_cfg)
        self.assertIn("post-lock CREATE INDEX", v_cfg[0])
        self.assertIn("'archive.items'", v_cfg[0])

        # 5. Ambiguous multi-schema search_path fails closed on unqualified relation references.
        sp_ambig_sql = """
        SET LOCAL search_path = archive, core;
        LOCK TABLE archive.items;
        UPDATE items SET x = 1;
        """
        v_ambig = gate.evaluate_migrations(
            [("hirey_core/migrations/0909zg_ambig_sp.sql", sp_ambig_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_ambig), 1, v_ambig)
        self.assertIn("ambiguous or empty search_path", v_ambig[0])

        # 6. Resetting search_path TO DEFAULT resolves subsequent unqualified refs back to core.
        sp_reset_sql = """
        SET LOCAL search_path = archive;
        LOCK TABLE archive.items;
        SET LOCAL search_path TO DEFAULT;
        UPDATE items SET x = 1;
        """
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0909zh_reset_sp.sql", sp_reset_sql)],
                self._empty_index(),
            ),
            [],
        )

        # 7. Quoted setting/function identifiers, "$user", embedded/conditional set_config, tx rollback/savepoints, and malformed PREPARE/EXECUTE fail closed.
        for label, sql, expected_substr in [
            (
                "quoted_set",
                'SET "search_path" TO archive;\nLOCK TABLE archive.people;\nUPDATE people SET value=1;\n',
                "'archive.people'",
            ),
            (
                "quoted_set_config",
                'SELECT pg_catalog."set_config"(\'search_path\',\'archive\',true);\nLOCK TABLE archive.people;\nUPDATE people SET value=1;\n',
                "'archive.people'",
            ),
            (
                "dollar_user_ambiguous",
                'SET search_path TO "$user", core;\nLOCK TABLE alice.people;\nUPDATE people SET value=1;\n',
                "ambiguous or empty search_path",
            ),
            (
                "multi_set_config",
                "SELECT set_config('search_path','core',true), set_config('search_path','archive',true);\nLOCK TABLE archive.people;\nUPDATE people SET value=1;\n",
                "multi-call set_config",
            ),
            (
                "embedded_set_config_in_update",
                "LOCK TABLE core.people;\nUPDATE people SET value=length(set_config('search_path','archive',true));\n",
                "set_config('search_path'",
            ),
            (
                "conditional_set_config_where",
                "LOCK TABLE core.people;\nSELECT set_config('search_path','archive',true) WHERE false;\nUPDATE people SET value=1;\n",
                "set_config('search_path'",
            ),
            (
                "conditional_set_search_path_in_do",
                "DO $$ BEGIN IF false THEN SET LOCAL search_path = archive; END IF; END $$;\n",
                "conditional search_path",
            ),
            (
                "conditional_multi_stmt_if_in_do",
                "LOCK TABLE core.people;\nDO $$ BEGIN IF false THEN NULL; SET LOCAL search_path = archive; END IF; END $$;\nUPDATE people SET value = 1;\n",
                "conditional search_path",
            ),
            (
                "plpgsql_assignment_set_config",
                "LOCK TABLE archive.people;\nDO $$ DECLARE p text; BEGIN p := set_config('search_path','archive',true); END $$;\nUPDATE people SET value = 1;\n",
                "set_config('search_path'",
            ),
            (
                "plpgsql_quoted_assignment_set_config",
                "DO $$ BEGIN \"p\" := set_config('search_path','archive',true); END $$;\n",
                "set_config('search_path'",
            ),
            (
                "plpgsql_header_cond_set_config",
                "DO $$ BEGIN IF set_config('search_path','archive',true) = 'archive' THEN NULL; END IF; END $$;\n",
                "search_path mutation",
            ),
            (
                "plpgsql_declare_first_set_config",
                "LOCK TABLE archive.people;\nDO $$ DECLARE p text := set_config('search_path','archive',true); BEGIN NULL; END $$;\nUPDATE people SET x = 1;\n",
                "DO declaration",
            ),
            (
                "plpgsql_declare_second_set_config",
                "LOCK TABLE archive.people;\nDO $$ DECLARE a int := 1; p text := set_config('search_path','archive',true); BEGIN NULL; END $$;\nUPDATE people SET x = 1;\n",
                "DO declaration",
            ),
            (
                "candidate_rollback_blocked",
                "BEGIN;\nSET search_path=archive;\nROLLBACK;\nBEGIN;\nLOCK TABLE core.people;\nUPDATE people SET value=1;\n",
                "ROLLBACK inside candidate migrations",
            ),
            (
                "candidate_savepoint_blocked",
                "BEGIN;\nSET search_path=archive;\nSAVEPOINT s;\nSET search_path=core;\nROLLBACK WORK TO SAVEPOINT s;\nLOCK TABLE archive.people;\nUPDATE people SET value=1;\n",
                "SAVEPOINT",
            ),
            (
                "malformed_prepare_header",
                "PREPARE p nonsense AS SELECT 1;\n",
                "malformed PREPARE",
            ),
            (
                "malformed_prepare_body",
                "PREPARE p AS nonsense;\n",
                "malformed PREPARE",
            ),
            (
                "malformed_prepare_incomplete_update",
                "PREPARE p AS UPDATE;\n",
                "malformed PREPARE",
            ),
            (
                "malformed_execute_parens",
                "PREPARE p AS SELECT 1;\nEXECUTE p(;\n",
                "prepared statement",
            ),
            (
                "malformed_execute_empty_comma",
                "PREPARE p AS SELECT 1;\nEXECUTE p(,);\n",
                "prepared statement",
            ),
        ]:
            with self.subTest(case=label):
                v = gate.evaluate_migrations(
                    [(f"hirey_core/migrations/0909zi_{label}.sql", sql)],
                    self._empty_index(),
                )
                self.assertTrue(any(expected_substr in m for m in v), v)

        # 8. Historical migrations with ROLLBACK / nested duplicate SAVEPOINT / RELEASE / WORK TO preserve live inventory.
        prior_tx = [
            ("hirey_core/migrations/0001_init.sql", "CREATE TABLE core.people(value int);\n"),
            (
                "hirey_core/migrations/0002_rolled_back.sql",
                "BEGIN;\nDROP TABLE core.people;\nROLLBACK;\n",
            ),
            (
                "hirey_core/migrations/0003_savepoint_stack.sql",
                (
                    "BEGIN;\n"
                    "SAVEPOINT s;\n"
                    "SET search_path=archive;\n"
                    "SAVEPOINT s;\n"
                    "SET search_path=core;\n"
                    "ROLLBACK WORK TO SAVEPOINT s;\n"
                    "RELEASE SAVEPOINT s;\n"
                    "ROLLBACK TO s;\n"
                    "CREATE TABLE kept_after_outer_rollback(id int);\n"
                    "COMMIT;\n"
                ),
            ),
        ]
        candidate_after_tx = """
        CREATE TABLE IF NOT EXISTS core.people(value int);
        LOCK TABLE core.people;
        UPDATE core.people SET value = 1;
        CREATE TABLE IF NOT EXISTS core.kept_after_outer_rollback(id int);
        LOCK TABLE core.kept_after_outer_rollback;
        UPDATE core.kept_after_outer_rollback SET id = 2;
        """
        v_hist_tx = gate.evaluate_migrations(
            [("hirey_core/migrations/0909zj_hist_tx.sql", candidate_after_tx)],
            self._empty_index(),
            prior_migrations=prior_tx,
        )
        self.assertEqual(len(v_hist_tx), 2, v_hist_tx)
        self.assertIn("'core.people'", v_hist_tx[0])
        self.assertIn("'core.kept_after_outer_rollback'", v_hist_tx[1])

        # 9. Sequential unconditional SET LOCAL after END IF still resolves subsequent table locks.
        after_if_sql = """
        DO $$ BEGIN
            IF false THEN
                NULL;
            END IF;
            SET LOCAL search_path = archive;
        END $$;
        LOCK TABLE archive.people;
        UPDATE people SET value = 1;
        """
        v_after_if = gate.evaluate_migrations(
            [("hirey_core/migrations/0909zk_after_if.sql", after_if_sql)],
            self._empty_index(),
        )
        self.assertEqual(len(v_after_if), 1, v_after_if)
        self.assertIn("'archive.people'", v_after_if[0])

    # --- Rule 2: Contraction migration gate (L2a) ----------------------------

    def test_contraction_variants_blocked_without_annotations(self) -> None:
        cases = [
            ("drop_table", "DROP TABLE core.legacy_items;"),
            ("drop_view", "DROP VIEW IF EXISTS core.legacy_view;"),
            ("drop_mview", "DROP MATERIALIZED VIEW core.legacy_mv;"),
            ("drop_index", "DROP INDEX IF EXISTS core.legacy_idx;"),
            ("drop_col", "ALTER TABLE core.items DROP COLUMN old_col;"),
            ("drop_col_short", "ALTER TABLE core.items DROP old_col;"),
            ("drop_constraint", "ALTER TABLE core.items DROP CONSTRAINT items_old_chk;"),
            ("rename_table", "ALTER TABLE core.items RENAME TO items_v2;"),
            ("rename_col", "ALTER TABLE core.items RENAME COLUMN old_col TO new_col;"),
            ("rename_constraint", "ALTER TABLE core.items RENAME CONSTRAINT c_old TO c_new;"),
            ("rename_index", "ALTER INDEX core.old_idx RENAME TO new_idx;"),
            ("rename_view", "ALTER VIEW core.old_view RENAME TO new_view;"),
            ("rename_mview", "ALTER MATERIALIZED VIEW core.old_mv RENAME TO new_mv;"),
            ("rename_view_col", "ALTER VIEW core.old_view RENAME COLUMN old_col TO new_col;"),
            ("set_not_null", "ALTER TABLE core.items ALTER COLUMN old_col SET NOT NULL;"),
            ("alter_type", "ALTER TABLE core.items ALTER COLUMN old_col TYPE bigint;"),
            ("set_data_type", "ALTER TABLE core.items ALTER COLUMN old_col SET DATA TYPE bigint;"),
            ("add_check", "ALTER TABLE core.items ADD CONSTRAINT items_chk CHECK (old_col <> '');"),
            ("add_unique", "ALTER TABLE core.items ADD CONSTRAINT items_uk UNIQUE (old_col);"),
            (
                "add_fk",
                "ALTER TABLE core.items ADD CONSTRAINT items_fk FOREIGN KEY (other_id) REFERENCES core.others(id);",
            ),
        ]
        for label, sql in cases:
            with self.subTest(case=label):
                violations = gate.evaluate_migrations(
                    [(f"hirey_core/migrations/0910_{label}.sql", sql)],
                    self._empty_index(),
                )
                self.assertEqual(len(violations), 1, violations)
                self.assertIn("Contraction-Of:", violations[0])
                self.assertIn("Consumers-Cleared:", violations[0])

    def test_bare_placeholders_do_not_satisfy_contraction_headers(self) -> None:
        sql = """
        -- Contraction-Of: TBD
        -- Consumers-Cleared: N/A
        DROP TABLE core.legacy_items;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0911_placeholder.sql", sql)],
            self._empty_index(),
        )
        self.assertEqual(len(violations), 1)

        # Markdown bold/emphasis/code wrappers must not bypass placeholder checks.
        formatted_bodies = [
            "- **Contraction-Of:** TBD\n- **Consumers-Cleared:** ** N/A **\n",
            "**Contraction-Of:** todo\n**Consumers-Cleared:** `TBD`\n",
            "* **Contraction-Of**: _none_\n* **Consumers-Cleared**: ~~pending~~\n",
        ]
        for body in formatted_bodies:
            with self.subTest(body=body):
                v = gate.evaluate_migrations(
                    [("hirey_core/migrations/0911b_fmt.sql", "DROP TABLE core.legacy_items;")],
                    self._empty_index(),
                    pr_body=body,
                )
                self.assertEqual(len(v), 1, v)

    def test_in_place_constraints_and_type_changes_allow_active_column_readers(self) -> None:
        index = gate.build_consumer_index({
            "hirey_core/messages.py": (
                'QUERY = "SELECT sender_kind, body_json FROM core.workspace_agent_messages"\n'
            ),
        })
        sqls = [
            (
                "set_not_null",
                "ALTER TABLE core.workspace_agent_messages ALTER COLUMN sender_kind SET NOT NULL;",
            ),
            (
                "alter_type",
                "ALTER TABLE core.workspace_agent_messages ALTER COLUMN body_json TYPE jsonb;",
            ),
            (
                "add_check",
                "ALTER TABLE core.workspace_agent_messages "
                "ADD CONSTRAINT msg_sender_kind_chk CHECK (sender_kind <> '');",
            ),
        ]
        pr_body = (
            "- **Contraction-Of:** ENG-537 tighten live column invariant after backfill\n"
            "- **Consumers-Cleared:** verified all existing rows/writers satisfy constraint\n"
        )
        for label, sql in sqls:
            with self.subTest(case=label):
                # Still blocked without Contraction-Of / Consumers-Cleared declarations.
                unannotated = gate.evaluate_migrations(
                    [(f"hirey_core/migrations/0912_{label}.sql", sql)],
                    index,
                )
                self.assertEqual(len(unannotated), 1, unannotated)
                self.assertIn("Contraction-Of:", unannotated[0])

                # Passes once declared even though runtime code legitimately queries those columns.
                annotated = gate.evaluate_migrations(
                    [(f"hirey_core/migrations/0912_{label}.sql", sql)],
                    index,
                    pr_body=pr_body,
                )
                self.assertEqual(annotated, [], annotated)

    def test_uppercase_sql_consumers_and_malformed_indentation_fallback(self) -> None:
        index = gate.build_consumer_index({
            "hirey_core/upper_reader.py": (
                'def read_upper():\n'
                '    return "SELECT SENDER_KIND FROM CORE.WORKSPACE_AGENT_MESSAGES"\n'
            ),
            "hirey_core/bad_indent.py": (
                "def broken():\n"
                "  x = 1\n"
                "   y = 'SELECT legacy_upper_col FROM core.items'\n"
            ),
        })
        sql_upper = (
            "-- Contraction-Of: drop sender_kind\n"
            "-- Consumers-Cleared: claimed\n"
            "ALTER TABLE core.workspace_agent_messages DROP COLUMN sender_kind;\n"
        )
        hits_upper = gate.evaluate_migrations(
            [("hirey_core/migrations/0912c_upper.sql", sql_upper)],
            index,
        )
        self.assertEqual(len(hits_upper), 1)
        self.assertIn("hirey_core/upper_reader.py:2", hits_upper[0])

        sql_bad_indent = (
            "-- Contraction-Of: drop legacy_upper_col\n"
            "-- Consumers-Cleared: claimed\n"
            "ALTER TABLE core.items DROP COLUMN legacy_upper_col;\n"
        )
        hits_indent = gate.evaluate_migrations(
            [("hirey_core/migrations/0912d_indent.sql", sql_bad_indent)],
            index,
        )
        self.assertEqual(len(hits_indent), 1)
        self.assertIn("hirey_core/bad_indent.py:3", hits_indent[0])

    def test_annotated_contraction_passes_with_zero_consumers_via_sql_comments_or_pr_body(self) -> None:
        sql_with_comments = """
        -- Contraction-Of: ENG-537 remove unused legacy_items table after backfill
        -- Consumers-Cleared: verified 0 runtime refs in hi-secretary and hi-platform
        DROP TABLE core.legacy_items;
        """
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0912_clean_comment.sql", sql_with_comments)],
                self._empty_index(),
            ),
            [],
        )

        sql_plain = "ALTER TABLE core.items DROP COLUMN obsolete_flag;"
        pr_body = (
            "## What changed\n"
            "- Contraction-Of: ENG-537 drop obsolete_flag column\n"
            "- Consumers-Cleared: PR #999 removed all runtime queries\n"
        )
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0913_clean_pr.sql", sql_plain)],
                self._empty_index(),
                pr_body=pr_body,
            ),
            [],
        )

    def test_annotated_contraction_blocked_when_runtime_consumers_remain(self) -> None:
        index = gate.build_consumer_index({
            "hirey_core/items_reader.py": (
                '"""Module doc referencing obsolete_flag and core.items safely."""\n'
                "# Comment mentioning obsolete_flag safely\n"
                "def read_item():\n"
                '    return "SELECT id, obsolete_flag FROM core.items"\n'
            ),
            "hirey_core/unrelated_module.py": (
                '"""Unrelated module using generic column name status."""\n'
                'def get_other():\n    return "SELECT status FROM core.other_table"\n'
            ),
        })

        # Unresolved consumer on `obsolete_flag` inside `core.items` reader blocks even with headers.
        sql_blocked = """
        -- Contraction-Of: ENG-537 drop obsolete_flag
        -- Consumers-Cleared: claimed cleared
        ALTER TABLE core.items DROP COLUMN obsolete_flag;
        """
        violations = gate.evaluate_migrations(
            [("hirey_core/migrations/0914_has_consumer.sql", sql_blocked)],
            index,
        )
        self.assertEqual(len(violations), 1)
        self.assertIn("hirey_core/items_reader.py:4", violations[0])

        # Generic column `status` on `core.items` passes once `items_reader.py` doesn't mention `status`
        # (does NOT false-positive on `unrelated_module.py`).
        sql_generic_col = """
        -- Contraction-Of: ENG-537 drop legacy status on core.items
        -- Consumers-Cleared: cleared in items_reader
        ALTER TABLE core.items DROP COLUMN status;
        """
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0915_generic_col_ok.sql", sql_generic_col)],
                index,
            ),
            [],
        )

    def test_table_drop_consumer_ignores_comments_and_docstrings(self) -> None:
        index = gate.build_consumer_index({
            "hirey_core/clean.py": (
                '"""Docstring mentioning legacy_table."""\n'
                "# legacy_table was retired in 0916\n"
                "def ok():\n    return 1\n"
            ),
        })
        sql = """
        -- Contraction-Of: ENG-537 drop legacy_table
        -- Consumers-Cleared: all callers removed
        DROP TABLE core.legacy_table;
        """
        self.assertEqual(
            gate.evaluate_migrations([("hirey_core/migrations/0916_drop.sql", sql)], index),
            [],
        )

    def test_lock_table_mode_distinction_share_update_exclusive_vs_dml_blocking(self) -> None:
        # 1. SHARE UPDATE EXCLUSIVE + UPDATE backfill (e.g. 0873 pattern) passes.
        sue_backfill = """
        DO $$
        BEGIN
            LOCK TABLE core.workspace_agent_messages IN SHARE UPDATE EXCLUSIVE MODE NOWAIT;
        END $$;
        UPDATE core.workspace_agent_messages SET message_kind = body_json->>'kind';
        """
        self.assertEqual(
            gate.evaluate_migrations(
                [("hirey_core/migrations/0950_sue_backfill.sql", sue_backfill)],
                self._empty_index(),
            ),
            [],
        )

        # Other non-DML-blocking LOCK modes also allow UPDATE backfills without ALTER TABLE.
        for mode in ("ACCESS SHARE", "ROW SHARE", "ROW EXCLUSIVE"):
            sql_ok = f"""
            LOCK TABLE core.items IN {mode} MODE;
            UPDATE core.items SET label = chr(120);
            """
            self.assertEqual(
                gate.evaluate_migrations(
                    [("hirey_core/migrations/0950b_non_dml_lock.sql", sql_ok)],
                    self._empty_index(),
                ),
                [],
                mode,
            )

        # 2. SHARE UPDATE EXCLUSIVE still blocks CREATE INDEX in the same migration.
        sue_index = """
        LOCK TABLE core.workspace_agent_messages IN SHARE UPDATE EXCLUSIVE MODE NOWAIT;
        CREATE INDEX IF NOT EXISTS workspace_agent_messages_kind_idx
            ON core.workspace_agent_messages(message_kind);
        """
        v_sue_idx = gate.evaluate_migrations(
            [("hirey_core/migrations/0951_sue_create_index.sql", sue_index)],
            self._empty_index(),
        )
        self.assertEqual(len(v_sue_idx), 1)
        self.assertIn("post-lock CREATE INDEX", v_sue_idx[0])

        # 3. DML/read-blocking lock modes (ACCESS EXCLUSIVE, EXCLUSIVE, SHARE ROW EXCLUSIVE,
        # SHARE, default LOCK TABLE) and ALTER TABLE still block UPDATE backfills.
        for lock_stmt in (
            "LOCK TABLE core.items IN ACCESS EXCLUSIVE MODE;",
            "LOCK TABLE core.items IN EXCLUSIVE MODE;",
            "LOCK TABLE core.items IN SHARE ROW EXCLUSIVE MODE;",
            "LOCK TABLE core.items IN SHARE MODE;",
            "LOCK TABLE core.items;",
            "ALTER TABLE core.items ADD COLUMN IF NOT EXISTS note text;",
        ):
            blocked_sql = f"""
            {lock_stmt}
            UPDATE core.items SET note = chr(120);
            """
            v_blk = gate.evaluate_migrations(
                [("hirey_core/migrations/0952_dml_blocking_lock.sql", blocked_sql)],
                self._empty_index(),
            )
            self.assertEqual(len(v_blk), 1, lock_stmt)
            self.assertIn("post-lock UPDATE backfill", v_blk[0])

        # 4. SHARE UPDATE EXCLUSIVE followed by ALTER TABLE upgrades lock to DML-blocking.
        upgraded = """
        LOCK TABLE core.items IN SHARE UPDATE EXCLUSIVE MODE NOWAIT;
        ALTER TABLE core.items ADD COLUMN IF NOT EXISTS note text;
        UPDATE core.items SET note = chr(120);
        """
        v_upg = gate.evaluate_migrations(
            [("hirey_core/migrations/0953_upgraded_lock.sql", upgraded)],
            self._empty_index(),
        )
        self.assertEqual(len(v_upg), 1)
        self.assertIn("post-lock UPDATE backfill", v_upg[0])
        self.assertIn("ALTER TABLE core.items", v_upg[0])


class MigrationSafetyGateCLITests(unittest.TestCase):
    def test_end_to_end_git_base_head_and_pr_body_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _git(repo, "init", "-q", "-b", "main")
            _git(repo, "config", "user.email", "gate@test")
            _git(repo, "config", "user.name", "gate")

            (repo / "hirey_core" / "migrations").mkdir(parents=True)
            (repo / "hirey_core" / "migrations" / "0001_init.sql").write_text(
                "CREATE TABLE core.widgets (id text PRIMARY KEY, old_col text);\n",
                encoding="utf-8",
            )
            (repo / "hirey_core" / "widgets.py").write_text(
                'QUERY = "SELECT id, old_col FROM core.widgets"\n',
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-q", "-m", "base")
            base = _git(repo, "rev-parse", "HEAD")

            # Branch 1: contraction without consumer removal fails (exit 1).
            _git(repo, "checkout", "-q", "-b", "bad-contraction", base)
            (repo / "hirey_core" / "migrations" / "0002_drop_old_col.sql").write_text(
                "-- Contraction-Of: drop old_col\n"
                "-- Consumers-Cleared: attempt\n"
                "ALTER TABLE core.widgets DROP COLUMN old_col;\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-q", "-m", "drop col prematurely")
            bad_res = subprocess.run(
                [sys.executable, str(SCRIPT), "--base", base, "--head", "HEAD"],
                cwd=str(repo),
                capture_output=True,
                text=True,
            )
            self.assertEqual(bad_res.returncode, 1, bad_res.stdout + bad_res.stderr)
            self.assertIn("hirey_core/widgets.py:1", bad_res.stderr)

            # Branch 2: consumer removed + PR_BODY supplies headers -> passes (exit 0).
            _git(repo, "checkout", "-q", "-b", "good-contraction", base)
            (repo / "hirey_core" / "widgets.py").write_text(
                'QUERY = "SELECT id FROM core.widgets"\n',
                encoding="utf-8",
            )
            (repo / "hirey_core" / "migrations" / "0002_drop_old_col.sql").write_text(
                "ALTER TABLE core.widgets DROP COLUMN old_col;\n",
                encoding="utf-8",
            )
            _git(repo, "add", ".")
            _git(repo, "commit", "-q", "-m", "drop col after clearing consumer")
            env = dict(os.environ)
            env["PR_BODY"] = (
                "Contraction-Of: ENG-537 remove old_col\n"
                "Consumers-Cleared: widgets.py updated\n"
            )
            good_res = subprocess.run(
                [sys.executable, str(SCRIPT), "--base", base, "--head", "HEAD"],
                cwd=str(repo),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(good_res.returncode, 0, good_res.stdout + good_res.stderr)

            # Verify standalone invocation when copied into RUNNER_TEMP outside the repo tree.
            with tempfile.TemporaryDirectory() as runner_tmp:
                copied_gate = Path(runner_tmp) / "migration_safety_gate.py"
                copied_gate.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
                runner_res = subprocess.run(
                    [sys.executable, str(copied_gate), "--base", base, "--head", "HEAD"],
                    cwd=str(repo),
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(runner_res.returncode, 0, runner_res.stdout + runner_res.stderr)

    def test_real_repo_sample_files_via_file_mode(self) -> None:
        pass_cases = [
            ROOT / "hirey_core/migrations/0873_backfill_workspace_agent_messages_relational_columns.sql",
            ROOT / "hirey_core/migrations/0870_page_rey_proposals.sql",
            ROOT / "hirey_core/migrations/0869_retire_agent_message_attachment_fact_sha256_sql_functions.sql",
            ROOT / "hirey_core/migrations/0850_retire_batch_32_survivor_and_media_job_sql_functions.sql",
            ROOT / "hirey_core/migrations/0843_need_categories.sql",
        ]
        for path in pass_cases:
            res = subprocess.run(
                [sys.executable, str(SCRIPT), "--file", str(path), "--repo-root", str(ROOT)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"{path}: {res.stdout} {res.stderr}")

        fail_0868 = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--file",
                str(ROOT / "hirey_core/migrations/0868_workspace_agent_messages_relational_columns.sql"),
                "--repo-root",
                str(ROOT),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(fail_0868.returncode, 1)
        self.assertIn("post-lock", fail_0868.stderr)


if __name__ == "__main__":
    unittest.main()
