#!/usr/bin/env python3
"""Merged Core migrations are immutable; a PR may only add higher numbers (ENG-143, ENG-524).

A migration that reached main may already be applied somewhere (test, prod),
and `hirey_core/migrations.py` refuses a checksum drift. So a PR may only add
files under `hirey_core/migrations/`, and each added file's number must be
above the highest number already on the base branch. To change behaviour,
add a new migration (for example `CREATE OR REPLACE` the function).

ENG-524 adds four guards on top of that rule:

* Rule A: added migration numbers in the reserved placeholder band
  ``9000..9999`` are refused before merge.
* Rule B: real migration numbers in ``8500..8999`` are refused, because they
  are approaching the placeholder band.
* Rule C: ``hirey_core/migrations/LATEST`` must name the highest real
  migration on the head, must move whenever the PR adds/renames a migration,
  and must not move when the PR has no migration change.
* Rule D: while ``main`` still has a duplicate real migration number, the only
  allowed migration-area change is a byte-identical ``R100`` rename of the
  *later-merged* duplicate to the next free numbers.  The earlier-merged file
  may already have been applied, so it can never be renamed.  The repair must
  leave no duplicate (and no ``>= 8500``) migration on the head.  First-parent
  history proves the later duplicate was never loaded on ``main``: ``discover_migrations()``
  rejects duplicate versions before running any SQL, on the premise that nobody
  manually deployed a PR branch containing that duplicate file to test or prod
  before merge.

The script is deliberately self-contained: only the standard library and
``git`` subprocesses from the current working directory.  CI copies it to
``${RUNNER_TEMP}`` and runs it against a full-history checkout, so it must not
import a sibling module.

Usage: migration_immutability_gate.py --base <base sha> --head <head sha>
"""

import argparse
import re
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MIGRATIONS_PREFIX = "hirey_core/migrations/"
RESTORES_PREFIX = "hirey_core/migrations_restore/"
LATEST_PATH = "hirey_core/migrations/LATEST"

MIGRATION_VERSION = re.compile(r"^(?P<version>[0-9]{4})_(?P<slug>.+)\.sql$")
RESTORE_VERSION = re.compile(r"^(?P<version>[0-9]{4})_(?P<slug>.+)_restore\.sql$")

PLACEHOLDER_MIN = 9000
PLACEHOLDER_MAX = 9999
CEILING_WARN = 8500


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _version(path: str) -> Optional[int]:
    match = MIGRATION_VERSION.match(_basename(path))
    return int(match.group("version")) if match else None


def _slug(path: str) -> Optional[str]:
    match = MIGRATION_VERSION.match(_basename(path))
    return match.group("slug") if match else None


def _restore_parts(path: str) -> Optional[Tuple[str, int]]:
    match = RESTORE_VERSION.match(_basename(path))
    if not match:
        return None
    return match.group("slug"), int(match.group("version"))


def _is_migration(path: str) -> bool:
    return path.startswith(MIGRATIONS_PREFIX) and path.endswith(".sql")


def _is_restore(path: str) -> bool:
    return path.startswith(RESTORES_PREFIX) and path.endswith(".sql")


def _rows(name_status: Iterable[str]) -> List[Tuple[str, List[str]]]:
    rows: List[Tuple[str, List[str]]] = []
    for line in name_status:
        if not line.strip():
            continue
        fields = line.split("\t")
        rows.append((fields[0], fields[1:]))
    return rows


def _derive_head_paths(base_paths: Iterable[str], name_status: Iterable[str]) -> List[str]:
    """Approximate the head tree for pure unit tests that only pass two arguments."""
    paths = set(base_paths)
    for status, row_paths in _rows(name_status):
        if status == "A":
            paths.update(row_paths)
        elif status == "D":
            paths.difference_update(row_paths)
        elif status.startswith(("R", "C")) and len(row_paths) >= 2:
            paths.discard(row_paths[0])
            paths.add(row_paths[1])
        # M / T keep the same path.
    return sorted(paths)


def _highest_real_migration(paths: Iterable[str]) -> Optional[str]:
    best: Optional[Tuple[int, str]] = None
    for path in paths:
        if not _is_migration(path):
            continue
        version = _version(path)
        if version is None or version >= PLACEHOLDER_MIN:
            continue
        candidate = (version, path)
        if best is None or candidate > best:
            best = candidate
    return best[1] if best else None


