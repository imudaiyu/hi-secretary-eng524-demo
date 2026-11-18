#!/usr/bin/env python3
"""Tests for the merged-migration immutability gate (ENG-143, ENG-524)."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "migration_immutability_gate.py"
spec = importlib.util.spec_from_file_location("migration_immutability_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)

M = "hirey_core/migrations/"
R = "hirey_core/migrations_restore/"
LATEST = M + "LATEST"
BASE = [M + "0741_a.sql", M + "0742_b.sql", M + "0743_c.sql"]
checks = 0


def ok(condition, message):
    global checks
    assert condition, "FAIL: " + message
    checks += 1


def has(violations, *needles):
    return all(any(needle in item for item in violations) for needle in needles)


# ---------------------------------------------------------------------------
# Pure evaluation: the original ENG-143 contract.
# ---------------------------------------------------------------------------
ok(gate.evaluate([f"A\t{M}0744_new.sql"], BASE) == [], "a higher new number passes")
ok(gate.evaluate([], BASE) == [], "no migration change passes")
ok(gate.evaluate(["M\tREADME.md", "D\tscripts/x.py"], BASE) == [], "non-migration paths are ignored")
ok(gate.evaluate([f"M\t{M}notes.txt"], BASE) == [], "non-.sql files in the directory are ignored")

v = gate.evaluate([f"M\t{M}0722_old.sql"], BASE)
ok(len(v) == 1 and "0722_old.sql" in v[0] and "can't be changed" in v[0], "editing a merged migration fails")
ok(len(gate.evaluate([f"D\t{M}0741_a.sql"], BASE)) == 1, "deleting a merged migration fails")
ok(len(gate.evaluate([f"R100\t{M}0741_a.sql\t{M}0745_a.sql"], BASE)) == 1, "renaming a merged migration fails")
ok(len(gate.evaluate([f"T\t{M}0741_a.sql"], BASE)) == 1, "a type change fails")

v = gate.evaluate([f"A\t{M}0743_dup.sql"], BASE)
ok(len(v) == 1 and "0743" in v[0] and "0744" in v[0], "reusing main's latest number fails and names the next free one")
ok(len(gate.evaluate([f"A\t{M}0738_old_slot.sql"], BASE)) == 1, "a number below main's latest fails")
ok(gate.evaluate([f"A\t{M}0001_first.sql"], []) == [], "an empty base allows any number")

two = gate.evaluate([f"M\t{M}0741_a.sql", f"A\t{M}0700_x.sql", f"A\t{M}0744_y.sql"], BASE)
ok(len(two) == 2, "every violation is listed, valid additions are not")

# ---------------------------------------------------------------------------
# Rule A: 9000..9999 placeholders are always refused.
# ---------------------------------------------------------------------------
for placeholder in ("9000", "9001", "9999"):
    v = gate.evaluate([f"A\t{M}{placeholder}_demo.sql"], BASE)
    expected = (
        f"A {M}{placeholder}_demo.sql: placeholder migration number {placeholder} cannot be "
        "merged to main; run 'python3 scripts/renumber_migrations.py' before merging."
    )
    ok(v == [expected], f"placeholder {placeholder} is refused with the exact message: {v}")

# A stale 9xxx file already on base must not raise base_max.
base_with_placeholder = BASE + [M + "9999_stale.sql", M + "9001_old_placeholder.sql"]
ok(gate.evaluate([f"A\t{M}0744_new.sql"], base_with_placeholder) == [],
   "base_max ignores 9xxx placeholders already on base")
ok(len(gate.evaluate([f"A\t{M}9001_demo.sql"], base_with_placeholder)) == 1,
   "a new placeholder is still refused when base already has one")

# ---------------------------------------------------------------------------
# Rule B: 8500..8999 is the approaching-placeholder ceiling.
# ---------------------------------------------------------------------------
v = gate.evaluate([f"A\t{M}8500_ceiling.sql"], BASE)
ok(len(v) == 1 and "8500" in v[0] and "safety threshold" in v[0],
   "8500 is refused with the ceiling explanation")
ok(len(gate.evaluate([f"A\t{M}8999_ceiling.sql"], BASE)) == 1, "8999 is refused")
ok(gate.evaluate([f"A\t{M}8499_just_below.sql"], BASE) == [], "8499 still passes")
v = gate.evaluate([f"R100\t{M}0741_a.sql\t{M}8500_renamed.sql"], BASE)
ok(any("8500" in item and "safety threshold" in item for item in v),
   "a rename into the ceiling is refused too")

# ---------------------------------------------------------------------------
# Rule C: LATEST pointer discipline.
# ---------------------------------------------------------------------------
ok(gate.evaluate([f"A\t{M}0744_new.sql", f"M\t{LATEST}"], BASE,
                 head_paths=BASE + [M + "0744_new.sql"],
                 base_latest="0743_c.sql\n", head_latest="0744_new.sql\n") == [],
   "a migration addition with the matching LATEST passes")

v = gate.evaluate([f"A\t{M}0744_new.sql", f"M\t{LATEST}"], BASE,
                  head_paths=BASE + [M + "0744_new.sql"],
                  base_latest="0743_c.sql\n", head_latest="0743_c.sql\n")
ok(has(v, "must name 0744_new.sql"), "a stale LATEST is refused: %s" % v)

v = gate.evaluate([f"A\t{M}0744_new.sql", f"M\t{LATEST}"], BASE,
                  head_paths=BASE + [M + "0744_new.sql"],
                  base_latest="0743_c.sql\n", head_latest="0744_new.sql\n")
ok(v == [], "an up-to-date LATEST passes")

v = gate.evaluate([f"D\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest="0743_c.sql\n", head_latest=None)
ok(has(v, "LATEST cannot be deleted"), "deleting LATEST fails: %s" % v)

# A PR branched before LATEST existed has no LATEST on its head; that is not a
# deletion unless it actually touches LATEST or a migration.
v = gate.evaluate([], BASE, head_paths=BASE,
                  base_latest="0743_c.sql\n", head_latest=None)
ok(v == [], "pre-ENG-524 non-migration head is not a LATEST deletion: %s" % v)

v = gate.evaluate([f"A\t{M}0744_new.sql"], BASE,
                  head_paths=BASE + [M + "0744_new.sql"],
                  base_latest="0743_c.sql\n", head_latest=None)
ok(has(v, "must update"), "pre-ENG-524 migration head without LATEST still fails: %s" % v)

v = gate.evaluate([f"D\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest="0743_c.sql\n", head_latest=None)
ok(has(v, "LATEST cannot be deleted"),
   "a genuine post-ENG-524 LATEST deletion still fails: %s" % v)

# A single PR adding two migrations with the same real number must be refused.
DUP_PR_BASE = [M + "0001_a.sql", M + "0002_b.sql"]
DUP_PR_HEAD = DUP_PR_BASE + [M + "0003_alpha.sql", M + "0003_beta.sql"]
v = gate.evaluate(
    [f"A\t{M}0003_alpha.sql", f"A\t{M}0003_beta.sql", f"M\t{LATEST}"],
    DUP_PR_BASE,
    head_paths=DUP_PR_HEAD,
    base_latest="0002_b.sql\n",
    head_latest="0003_beta.sql\n",
)
ok(len(v) == 1 and "duplicate migration number 0003 on head" in v[0],
   "duplicate real numbers added by one PR are refused: %s" % v)
ok(M + "0003_alpha.sql" in v[0] and M + "0003_beta.sql" in v[0],
   "the duplicate violation names both files: %s" % v)
# A re-used number at or below base_max is reported once by the existing rule,
# not also as a head duplicate (0743 already exists on BASE).
ok(len(gate.evaluate([f"A\t{M}0743_dup.sql"], BASE)) == 1,
   "a re-used base_max number is still a single existing-rule violation")

v = gate.evaluate([f"M\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest="0743_c.sql\n", head_latest="0743_c.sql\n0742_b.sql\n")
ok(has(v, "single line", "must not modify"), "a multi-line LATEST fails: %s" % v)

v = gate.evaluate([f"A\t{M}0744_new.sql"], BASE, head_paths=BASE,
                  base_latest="0743_c.sql\n", head_latest="0743_c.sql\n")
ok(has(v, "must update"), "touching migrations without LATEST fails: %s" % v)

v = gate.evaluate([f"M\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest="0743_c.sql\n", head_latest="0743_c.sql\n")
ok(has(v, "must not modify"), "touching LATEST with no migration change fails: %s" % v)

# Initial creation is allowed for the ENG-524 PR itself.
v = gate.evaluate([f"A\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest=None, head_latest="0743_c.sql\n")
ok(v == [], "initial LATEST creation without a migration change passes: %s" % v)
v = gate.evaluate([f"A\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest=None, head_latest="0742_b.sql\n")
ok(has(v, "must name 0743_c.sql"), "initial LATEST creation must name the highest migration: %s" % v)
v = gate.evaluate([f"A\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest=None, head_latest="0743_c.sql\n",
                  later_duplicate_paths=set())
ok(v == [], "initial creation stays clean")

# Added migrations need an updated pointer; a rename also does.
v = gate.evaluate([f"A\t{LATEST}"], BASE, head_paths=BASE,
                  base_latest=None, head_latest="0743_c.sql\n")
ok(v == [], "initial creation without migration changes is the one exception")

# LATEST itself must be ignored for the migration-area parsing (non-.sql).
ok(gate.evaluate([f"M\t{LATEST}"], BASE, head_paths=BASE, base_latest=None, head_latest=None)
   != [], "LATEST-only modification is not silently ignored when it is touched")

# ---------------------------------------------------------------------------
# Rule D: narrow duplicate-number repair channel.
# ---------------------------------------------------------------------------
DUP_BASE = [M + "0853_a.sql", M + "0854_first.sql", M + "0854_second.sql"]
DUP_HEAD = [M + "0853_a.sql", M + "0854_first.sql", M + "0855_second.sql"]
DUP_LATER = {M + "0854_second.sql"}
DUP_ROWS = [f"R100\t{M}0854_second.sql\t{M}0855_second.sql", f"M\t{LATEST}"]


def dup_eval(rows, head_paths=DUP_HEAD, base_latest="0854_second.sql\n",
             head_latest="0855_second.sql\n", restore_rows=None):
    return gate.evaluate(
        rows,
        DUP_BASE,
        head_paths=head_paths,
        base_latest=base_latest,
        head_latest=head_latest,
        later_duplicate_paths=DUP_LATER,
        restore_name_status=restore_rows,
    )


ok(dup_eval(DUP_ROWS) == [], "R100 of the later duplicate to the next number passes: %s" % dup_eval(DUP_ROWS))

v = dup_eval([f"R100\t{M}0854_first.sql\t{M}0855_first.sql", f"M\t{LATEST}"],
             head_paths=[M + "0853_a.sql", M + "0855_first.sql", M + "0854_second.sql"],
             head_latest="0855_first.sql\n")
ok(has(v, "was merged before another migration"), "renaming the earlier duplicate fails: %s" % v)

v = dup_eval([f"R099\t{M}0854_second.sql\t{M}0855_second.sql", f"M\t{LATEST}"])
ok(has(v, "R100"), "a non-R100 rename fails: %s" % v)

v = dup_eval([f"D\t{M}0854_second.sql", f"A\t{M}0855_second.sql", f"M\t{LATEST}"])
ok(len(v) >= 1 and has(v, "only byte-identical R100"), "delete+add is not a repair: %s" % v)

v = dup_eval([f"R100\t{M}0854_second.sql\t{M}0856_second.sql", f"M\t{LATEST}"],
             head_paths=[M + "0853_a.sql", M + "0854_first.sql", M + "0856_second.sql"],
             head_latest="0856_second.sql\n")
ok(has(v, "consecutive numbers"), "a non-sequential repair fails: %s" % v)

v = dup_eval(DUP_ROWS, head_paths=DUP_BASE)
ok(has(v, "still remain on the head"), "a repair that leaves a duplicate fails: %s" % v)

v = dup_eval([f"A\t{M}0860_unrelated.sql"], head_paths=DUP_BASE + [M + "0860_unrelated.sql"],
             head_latest="0860_unrelated.sql\n")
ok(has(v, "only byte-identical R100"), "an unrelated new migration is blocked while main is broken: %s" % v)

restore_ok = [f"R100\t{R}0854_second_restore.sql\t{R}0855_second_restore.sql"]
ok(dup_eval(DUP_ROWS, restore_rows=restore_ok) == [],
   "a matching byte-identical companion restore rename passes: %s" % dup_eval(DUP_ROWS, restore_rows=restore_ok))

v = dup_eval(DUP_ROWS, restore_rows=[f"R099\t{R}0854_second_restore.sql\t{R}0855_second_restore.sql"])
ok(has(v, "companion restore"), "a non-R100 companion restore fails: %s" % v)

v = dup_eval(DUP_ROWS, restore_rows=[f"R100\t{R}0854_other_restore.sql\t{R}0855_other_restore.sql"])
ok(has(v, "does not match an allowed migration repair"),
   "an unrelated companion restore rename fails: %s" % v)

# The original fail-closed behaviour stays when main has no duplicates.
ok(len(gate.evaluate([f"R100\t{M}0741_a.sql\t{M}0745_a.sql"], BASE)) == 1,
   "R100 still fails when main has no duplicate")

# ---------------------------------------------------------------------------
# End to end against scratch git repositories.
# ---------------------------------------------------------------------------
def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def run_gate(cwd, base, head):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--head", head],
        cwd=cwd, capture_output=True, text=True,
    )


def init_repo(root):
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "gate@test")
    git(root, "config", "user.name", "gate")


def commit_files(root, files, message):
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", message)
    return git(root, "rev-parse", "HEAD")


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp)
    init_repo(repo)
    (repo / M).mkdir(parents=True)
    (repo / M / "0001_a.sql").write_text("select 1;\n")
    (repo / M / "0002_b.sql").write_text("select 2;\n")
    (repo / LATEST).write_text("0002_b.sql\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")

    git(repo, "checkout", "-q", "-b", "good")
    commit_files(repo, {
        M + "0003_c.sql": "select 3;\n",
        LATEST: "0003_c.sql\n",
    }, "add")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 0, "git: adding 0003 with LATEST passes: " + result.stdout + result.stderr)

    git(repo, "checkout", "-q", "-b", "stale", base)
    commit_files(repo, {M + "0003_c.sql": "select 3;\n"}, "add without LATEST")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "LATEST" in result.stdout, "git: missing LATEST update fails")
    ok("must update" in result.stdout, "git: the LATEST repair is named")

    git(repo, "checkout", "-q", "-b", "placeholder", base)
    commit_files(repo, {
        M + "9001_demo.sql": "select 9001;\n",
        LATEST: "9001_demo.sql\n",
    }, "placeholder")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "placeholder migration number 9001" in result.stdout,
       "git: a 9001 placeholder fails")

    git(repo, "checkout", "-q", "-b", "ceiling", base)
    commit_files(repo, {
        M + "8500_ceiling.sql": "select 8500;\n",
        LATEST: "8500_ceiling.sql\n",
    }, "ceiling")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "safety threshold" in result.stdout,
       "git: an 8500 migration fails")

    git(repo, "checkout", "-q", "-b", "no-latest", base)
    commit_files(repo, {M + "0004_d.sql": "select 4;\n"}, "add without touching LATEST")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "LATEST" in result.stdout,
       "git: a migration addition without updating the existing LATEST fails")

    git(repo, "checkout", "-q", "-b", "deleted-latest", base)
    (repo / LATEST).unlink()
    commit_files(repo, {"note.txt": "x\n"}, "delete LATEST")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "cannot be deleted" in result.stdout,
       "git: deleting LATEST fails")

    git(repo, "checkout", "-q", "-b", "bad", base)
    (repo / M / "0001_a.sql").write_text("select 10;\n")
    (repo / M / "0002_dup.sql").write_text("select 22;\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "edit")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1, "git: editing 0001 and reusing 0002 fails")
    ok("0001_a.sql" in result.stdout and "0002_dup.sql" in result.stdout, "git: both files are named")


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp)
    init_repo(repo)
    pre_eng524 = commit_files(repo, {
        M + "0741_a.sql": "select 741;\n",
        M + "0742_b.sql": "select 742;\n",
    }, "pre-ENG-524 base without LATEST")
    post_eng524_main = commit_files(repo, {LATEST: "0742_b.sql\n"}, "ENG-524 adds LATEST")

    # An already-open, pre-ENG-524 PR that changes no migration file passes
    # against the post-ENG-524 main.
    git(repo, "checkout", "-q", "-b", "old-non-migration", pre_eng524)
    commit_files(repo, {"docs/readme.md": "unrelated\n"}, "old non-migration change")
    old_non_migration_head = git(repo, "rev-parse", "HEAD")
    result = run_gate(repo, post_eng524_main, old_non_migration_head)
    ok(result.returncode == 0, "git: pre-ENG-524 non-migration PR passes: " + result.stdout + result.stderr)
    ok("Migrations:" in result.stdout, "git: the pre-ENG-524 non-migration PR prints the pass line")

    # The same branch adding a migration without LATEST must still fail.
    git(repo, "checkout", "-q", "-b", "old-migration", pre_eng524)
    commit_files(repo, {M + "0743_new.sql": "select 743;\n"}, "old migration change")
    old_migration_head = git(repo, "rev-parse", "HEAD")
    result = run_gate(repo, post_eng524_main, old_migration_head)
    ok(result.returncode == 1 and "LATEST" in result.stdout,
       "git: pre-ENG-524 migration PR without LATEST still fails")
    ok("must update" in result.stdout, "git: the pre-ENG-524 migration PR is told to update LATEST")

    # A genuine post-ENG-524 deletion of LATEST still fails.
    git(repo, "checkout", "-q", "-b", "delete-latest-post", post_eng524_main)
    (repo / LATEST).unlink()
    commit_files(repo, {"note.txt": "x\n"}, "delete LATEST after ENG-524")
    result = run_gate(repo, post_eng524_main, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "cannot be deleted" in result.stdout,
       "git: post-ENG-524 LATEST deletion fails")


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp)
    init_repo(repo)
    base = commit_files(repo, {
        M + "0001_a.sql": "select 1;\n",
        M + "0002_b.sql": "select 2;\n",
        LATEST: "0002_b.sql\n",
    }, "base")

    git(repo, "checkout", "-q", "-b", "duplicate-real-numbers", base)
    commit_files(repo, {
        M + "0003_alpha.sql": "select 31;\n",
        M + "0003_beta.sql": "select 32;\n",
        LATEST: "0003_beta.sql\n",
    }, "one PR adds two 0003 migrations")
    result = run_gate(repo, base, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "duplicate migration number 0003 on head" in result.stdout,
       "git: one PR adding duplicate real numbers fails: " + result.stdout)
    ok(M + "0003_alpha.sql" in result.stdout and M + "0003_beta.sql" in result.stdout,
       "git: the duplicate PR failure names both files")


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp)
    init_repo(repo)
    first = commit_files(repo, {
        M + "0853_a.sql": "select 853;\n",
        M + "0854_first.sql": "select 8541;\n",
        LATEST: "0854_first.sql\n",
    }, "first 0854")
    dup_main = commit_files(repo, {
        M + "0854_second.sql": "select 8542;\n",
        LATEST: "0854_second.sql\n",
    }, "later duplicate 0854")

    git(repo, "checkout", "-q", "-b", "rename-earlier", dup_main)
    git(repo, "mv", f"{M}0854_first.sql", f"{M}0855_first.sql")
    (repo / LATEST).write_text("0855_first.sql\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "rename earlier")
    result = run_gate(repo, dup_main, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "was merged before another migration" in result.stdout,
       "git: renaming the earlier duplicate fails: " + result.stdout)

    git(repo, "checkout", "-q", "-b", "rename-later", dup_main)
    git(repo, "mv", f"{M}0854_second.sql", f"{M}0855_second.sql")
    (repo / LATEST).write_text("0855_second.sql\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "rename later")
    result = run_gate(repo, dup_main, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 0, "git: R100 rename of the later duplicate passes: " + result.stdout + result.stderr)

    git(repo, "checkout", "-q", "-b", "rename-later-edited", dup_main)
    git(repo, "mv", f"{M}0854_second.sql", f"{M}0855_second.sql")
    (repo / M / "0855_second.sql").write_text("select 8542;\n-- edited\n")
    (repo / LATEST).write_text("0855_second.sql\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "rename later and edit")
    result = run_gate(repo, dup_main, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "R100" in result.stdout,
       "git: a rename with edited SQL fails: " + result.stdout)

    git(repo, "checkout", "-q", "-b", "rename-later-far", dup_main)
    git(repo, "mv", f"{M}0854_second.sql", f"{M}0856_second.sql")
    (repo / LATEST).write_text("0856_second.sql\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "rename later too far")
    result = run_gate(repo, dup_main, git(repo, "rev-parse", "HEAD"))
    ok(result.returncode == 1 and "consecutive numbers" in result.stdout,
       "git: a non-sequential repair fails: " + result.stdout)

    # The only file on the head is still unique after a correct repair.
    result = run_gate(repo, dup_main, "rename-later")
    ok(result.returncode == 0, "git: the correct repair branch is still green")
    ok("PASS" not in result.stdout and "Migrations:" in result.stdout,
       "git: the successful run prints the pass line")

print(f"migration immutability gate: {checks} checks passed")