def _migrations_by_version(paths: Iterable[str]) -> Dict[int, List[str]]:
    by_version: Dict[int, List[str]] = {}
    for path in paths:
        if not _is_migration(path):
            continue
        version = _version(path)
        if version is None or version >= PLACEHOLDER_MIN:
            continue
        by_version.setdefault(version, []).append(path)
    return by_version


def _touches_latest(rows: Sequence[Tuple[str, List[str]]]) -> bool:
    return any(LATEST_PATH in paths for _status, paths in rows)


def _touches_migration(rows: Sequence[Tuple[str, List[str]]]) -> bool:
    return any(_is_migration(path) for _status, paths in rows for path in paths)


def _latest_violations(
    rows: Sequence[Tuple[str, List[str]]],
    head_paths: Iterable[str],
    base_latest: Optional[str],
    head_latest: Optional[str],
) -> List[str]:
    touched_latest = _touches_latest(rows)
    touched_sql = _touches_migration(rows)
    if base_latest is None and head_latest is None and not touched_latest:
        return []
    # A PR branched before LATEST existed has no LATEST on its head only because
    # the file did not exist at the merge-base.  It is not a deletion: a genuine
    # deletion touches LATEST (a D row for hirey_core/migrations/LATEST), and a
    # migration PR without LATEST is still rejected below by the touched_sql checks.
    if head_latest is None and not touched_latest and not touched_sql:
        return []

    violations: List[str] = []
    expected_path = _highest_real_migration(head_paths)
    expected_name = _basename(expected_path) if expected_path else None
    expected_label = expected_name or "the highest real <NNNN>_<slug>.sql migration"

    if head_latest is None:
        violations.append(
            f"{LATEST_PATH}: LATEST cannot be deleted; it must name {expected_label}."
        )
    else:
        value = head_latest.strip()
        if "\n" in value or "\r" in value:
            violations.append(
                f"{LATEST_PATH}: must be a single line naming {expected_label}; got {value!r}."
            )
        elif expected_name is None:
            violations.append(
                f"{LATEST_PATH}: cannot name a migration because the head has no real "
                "<NNNN>_<slug>.sql migration."
            )
        elif value != expected_name:
            violations.append(
                f"{LATEST_PATH}: must name {expected_name}; got {value or '(empty)'}."
            )

    initial_creation = (
        base_latest is None
        and touched_latest
        and any(status == "A" and LATEST_PATH in paths for status, paths in rows)
    )
    if touched_sql and not touched_latest:
        violations.append(
            f"{LATEST_PATH}: PR touches Core migrations and must update "
            f"{LATEST_PATH} to {expected_label}."
        )
    if not touched_sql and touched_latest and not initial_creation:
        violations.append(
            f"{LATEST_PATH}: PR does not add or rename any Core migration so it must not "
            f"modify {LATEST_PATH}."
        )
    return violations


def _repair_violations(
    sql_rows: Sequence[Tuple[str, List[str]]],
    duplicate_versions: Set[int],
    base_max: int,
    head_paths: Iterable[str],
    later_duplicate_paths: Set[str],
    restore_rows: Sequence[Tuple[str, List[str]]],
) -> List[str]:
    violations: List[str] = []
    duplicate_label = ", ".join(f"{version:04d}" for version in sorted(duplicate_versions))
    renames: List[Tuple[str, int, int, str, str]] = []

    for status, paths in sql_rows:
        joined = " -> ".join(paths)
        if len(paths) != 2 or not status.startswith("R"):
            violations.append(
                f"{status} {joined}: main has duplicate migration number(s) {duplicate_label}; "
                "only byte-identical R100 renames of the later-merged duplicate can be merged."
            )
            continue
        old_path, new_path = paths
        if status != "R100":
            violations.append(
                f"{status} {joined}: a duplicate-number repair must be a byte-identical R100 rename."
            )
            continue
        old_version = _version(old_path)
        new_version = _version(new_path)
        old_slug = _slug(old_path)
        new_slug = _slug(new_path)
        if old_version is None or new_version is None or old_slug is None or new_slug is None:
            violations.append(
                f"{status} {joined}: both sides of a duplicate-number repair must be "
                "<NNNN>_<slug>.sql migrations."
            )
            continue
        if old_path not in later_duplicate_paths:
            violations.append(
                f"R100 {joined}: {old_path} (version {old_version:04d}) was merged before "
                "another migration with the same number; renaming it would change which "
                "migration test/prod applied. Only the later-merged duplicate can be renamed."
            )
            continue
        if old_slug != new_slug:
            violations.append(
                f"R100 {joined}: the slug must not change during a duplicate-number repair."
            )
            continue
        renames.append((old_slug, old_version, new_version, old_path, new_path))

    if not renames:
        return violations

    new_versions = [item[2] for item in renames]
    expected = list(range(base_max + 1, base_max + 1 + len(new_versions)))
    if sorted(new_versions) != expected:
        first = expected[0]
        last = expected[-1]
        violations.append(
            f"duplicate-number repair renames must use the consecutive numbers "
            f"{first:04d}..{last:04d} above main's latest {base_max:04d}; "
            f"got {sorted(new_versions)}."
        )
    if any(version >= CEILING_WARN for version in new_versions):
        violations.append(
            f"duplicate-number repair numbers must stay below {CEILING_WARN}; "
            f"got {sorted(new_versions)}."
        )

    head_versions: List[int] = []
    for path in head_paths:
        if not _is_migration(path):
            continue
        version = _version(path)
        if version is not None:
            head_versions.append(version)
    seen: Set[int] = set()
    remaining_duplicates: Set[int] = set()
    for version in head_versions:
        if version in seen:
            remaining_duplicates.add(version)
        seen.add(version)
    if remaining_duplicates:
        violations.append(
            "duplicate migration number(s) still remain on the head after the repair: "
            + ", ".join(f"{version:04d}" for version in sorted(remaining_duplicates))
            + "."
        )
    too_high = sorted({version for version in head_versions if version >= CEILING_WARN})
    if too_high:
        violations.append(
            f"the head still has migration number(s) >= {CEILING_WARN}: "
            + ", ".join(f"{version:04d}" for version in too_high)
            + "."
        )

    allowed_restores = {(slug, old, new) for slug, old, new, _o, _n in renames}
    for status, paths in restore_rows:
        if not any(_is_restore(path) for path in paths):
            continue
        joined = " -> ".join(paths)
        if len(paths) != 2 or status != "R100":
            violations.append(
                f"{status} {joined}: companion restore files may only be byte-identical "
                "R100 renames during a duplicate-number repair."
            )
            continue
        old_parts = _restore_parts(paths[0])
        new_parts = _restore_parts(paths[1])
        if old_parts is None or new_parts is None:
            violations.append(
                f"{status} {joined}: both sides of a companion restore repair must be "
                "<NNNN>_<slug>_restore.sql."
            )
            continue
        old_slug, old_version = old_parts
        new_slug, new_version = new_parts
        if (
            old_slug != new_slug
            or (old_slug, old_version, new_version) not in allowed_restores
        ):
            violations.append(
                f"R100 {joined}: companion restore rename does not match an allowed "
                "migration repair."
            )
    return violations


def evaluate(
    name_status: Iterable[str],
    base_paths: Iterable[str],
    head_paths: Optional[Iterable[str]] = None,
    base_latest: Optional[str] = None,
    head_latest: Optional[str] = None,
    later_duplicate_paths: Optional[Iterable[str]] = None,
    restore_name_status: Optional[Iterable[str]] = None,
) -> List[str]:
    """Return one message per violation.

    name_status: lines of `git diff --name-status --find-renames base...head`.
    base_paths: paths of the migrations on the base branch tip.
    head_paths: paths of the migrations on the head tip (derived for 2-arg calls).
    base_latest / head_latest: contents of ``hirey_core/migrations/LATEST``.
    later_duplicate_paths: base duplicate paths that entered main second.
    restore_name_status: lines of the same diff restricted to the restore dir.
    """
    base_paths = list(base_paths)
    rows = _rows(name_status)
    restore_rows = _rows(restore_name_status or [])
    if head_paths is None:
        head_paths = _derive_head_paths(base_paths, name_status)
    else:
        head_paths = list(head_paths)
    later = set(later_duplicate_paths or ())

    base_versions = [
        version
        for version in (_version(path) for path in base_paths if _is_migration(path))
        if version is not None and version < PLACEHOLDER_MIN
    ]
    base_max = max(base_versions, default=0)
    base_by_version = _migrations_by_version(base_paths)
    duplicate_versions = {v for v, paths in base_by_version.items() if len(paths) >= 2}

    violations: List[str] = []

    # Rule A / Rule B: every file that enters the migrations directory.
    for status, paths in rows:
        if status == "A":
            incoming = [path for path in paths if _is_migration(path)]
        elif status.startswith(("R", "C")):
            incoming = [path for path in paths[1:] if _is_migration(path)]
        else:
            incoming = []
        for path in incoming:
            version = _version(path)
            if version is None:
                continue
            if PLACEHOLDER_MIN <= version <= PLACEHOLDER_MAX:
                violations.append(
                    f"A {path}: placeholder migration number {version:04d} cannot be "
                    "merged to main; run 'python3 scripts/renumber_migrations.py' before "
                    "merging."
                )
            elif CEILING_WARN <= version < PLACEHOLDER_MIN:
                violations.append(
                    f"A {path}: migration number {version:04d} has reached the "
                    f"{CEILING_WARN} safety threshold; real migration numbers must stay "
                    f"below {CEILING_WARN} before the {PLACEHOLDER_MIN}..{PLACEHOLDER_MAX} "
                    "placeholder band. Run 'python3 scripts/renumber_migrations.py' "
                    "before merging."
                )

    sql_rows = [(status, paths) for status, paths in rows if any(_is_migration(p) for p in paths)]

    if duplicate_versions:
        # Rule D: while main is broken only the narrow later-duplicate repair is allowed.
        if sql_rows:
            violations.extend(
                _repair_violations(
                    sql_rows,
                    duplicate_versions,
                    base_max,
                    head_paths,
                    later,
                    restore_rows,
                )
            )
    else:
        for status, paths in sql_rows:
            touched = [path for path in paths if _is_migration(path)]
            if status != "A":
                violations.append(
                    f"{status[0]} {' -> '.join(paths)}: merged migrations can't be changed, "
                    "renamed or deleted; add a new, higher-numbered migration instead."
                )
                continue
            version = _version(touched[0])
            if version is not None and version <= base_max:
                message = (
                    f"A {touched[0]}: number {version:04d} is not above main's latest "
                    f"{base_max:04d}; renumber it to {base_max + 1:04d} or higher."
                )
                if _touches_latest(rows):
                    message += (
                        f" Also update {LATEST_PATH} to the renumbered migration name."
                    )
                violations.append(message)
        if sql_rows:
            # Two PR-added migrations can share a real number while each is
            # individually above base_max; only the head tree shows the collision.
            for version, paths in sorted(_migrations_by_version(head_paths).items()):
                if version > base_max and len(paths) >= 2:
                    violations.append(
                        f"duplicate migration number {version:04d} on head: "
                        f"{', '.join(sorted(paths))}; each migration must have a unique number."
                    )

    # Rule C: the LATEST pointer discipline.
    violations.extend(_latest_violations(rows, head_paths, base_latest, head_latest))
    return violations


# ---------------------------------------------------------------------------
# Git-backed inspection (stdlib + git only; safe when copied to RUNNER_TEMP).
# ---------------------------------------------------------------------------


def _run_git(*args: str, cwd=None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"git {' '.join(args)} failed: {detail or proc.returncode}")
    return proc


def _git(*args: str, cwd=None) -> str:
    return _run_git(*args, cwd=cwd).stdout


def _read_at(rev: str, path: str, cwd=None) -> Optional[str]:
    proc = _run_git("show", f"{rev}:{path}", cwd=cwd, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout


def _tree_has(rev: str, path: str, cwd=None) -> bool:
    return bool(_run_git("ls-tree", "--name-only", rev, "--", path, cwd=cwd).stdout.strip())


def _first_parent_introduction(base: str, path: str, cwd=None) -> Optional[str]:
    """Return the first-parent commit on ``base`` that introduced ``path``."""
    proc = _run_git(
        "log",
        "--first-parent",
        "--no-renames",
        "--reverse",
        "--diff-filter=A",
        "--format=%H",
        base,
        "--",
        path,
        cwd=cwd,
        check=False,
    )
    for line in proc.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate
    # A file present from the root commit has no add commit in its history.
    roots = _git("rev-list", "--first-parent", "--max-parents=0", base, cwd=cwd).splitlines()
    for root in roots:
        if _tree_has(root.strip(), path, cwd=cwd):
            return root.strip()
    return None


def later_duplicate_paths(
    base: str,
    duplicate_versions: Iterable[int],
    base_by_version: Dict[int, List[str]],
    cwd=None,
) -> Set[str]:
    """Paths that entered main while another file of the same version was already there.

    First-parent history proves the later duplicate was never loaded on ``main``:
    ``discover_migrations()`` rejects duplicate versions before running any SQL, on
    the premise that nobody manually deployed a PR branch containing that duplicate
    file to test or prod before merge.
    """
    later: Set[str] = set()
    for version in sorted(duplicate_versions):
        for path in base_by_version.get(version, []):
            introduced = _first_parent_introduction(base, path, cwd=cwd)
            if introduced is None:
                continue
            parent = _run_git("rev-parse", "--verify", f"{introduced}^1", cwd=cwd, check=False)
            if parent.returncode != 0:
                continue
            parent_sha = parent.stdout.strip()
            parent_paths = _git(
                "ls-tree", "--name-only", parent_sha, MIGRATIONS_PREFIX, cwd=cwd
            ).splitlines()
            if version in _migrations_by_version(parent_paths):
                later.add(path)
    return later


def inspect(base: str, head: str, cwd=None) -> Dict[str, object]:
    name_status = _git(
        "diff", "--name-status", "--find-renames", f"{base}...{head}", "--", MIGRATIONS_PREFIX,
        cwd=cwd,
    ).splitlines()
    restore_name_status = _git(
        "diff", "--name-status", "--find-renames", f"{base}...{head}", "--", RESTORES_PREFIX,
        cwd=cwd,
    ).splitlines()
    base_paths = _git("ls-tree", "--name-only", base, MIGRATIONS_PREFIX, cwd=cwd).splitlines()
    head_paths = _git("ls-tree", "--name-only", head, MIGRATIONS_PREFIX, cwd=cwd).splitlines()
    base_by_version = _migrations_by_version(base_paths)
    duplicate_versions = {v for v, paths in base_by_version.items() if len(paths) >= 2}
    return {
        "name_status": name_status,
        "restore_name_status": restore_name_status,
        "base_paths": base_paths,
        "head_paths": head_paths,
        "base_latest": _read_at(base, LATEST_PATH, cwd=cwd),
        "head_latest": _read_at(head, LATEST_PATH, cwd=cwd),
        "later_duplicate_paths": later_duplicate_paths(
            base, duplicate_versions, base_by_version, cwd=cwd
        ),
    }


def _tree_entry_at(rev: str, path: str, cwd=None) -> Optional[Tuple[str, str]]:
    out = _run_git("ls-tree", "-z", "--full-tree", rev, "--", path, cwd=cwd).stdout
    if not out:
        return None
    record = out.split("\0", 1)[0]
    meta, _tab, record_path = record.partition("\t")
    if record_path != path:
        return None
    fields = meta.split(" ")
    if len(fields) != 3:
        return None
    mode, obj_type, oid = fields
    if obj_type != "blob":
        return None
    return mode, oid


def _rename_identity_violations(
    base: str,
    head: str,
    *row_groups: Iterable[str],
    cwd=None,
) -> List[str]:
    """Refuse an R100 rename whose base blob/mode is not byte-identical at head."""
    violations: List[str] = []
    for name_status in row_groups:
        for status, paths in _rows(name_status):
            if status != "R100" or len(paths) != 2:
                continue
            old_path, new_path = paths
            old_entry = _tree_entry_at(base, old_path, cwd=cwd)
            new_entry = _tree_entry_at(head, new_path, cwd=cwd)
            if old_entry is None or new_entry is None or old_entry != new_entry:
                violations.append(
                    f"R100 {old_path} -> {new_path}: git reported 100% similarity but "
                    "the blob or mode differs between base and head; refusing the rename."
                )
    return violations


def check(base: str, head: str, cwd=None) -> List[str]:
    """Return every violation for ``base...head`` using the full git inspection."""
    data = inspect(base, head, cwd=cwd)
    violations = evaluate(**data)  # type: ignore[arg-type]
    violations.extend(
        _rename_identity_violations(
            base,
            head,
            data["name_status"],  # type: ignore[arg-type]
            data["restore_name_status"],  # type: ignore[arg-type]
            cwd=cwd,
        )
    )
    return violations


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    violations = check(args.base, args.head)
    for violation in violations:
        print(f"::error::{violation}")
    if violations:
        print(
            f"{len(violations)} migration violation(s). See AGENTS.md, "
            "'Merged migrations are immutable'."
        )
        return 1
    print("Migrations: only new, higher-numbered files added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
