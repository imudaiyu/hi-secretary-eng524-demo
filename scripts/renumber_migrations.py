#!/usr/bin/env python3
"""One-command Core migration renumber and mechanical-diff verifier (ENG-414).

Subcommands / modes:
  1. Default (renumber + merge base + gate + verify):
     Merges ``--base`` (default ``origin/main``) into the current branch if needed,
     moves every PR-added file under ``hirey_core/migrations/`` and its companion
     file under ``hirey_core/migrations_restore/`` to ``max(base) + 1 .. max(base) + k``
     via two-phase ``git mv`` (preserving byte-identical contents), writes
     ``hirey_core/migrations/LATEST`` to the final highest real filename, preserves
     any ``Write-Set-Boundary:`` trailer from the previous ``HEAD``, commits the
     result, runs ``scripts/migration_immutability_gate.py``, and prints the
     mechanical-diff verification block.  Placeholder numbers (``9000..9999``) are
     always renumbered; a conflict on ``LATEST`` alone is resolved mechanically.

  2. ``--verify-only``:
     Verifies that ``new_head`` relative to ``reviewed_head`` contains only a
     conflict-free merge of ``base`` plus ``R100`` (byte-identical) renames of the
     PR's added migrations and companion restores, with zero other edits, and exits
     non-zero on any non-mechanical delta. Does not modify any file or Git ref.

Usage:
  python3 scripts/renumber_migrations.py [--base origin/main] [--no-fetch]
  python3 scripts/renumber_migrations.py --verify-only --reviewed-head <sha> [--new-head HEAD] [--base <sha>]
  python3 scripts/renumber_migrations.py --ci-post-mechanical-review --github-repo <owner/repo> --pr <number> --base <sha> --head <sha>
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
import urllib.error
import urllib.parse
import urllib.request

MIGRATIONS_DIR = "hirey_core/migrations"
RESTORES_DIR = "hirey_core/migrations_restore"
LATEST_PATH = "hirey_core/migrations/LATEST"
PLACEHOLDER_MIN = 9000
PLACEHOLDER_MAX = 9999
CEILING_WARN = 8500
MIGRATION_RE = re.compile(
    r"^hirey_core/migrations/(?P<version>[0-9]{4})_(?P<slug>[a-z0-9_]+)\.sql$"
)
RESTORE_RE = re.compile(
    r"^hirey_core/migrations_restore/(?P<version>[0-9]{4})_(?P<slug>[a-z0-9_]+)_restore\.sql$"
)
WRITE_SET_BOUNDARY_RE = re.compile(
    r"^Write-Set-Boundary:[ \t]*([A-Za-z0-9_-]{1,100})[ \t]*$"
)
WRITE_SET_BOUNDARY_PREFIX = re.compile(r"^[ \t]*Write-Set-Boundary(?=[ \t:]|$)")
REGULAR_MODES = frozenset({"100644", "100755"})
ZERO_OID = "0" * 40
REVIEW_HEADER = "HIREY-PRERELEASE-REVIEW v1"
REVIEW_VERDICTS = ("PASS", "RETURNED", "ESCALATED")
SHA40_RE = re.compile(r"[0-9a-f]{40}")
REVIEW_FENCE_RE = re.compile(r"^(`{3,})[^`]*$|^(~{3,}).*$")
REVIEW_RECORD_LINE_RE = re.compile(
    r"^(head_sha|verdict|reviewer|checks|tool_suite|agent_eval|findings)\s*:\s*(.*?)\s*$"
)
WRITERS = ("admin", "write")
BOT_LOGIN = "github-actions[bot]"
GITHUB_API_TIMEOUT = 30
GITHUB_PAGE_SIZE = 100


class RenumberError(RuntimeError):
    """Fail-closed operational or validation error."""


class _TopologyMismatchError(RenumberError):
    """Non-mechanical merge or ancestry topology between reviewed_head and new_head."""


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    oid: str


@dataclass(frozen=True)
class DiffRow:
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str
    status: str
    path: str


@dataclass(frozen=True)
class RenameEntry:
    kind: str  # "migration" or "restore"
    slug: str
    old_version: int
    new_version: int
    old_path: str
    new_path: str
    blob_oid: str
    byte_identical: bool = True


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reviewed_head: str
    new_head: str
    old_base: str
    new_base: str
    upstream_commit_count: int
    renames: Tuple[RenameEntry, ...]
    errors: Tuple[str, ...]


def _run_git(
    repo: Path,
    *args: str,
    input_bytes: Optional[bytes] = None,
    check: bool = True,
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_bytes,
        capture_output=True,
        text=False,
    )
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        stdout = proc.stdout.decode("utf-8", errors="replace").strip()
        detail = stderr or stdout or f"exit code {proc.returncode}"
        raise RenumberError(f"git {' '.join(args)} failed: {detail}")
    return proc


def _git_text(repo: Path, *args: str) -> str:
    return _run_git(repo, *args).stdout.decode("utf-8", errors="replace")


def _resolve_commit(repo: Path, ref: str) -> str:
    return _git_text(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = _run_git(repo, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    raise RenumberError(f"git merge-base --is-ancestor failed: {stderr}")


def _single_merge_base(repo: Path, left: str, right: str) -> str:
    proc = _run_git(repo, "merge-base", "--all", left, right, check=False)
    if proc.returncode not in (0, 1):
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RenumberError(f"git merge-base --all failed: {stderr}")
    lines = [
        line.strip()
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    if len(lines) != 1:
        raise _TopologyMismatchError(
            f"expected a single merge-base between {left[:12]} and {right[:12]}, got {len(lines)}"
        )
    return lines[0]


def _parse_raw_diff(repo: Path, left: str, right: str) -> List[DiffRow]:
    raw = _run_git(repo, "diff", "--raw", "--no-renames", "-z", left, right).stdout
    if not raw:
        return []
    tokens = raw.decode("utf-8", errors="strict").split("\0")
    rows: List[DiffRow] = []
    index = 0
    while index < len(tokens):
        header = tokens[index]
        index += 1
        if not header:
            continue
        if not header.startswith(":"):
            raise RenumberError(f"unexpected git diff --raw header: {header!r}")
        parts = header[1:].split(" ")
        if len(parts) != 5:
            raise RenumberError(f"malformed git diff --raw header: {header!r}")
        if index >= len(tokens):
            raise RenumberError("truncated git diff --raw output (missing path)")
        path = tokens[index]
        index += 1
        old_mode, new_mode, old_oid, new_oid, status = parts
        rows.append(
            DiffRow(
                old_mode=old_mode,
                new_mode=new_mode,
                old_oid=old_oid,
                new_oid=new_oid,
                status=status,
                path=path,
            )
        )
    return rows


def _tree_entry(repo: Path, commit: str, path: str) -> Optional[TreeEntry]:
    proc = _run_git(repo, "ls-tree", "-z", "--full-tree", commit, "--", path)
    out = proc.stdout
    if not out:
        return None
    record = out.split(b"\0", 1)[0].decode("utf-8", errors="strict")
    meta, _, record_path = record.partition("\t")
    if record_path != path:
        raise RenumberError(f"ls-tree returned {record_path!r} instead of {path!r}")
    mode, obj_type, oid = meta.split(" ")
    if obj_type != "blob":
        raise RenumberError(f"unsupported git object type {obj_type!r} at {commit[:12]}:{path}")
    return TreeEntry(mode=mode, oid=oid)


def _blob_bytes(repo: Path, oid: str) -> bytes:
    return _run_git(repo, "cat-file", "blob", oid).stdout


def _three_way_merge_blob(
    repo: Path,
    ours_oid: str,
    base_oid: str,
    theirs_oid: str,
) -> Optional[str]:
    """Return merged blob SHA iff git merge-file merges with zero conflicts."""
    with tempfile.TemporaryDirectory(prefix="renumber-merge-file-") as tmp:
        tmp_dir = Path(tmp)
        ours_path = tmp_dir / "ours"
        base_path = tmp_dir / "base"
        theirs_path = tmp_dir / "theirs"
        ours_path.write_bytes(_blob_bytes(repo, ours_oid))
        base_path.write_bytes(_blob_bytes(repo, base_oid))
        theirs_path.write_bytes(_blob_bytes(repo, theirs_oid))
        merge = subprocess.run(
            ["git", "merge-file", "-p", str(ours_path), str(base_path), str(theirs_path)],
            cwd=repo,
            capture_output=True,
            text=False,
        )
        if merge.returncode != 0:
            return None
        hashed = _run_git(repo, "hash-object", "--stdin", input_bytes=merge.stdout)
        return hashed.stdout.decode("utf-8", errors="strict").strip()


def _parse_migration_or_restore(path: str) -> Optional[Tuple[str, int, str]]:
    mig = MIGRATION_RE.fullmatch(path)
    if mig:
        return ("migration", int(mig.group("version")), mig.group("slug"))
    res = RESTORE_RE.fullmatch(path)
    if res:
        return ("restore", int(res.group("version")), res.group("slug"))
    return None


def _is_migration_area(path: str) -> bool:
    return path.startswith(f"{MIGRATIONS_DIR}/") or path.startswith(f"{RESTORES_DIR}/")


def _highest_real_migration_name(paths: Iterable[str]) -> Optional[str]:
    """Return the basename of the highest ``<NNNN>_<slug>.sql`` below 9000."""
    best: Optional[Tuple[int, str]] = None
    for path in paths:
        parsed = _parse_migration_or_restore(path)
        if parsed is None or parsed[0] != "migration":
            continue
        version = parsed[1]
        if version >= PLACEHOLDER_MIN:
            continue
        candidate = (version, path)
        if best is None or candidate > best:
            best = candidate
    return Path(best[1]).name if best else None


def _expected_latest_content(paths: Iterable[str]) -> Optional[str]:
    name = _highest_real_migration_name(paths)
    return f"{name}\n" if name is not None else None


def _paths_at(repo: Path, sha: str) -> List[str]:
    return _git_text(repo, "ls-tree", "--name-only", sha, f"{MIGRATIONS_DIR}/").splitlines()


def _migration_area_paths_at(repo: Path, sha: str) -> set[str]:
    """Return every migration and companion-restore path present at ``sha``."""
    paths: set[str] = set()
    for directory in (MIGRATIONS_DIR, RESTORES_DIR):
        paths.update(
            _git_text(repo, "ls-tree", "--name-only", sha, f"{directory}/").splitlines()
        )
    return paths


def _latest_pointer_error(repo: Path, sha: str) -> Optional[str]:
    """Return an error when ``sha``'s LATEST pointer is present but wrong."""
    entry = _tree_entry(repo, sha, LATEST_PATH)
    if entry is None:
        return None
    expected = _expected_latest_content(_paths_at(repo, sha))
    if expected is None:
        return f"{LATEST_PATH} exists but there is no real <NNNN>_<slug>.sql migration"
    actual = _blob_bytes(repo, entry.oid).decode("utf-8", errors="replace")
    if actual != expected:
        return (
            f"{LATEST_PATH} must be {expected.strip()!r} "
            f"(single line + newline), got {actual!r}"
        )
    return None


def _load_immutability_gate():
    gate_path = Path(__file__).resolve().with_name("migration_immutability_gate.py")
    spec = importlib.util.spec_from_file_location("migration_immutability_gate", gate_path)
    if spec is None or spec.loader is None:
        raise RenumberError(f"cannot load {gate_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_immutability_gate(repo: Path, base_sha: str, head_sha: str) -> List[str]:
    gate = _load_immutability_gate()
    # Use the same git inspection as the gate's own main(): head paths, LATEST
    # contents, first-parent duplicate history and companion restore renames.
    return gate.check(base_sha, head_sha, cwd=repo)


def _infer_bases_for_verify(
    repo: Path,
    reviewed_sha: str,
    new_sha: str,
    base_ref: Optional[str],
) -> Tuple[str, str]:
    """Return (old_base, new_base) covering both merge+renumber and pure-renumber cases."""
    merges_raw = _git_text(
        repo, "rev-list", "--merges", "--parents", f"{reviewed_sha}..{new_sha}"
    ).splitlines()
    second_parents: List[str] = []
    for line in merges_raw:
        fields = line.split()
        if len(fields) < 3:
            continue
        if len(fields) > 3:
            raise _TopologyMismatchError(
                f"octopus merge {fields[0][:12]} between {reviewed_sha[:12]} and {new_sha[:12]} is unsupported"
            )
        second_parents.append(fields[2])

    if base_ref is not None:
        new_base = _resolve_commit(repo, base_ref)
        if not _is_ancestor(repo, new_base, new_sha):
            new_base = _single_merge_base(repo, new_sha, new_base)
        for sp in second_parents:
            if not _is_ancestor(repo, sp, new_base):
                raise _TopologyMismatchError(
                    f"merge parent {sp[:12]} is not an ancestor of supplied base {new_base[:12]}"
                )
        old_base = _single_merge_base(repo, reviewed_sha, new_base)
        return old_base, new_base

    if second_parents:
        # rev-list outputs newest commit first; require all second parents to lie on new_base's history.
        new_base = second_parents[0]
        for sp in second_parents[1:]:
            if not _is_ancestor(repo, sp, new_base):
                raise _TopologyMismatchError(
                    f"multiple non-linear merge bases between {reviewed_sha[:12]} and {new_sha[:12]}"
                )
        old_base = _single_merge_base(repo, reviewed_sha, new_base)
        return old_base, new_base

    # Pure renumber commit(s) without merging a newer upstream tip.
    return reviewed_sha, reviewed_sha


def _verify_pure_renumber_without_merge(
    repo: Path,
    reviewed_sha: str,
    new_sha: str,
    gate_base: Optional[str] = None,
) -> VerificationResult:
    rows = _parse_raw_diff(repo, reviewed_sha, new_sha)
    if not rows:
        return VerificationResult(
            ok=False,
            reviewed_head=reviewed_sha,
            new_head=new_sha,
            old_base=reviewed_sha,
            new_base=reviewed_sha,
            upstream_commit_count=0,
            renames=(),
            errors=("no changes found between reviewed_head and new_head",),
        )

    errors: List[str] = []
    deleted: Dict[Tuple[str, str], DiffRow] = {}
    added: Dict[Tuple[str, str], DiffRow] = {}
    for row in rows:
        if row.path == LATEST_PATH:
            # The pointer is not a migration; its final content is verified below.
            if row.status not in ("A", "M"):
                errors.append(f"{LATEST_PATH} must only be added or modified, got {row.status}")
            continue
        if not _is_migration_area(row.path):
            errors.append(f"non-migration file modified: {row.status} {row.path}")
            continue
        parsed = _parse_migration_or_restore(row.path)
        if parsed is None:
            errors.append(f"unrecognized migration-area path: {row.status} {row.path}")
            continue
        kind, _version, slug = parsed
        key = (kind, slug)
        if row.status == "D":
            if row.old_mode not in REGULAR_MODES:
                errors.append(f"non-regular mode {row.old_mode} for deleted {row.path}")
            if key in deleted:
                errors.append(f"duplicate deleted {kind} slug: {slug}")
            deleted[key] = row
        elif row.status == "A":
            if row.new_mode not in REGULAR_MODES:
                errors.append(f"non-regular mode {row.new_mode} for added {row.path}")
            if key in added:
                errors.append(f"duplicate added {kind} slug: {slug}")
            added[key] = row
        else:
            errors.append(f"in-place modification is not a mechanical rename: {row.status} {row.path}")

    if set(deleted) != set(added):
        missing_add = sorted(set(deleted) - set(added))
        missing_del = sorted(set(added) - set(deleted))
        if missing_add:
            errors.append(f"deleted without matching renamed target: {missing_add}")
        if missing_del:
            errors.append(f"newly added instead of renamed from reviewed_head: {missing_del}")

    renames: List[RenameEntry] = []
    migration_pairs: Dict[str, Tuple[int, int]] = {}
    restore_pairs: Dict[str, Tuple[int, int]] = {}
    has_migration = False
    deleted_migration_paths = {row.path for (kind, _), row in deleted.items() if kind == "migration"}
    existing_migrations = _git_text(
        repo, "ls-tree", "--name-only", reviewed_sha, f"{MIGRATIONS_DIR}/"
    ).splitlines()
    other_versions = []
    for path in existing_migrations:
        if path in deleted_migration_paths:
            continue
        parsed = _parse_migration_or_restore(path)
        if (
            parsed
            and parsed[0] == "migration"
            and parsed[1] < PLACEHOLDER_MIN
        ):
            other_versions.append(parsed[1])
    other_max = max(other_versions, default=0)

    # Without an explicit base the reviewed tree is the only evidence that a
    # downward move is a duplicate repair: only a real number carried by more
    # than one migration being renamed may pack below its old number.  A unique
    # migration may never move backwards without a base (that could rewrite a
    # migration main already applied).
    deleted_real_version_counts: Dict[int, int] = {}
    for (kind, _slug), row in deleted.items():
        if kind != "migration":
            continue
        parsed = _parse_migration_or_restore(row.path)
        if parsed and parsed[1] < PLACEHOLDER_MIN:
            deleted_real_version_counts[parsed[1]] = (
                deleted_real_version_counts.get(parsed[1], 0) + 1
            )

    # Strict gate_base immutability: a pure renumber may only move files the PR
    # added, never a file already present on the base.
    base_migration_paths: set[str] = set()
    if gate_base is not None:
        base_migration_paths = _migration_area_paths_at(repo, gate_base)

    # A destination number may be unique only in the result tree.  When no base
    # is supplied the immutability gate below does not run, so a repair that
    # packs two migrations onto one number (or lands a rename on a number an
    # untouched migration already holds) would otherwise pass as mechanical.
    # Count every real migration on new_sha and refuse any number a renamed
    # migration lands on more than once, base or no base.
    new_head_real_paths_by_version: Dict[int, List[str]] = {}
    for path in _git_text(
        repo, "ls-tree", "--name-only", new_sha, f"{MIGRATIONS_DIR}/"
    ).splitlines():
        parsed = _parse_migration_or_restore(path)
        if parsed and parsed[0] == "migration" and parsed[1] < PLACEHOLDER_MIN:
            new_head_real_paths_by_version.setdefault(parsed[1], []).append(path)
    duplicate_new_versions: Dict[int, List[str]] = {
        version: sorted(paths)
        for version, paths in new_head_real_paths_by_version.items()
        if len(paths) > 1
    }
    reported_duplicate_versions: set[int] = set()

    for key in sorted(set(deleted) & set(added)):
        kind, slug = key
        old_row = deleted[key]
        new_row = added[key]
        old_parsed = _parse_migration_or_restore(old_row.path)
        new_parsed = _parse_migration_or_restore(new_row.path)
        assert old_parsed is not None and new_parsed is not None
        old_version = old_parsed[1]
        new_version = new_parsed[1]
        if old_row.old_oid != new_row.new_oid:
            errors.append(
                f"content changed across renumber ({kind} {slug}): "
                f"{old_row.path} ({old_row.old_oid[:12]}) != {new_row.path} ({new_row.new_oid[:12]})"
            )
        if old_row.old_mode != new_row.new_mode:
            errors.append(f"file mode changed for {new_row.path}: {old_row.old_mode} -> {new_row.new_mode}")
        on_base = gate_base is not None and old_row.path in base_migration_paths
        if on_base:
            errors.append(
                f"migration {old_row.path} already exists on the base "
                f"{gate_base[:12]}; a pure renumber may only rename migrations "
                "the PR added"
            )
        if PLACEHOLDER_MIN <= old_version <= PLACEHOLDER_MAX:
            # A placeholder may move to any free real number; it need not advance.
            pass
        elif new_version <= old_version and not on_base:
            if gate_base is not None:
                # A PR-added migration may pack below its old number: the
                # duplicate-number repair moves 0870_alpha/0870_beta to
                # 0864/0865.  The other_max check below keeps it above every
                # migration the reviewed tree keeps, and the gate proves the
                # result is above base_max, below the ceiling and unique.
                pass
            elif deleted_real_version_counts.get(old_version, 0) <= 1:
                errors.append(
                    f"renumbered version must advance ({new_row.path}: "
                    f"{old_version:04d} -> {new_version:04d})"
                )
        if new_version >= PLACEHOLDER_MIN:
            errors.append(
                f"renumbered version must stay below {PLACEHOLDER_MIN} "
                f"({new_row.path}: {old_version:04d} -> {new_version:04d})"
            )
        if kind == "migration":
            has_migration = True
            migration_pairs[slug] = (old_version, new_version)
            conflicts = duplicate_new_versions.get(new_version)
            if conflicts and new_version not in reported_duplicate_versions:
                reported_duplicate_versions.add(new_version)
                errors.append(
                    f"duplicate migration number {new_version:04d} on new head: "
                    f"renumbered migration {new_row.path} lands on a number shared by "
                    f"{', '.join(conflicts)}"
                )
            if new_version <= other_max:
                errors.append(
                    f"renumbered migration {new_row.path} ({new_version:04d}) is not above "
                    f"other migrations max ({other_max:04d})"
                )
        else:
            restore_pairs[slug] = (old_version, new_version)
        renames.append(
            RenameEntry(
                kind=kind,
                slug=slug,
                old_version=old_version,
                new_version=new_version,
                old_path=old_row.path,
                new_path=new_row.path,
                blob_oid=new_row.new_oid,
                byte_identical=(old_row.old_oid == new_row.new_oid),
            )
        )

    if not has_migration:
        errors.append("no renamed migration under hirey_core/migrations/ found")
    for slug, pair in restore_pairs.items():
        if migration_pairs.get(slug) != pair:
            errors.append(
                f"companion restore {slug} version transition {pair} does not match migration "
                f"{migration_pairs.get(slug)}"
            )

    latest_error = _latest_pointer_error(repo, new_sha)
    if latest_error:
        errors.append(latest_error)

    # --verify-only --base <base> bypasses renumber_branch's own gate call, so
    # the pure-renumber verifier runs the gate itself when a base is known.  A
    # placeholder that the PR added and left behind must fail closed;
    # placeholders already on base are stale history and stay allowed.  The
    # gate is diffed gate_base..new_sha (not reviewed_head..new_sha), so the
    # PR's own additions are judged against the real base tree.
    if gate_base is not None:
        head_migration_paths = _git_text(
            repo, "ls-tree", "--name-only", new_sha, f"{MIGRATIONS_DIR}/"
        ).splitlines()
        for path in head_migration_paths:
            if path in base_migration_paths:
                continue
            parsed = _parse_migration_or_restore(path)
            if parsed and parsed[0] == "migration" and parsed[1] >= PLACEHOLDER_MIN:
                errors.append(
                    f"placeholder migration {path} (number {parsed[1]:04d}) still remains on "
                    "the head; run 'python3 scripts/renumber_migrations.py' before merging."
                )
        # The immutability gate already owns Rules A-C (placeholder band, 8500
        # ceiling, LATEST) and refuses a head duplicate or a real number
        # <= base_max.  A stale 9xxx that was already on gate_base stays
        # ignored because base_max skips it.
        for violation in _run_immutability_gate(repo, gate_base, new_sha):
            errors.append(f"migration_immutability_gate: {violation}")

    return VerificationResult(
        ok=not errors,
        reviewed_head=reviewed_sha,
        new_head=new_sha,
        old_base=reviewed_sha,
        new_base=reviewed_sha,
        upstream_commit_count=0,
        renames=tuple(renames),
        errors=tuple(errors),
    )


def verify_mechanical_renumber(
    repo: Path,
    reviewed_head: str,
    new_head: str = "HEAD",
    base_ref: Optional[str] = None,
) -> VerificationResult:
    """Verify that new_head vs reviewed_head is strictly a clean base merge + R100 renames."""
    reviewed_sha = _resolve_commit(repo, reviewed_head)
    new_sha = _resolve_commit(repo, new_head)
    if reviewed_sha == new_sha:
        return VerificationResult(
            ok=False,
            reviewed_head=reviewed_sha,
            new_head=new_sha,
            old_base=reviewed_sha,
            new_base=reviewed_sha,
            upstream_commit_count=0,
            renames=(),
            errors=("reviewed_head and new_head resolve to the same commit",),
        )
    if not _is_ancestor(repo, reviewed_sha, new_sha):
        return VerificationResult(
            ok=False,
            reviewed_head=reviewed_sha,
            new_head=new_sha,
            old_base=reviewed_sha,
            new_base=reviewed_sha,
            upstream_commit_count=0,
            renames=(),
            errors=(f"reviewed_head {reviewed_sha[:12]} is not an ancestor of new_head {new_sha[:12]}",),
        )

    resolved_base = _resolve_commit(repo, base_ref) if base_ref is not None else None
    try:
        old_base, new_base = _infer_bases_for_verify(repo, reviewed_sha, new_sha, resolved_base)
    except _TopologyMismatchError as exc:
        fallback_base = resolved_base or reviewed_sha
        return VerificationResult(
            ok=False,
            reviewed_head=reviewed_sha,
            new_head=new_sha,
            old_base=reviewed_sha,
            new_base=fallback_base,
            upstream_commit_count=0,
            renames=(),
            errors=(str(exc),),
        )
    if old_base == new_base:
        # Only an explicitly resolved base may act as the immutability gate.  A
        # base_ref-less pure renumber infers reviewed_sha for both sides, and
        # passing that as gate_base would treat the PR's own tree as main.
        gate_base = (
            new_base
            if resolved_base is not None and new_base != reviewed_sha
            else None
        )
        return _verify_pure_renumber_without_merge(
            repo, reviewed_sha, new_sha, gate_base=gate_base
        )

    upstream_count = int(
        _git_text(repo, "rev-list", "--count", f"{old_base}..{new_base}").strip()
    )
    old_rows = _parse_raw_diff(repo, old_base, reviewed_sha)
    new_rows = _parse_raw_diff(repo, new_base, new_sha)

    old_mig: Dict[Tuple[str, str], DiffRow] = {}
    new_mig: Dict[Tuple[str, str], DiffRow] = {}
    old_other: Dict[str, DiffRow] = {}
    new_other: Dict[str, DiffRow] = {}
    errors: List[str] = []

    for label, rows, mig_map, other_map in (
        ("reviewed_head", old_rows, old_mig, old_other),
        ("new_head", new_rows, new_mig, new_other),
    ):
        for row in rows:
            if row.path == LATEST_PATH:
                # The pointer is intentionally excluded from the shared-file
                # 3-way check: renumber rewrites it from the final migration set.
                if row.status not in ("A", "M"):
                    errors.append(
                        f"{label}: {LATEST_PATH} must only be added or modified "
                        f"(got {row.status} {row.path})"
                    )
                continue
            if not _is_migration_area(row.path):
                other_map[row.path] = row
                continue
            parsed = _parse_migration_or_restore(row.path)
            if parsed is None:
                errors.append(f"{label}: unrecognized migration-area path {row.status} {row.path}")
                continue
            if row.status != "A":
                errors.append(
                    f"{label}: migration-area path must only be added (got {row.status} {row.path})"
                )
                continue
            if row.new_mode not in REGULAR_MODES:
                errors.append(f"{label}: non-regular mode {row.new_mode} on {row.path}")
                continue
            kind, _version, slug = parsed
            key = (kind, slug)
            if key in mig_map:
                errors.append(f"{label}: duplicate added {kind} slug {slug}")
                continue
            mig_map[key] = row

    if set(old_mig) != set(new_mig):
        missing = sorted(set(old_mig) - set(new_mig))
        extra = sorted(set(new_mig) - set(old_mig))
        if missing:
            errors.append(f"migrations/restores disappeared after renumber: {missing}")
        if extra:
            errors.append(f"unexpected migrations/restores introduced during renumber: {extra}")

    renames: List[RenameEntry] = []
    migration_pairs: Dict[str, Tuple[int, int]] = {}
    restore_pairs: Dict[str, Tuple[int, int]] = {}
    has_migration = False
    version_changed = False

    for key in sorted(set(old_mig) & set(new_mig)):
        kind, slug = key
        old_row = old_mig[key]
        new_row = new_mig[key]
        old_parsed = _parse_migration_or_restore(old_row.path)
        new_parsed = _parse_migration_or_restore(new_row.path)
        assert old_parsed is not None and new_parsed is not None
        old_version = old_parsed[1]
        new_version = new_parsed[1]
        if old_row.new_oid != new_row.new_oid:
            errors.append(
                f"content changed across renumber ({kind} {slug}): "
                f"{old_row.path} ({old_row.new_oid[:12]}) != {new_row.path} ({new_row.new_oid[:12]})"
            )
        if old_row.new_mode != new_row.new_mode:
            errors.append(
                f"file mode changed for {new_row.path}: {old_row.new_mode} -> {new_row.new_mode}"
            )
        # old_mig comes from _parse_raw_diff(old_base, reviewed_sha): every row
        # is a file the PR added, never a file already on new_base.  Packing a
        # PR's duplicate numbers down to new_base_max + 1..k is therefore a
        # legitimate repair even when new_version < old_version; the gate below
        # still proves every real number is above new_base's max, below the
        # ceiling and unique, and that LATEST names the highest one.
        if new_version >= PLACEHOLDER_MIN:
            errors.append(
                f"renumbered migration must stay below {PLACEHOLDER_MIN} "
                f"({slug}: {old_version:04d} -> {new_version:04d})"
            )
        if new_version != old_version:
            version_changed = True
        if kind == "migration":
            has_migration = True
            migration_pairs[slug] = (old_version, new_version)
        else:
            restore_pairs[slug] = (old_version, new_version)
        renames.append(
            RenameEntry(
                kind=kind,
                slug=slug,
                old_version=old_version,
                new_version=new_version,
                old_path=old_row.path,
                new_path=new_row.path,
                blob_oid=new_row.new_oid,
                byte_identical=(old_row.new_oid == new_row.new_oid),
            )
        )

    if not has_migration:
        errors.append("PR does not add any migration under hirey_core/migrations/")
    if not version_changed:
        errors.append("no migration or restore version changed between reviewed_head and new_head")
    for slug, pair in restore_pairs.items():
        if migration_pairs.get(slug) != pair:
            errors.append(
                f"companion restore {slug} version transition {pair} does not match migration "
                f"{migration_pairs.get(slug)}"
            )

    latest_error = _latest_pointer_error(repo, new_sha)
    if latest_error:
        errors.append(latest_error)

    gate_violations = _run_immutability_gate(repo, new_base, new_sha)
    for violation in gate_violations:
        errors.append(f"migration_immutability_gate: {violation}")

    # Verify every non-migration file touched by either PR diff matches either
    # reviewed_head (if upstream did not touch it), new_base (if PR did not touch it),
    # or the exact conflict-free 3-way merge of (reviewed_head, old_base, new_base).
    for path in sorted(set(old_other) | set(new_other)):
        e_old_b = _tree_entry(repo, old_base, path)
        e_rev = _tree_entry(repo, reviewed_sha, path)
        e_new_b = _tree_entry(repo, new_base, path)
        e_new = _tree_entry(repo, new_sha, path)

        if e_old_b == e_new_b:
            if e_new != e_rev:
                errors.append(f"non-migration file altered during renumber (upstream unchanged): {path}")
            continue

        if e_rev == e_old_b:
            if e_new != e_new_b:
                errors.append(f"upstream file change altered or reverted during merge: {path}")
            continue

        # Both PR and upstream modified `path`.
        if e_rev == e_new_b:
            if e_new != e_new_b:
                errors.append(f"convergent non-migration file altered during merge: {path}")
            continue

        if (
            e_old_b is None
            or e_rev is None
            or e_new_b is None
            or e_new is None
            or e_old_b.mode not in REGULAR_MODES
            or e_rev.mode not in REGULAR_MODES
            or e_new_b.mode not in REGULAR_MODES
            or e_new.mode not in REGULAR_MODES
        ):
            errors.append(
                f"non-trivial delete/add/mode divergence on shared file during merge: {path}"
            )
            continue

        expected_mode = e_rev.mode if e_rev.mode == e_new_b.mode or e_new_b.mode == e_old_b.mode else None
        if expected_mode is None or e_new.mode != expected_mode:
            errors.append(f"mode divergence on shared file during merge: {path}")
            continue

        merged_oid = _three_way_merge_blob(repo, e_rev.oid, e_old_b.oid, e_new_b.oid)
        if merged_oid is None:
            errors.append(
                f"shared file required manual conflict resolution during merge (not mechanical): {path}"
            )
            continue
        if e_new.oid != merged_oid:
            errors.append(
                f"shared file differs from conflict-free 3-way merge of base and reviewed_head: {path}"
            )

    return VerificationResult(
        ok=not errors,
        reviewed_head=reviewed_sha,
        new_head=new_sha,
        old_base=old_base,
        new_base=new_base,
        upstream_commit_count=upstream_count,
        renames=tuple(renames),
        errors=tuple(errors),
    )


def format_markdown_report(result: VerificationResult) -> str:
    """Format a verification summary suitable for pasting into a PR comment."""
    verdict = "PASS (MECHANICAL)" if result.ok else "FAIL (NON-MECHANICAL)"
    lines = [
        f"Mechanical Migration Renumber Check: {verdict}",
        f"- reviewed_head: `{result.reviewed_head}`",
        f"- new_head: `{result.new_head}`",
        f"- old_base: `{result.old_base}`",
        f"- new_base: `{result.new_base}` (upstream commits merged: {result.upstream_commit_count})",
    ]
    if result.renames:
        lines.append("- renames:")
        for item in result.renames:
            if not item.byte_identical:
                tag = "CHANGED"
            elif item.old_version != item.new_version:
                tag = "R100"
            else:
                tag = "UNCHANGED"
            lines.append(
                f"  - `{tag}` `{item.old_path}` -> `{item.new_path}` (`{item.blob_oid[:12]}`)"
            )
    if result.errors:
        lines.append("- reasons:")
        for err in result.errors:
            lines.append(f"  - {err}")
    return "\n".join(lines)


def _extract_write_set_boundary_trailer(commit_message: str) -> Optional[str]:
    found: List[str] = []
    for line in commit_message.splitlines():
        if not WRITE_SET_BOUNDARY_PREFIX.match(line):
            continue
        match = WRITE_SET_BOUNDARY_RE.fullmatch(line)
        if match is None:
            raise RenumberError(f"malformed Write-Set-Boundary trailer in HEAD: {line!r}")
        found.append(match.group(1))
    if not found:
        return None
    if len(set(found)) != 1:
        raise RenumberError(f"conflicting Write-Set-Boundary trailers in HEAD: {found}")
    return found[0]


def _base_max_migration_version(repo: Path, base_sha: str) -> int:
    paths = _git_text(repo, "ls-tree", "--name-only", base_sha, f"{MIGRATIONS_DIR}/").splitlines()
    versions: List[int] = []
    for path in paths:
        parsed = _parse_migration_or_restore(path)
        if parsed and parsed[0] == "migration" and parsed[1] < PLACEHOLDER_MIN:
            versions.append(parsed[1])
    return max(versions, default=0)


def renumber_branch(
    repo: Path,
    base_ref: str = "origin/main",
    fetch: bool = True,
) -> Tuple[int, Optional[VerificationResult]]:
    """Merge base_ref into HEAD if needed, renumber PR migrations to max+1..k, gate & verify."""
    status_out = _git_text(repo, "status", "--porcelain").strip()
    if status_out:
        raise RenumberError("working tree has uncommitted or untracked changes; commit or clean first")

    if fetch and base_ref.startswith("origin/"):
        branch = base_ref.split("/", 1)[1]
        _run_git(repo, "fetch", "origin", branch)

    reviewed_head = _resolve_commit(repo, "HEAD")
    base_sha = _resolve_commit(repo, base_ref)
    merge_base = _single_merge_base(repo, reviewed_head, base_sha)

    pr_rows = _parse_raw_diff(repo, merge_base, reviewed_head)
    pr_migrations: List[Tuple[int, str, str]] = []  # (version, slug, path)
    pr_restores: Dict[Tuple[str, int], str] = {}
    for row in pr_rows:
        if row.path == LATEST_PATH:
            # The pointer is derived from the final migration set; never treat it
            # as an unrecognized migration-area path.
            continue
        if not _is_migration_area(row.path):
            continue
        parsed = _parse_migration_or_restore(row.path)
        if parsed is None:
            raise RenumberError(f"unrecognized migration-area path in PR: {row.status} {row.path}")
        if row.status != "A":
            raise RenumberError(
                f"PR modified or deleted existing migration-area path ({row.status} {row.path}); "
                "refusing automatic renumber"
            )
        kind, version, slug = parsed
        if kind == "migration":
            pr_migrations.append((version, slug, row.path))
        else:
            key = (slug, version)
            if key in pr_restores:
                raise RenumberError(f"duplicate restore for {slug} ({version:04d})")
            pr_restores[key] = row.path

    if not pr_migrations:
        raise RenumberError(f"no new migrations found in {merge_base[:12]}..{reviewed_head[:12]}")

    pr_migrations.sort(
        key=lambda item: (0 if item[0] < PLACEHOLDER_MIN else 1, item[0], item[1])
    )
    mig_keys = {(slug, version) for version, slug, _ in pr_migrations}
    orphan_restores = sorted(set(pr_restores) - mig_keys)
    if orphan_restores:
        raise RenumberError(
            f"restore file(s) have no matching PR migration at the same version/slug: {orphan_restores}"
        )

    base_max = _base_max_migration_version(repo, base_sha)
    already_merged = _is_ancestor(repo, base_sha, reviewed_head)
    # A placeholder (9000..9999) is always above main's latest, so the old
    # `min(versions) <= base_max` test would miss it entirely.  A PR can also
    # add two files with the same real number: each is above base_max, but the
    # pair must still be renumbered to consecutive unique numbers.
    has_duplicate_pr_versions = len({version for version, _, _ in pr_migrations}) != len(pr_migrations)
    needs_renumber = has_duplicate_pr_versions or any(
        version >= PLACEHOLDER_MIN or version <= base_max
        for version, _, _ in pr_migrations
    )

    if already_merged and not needs_renumber:
        violations = _run_immutability_gate(repo, base_sha, reviewed_head)
        if violations:
            raise RenumberError("migration_immutability_gate failed: " + "; ".join(violations))
        print(
            f"No renumber needed: branch is already up to date with {base_sha[:12]} "
            f"(base max {base_max:04d}) and migration gate passed."
        )
        return 0, None

    # Assign contiguous numbers base_max + 1 .. base_max + k when renumbering is needed.
    # If all existing PR numbers are already strictly above base_max, keep them unchanged on merge.
    target_versions: List[Tuple[int, int, str, str]] = []  # (old_v, new_v, slug, old_mig_path)
    if needs_renumber:
        highest_target = base_max + len(pr_migrations)
        if highest_target > PLACEHOLDER_MAX:
            raise RenumberError(f"renumbered version {highest_target} exceeds 4-digit format")
        if highest_target >= CEILING_WARN:
            raise RenumberError(
                f"renumbering {len(pr_migrations)} migration(s) above main's latest "
                f"{base_max:04d} would reach {highest_target:04d}, at or above the "
                f"{CEILING_WARN} safety threshold"
            )
        for offset, (old_v, slug, old_mig_path) in enumerate(pr_migrations, start=1):
            new_v = base_max + offset
            target_versions.append((old_v, new_v, slug, old_mig_path))
    else:
        for old_v, slug, old_mig_path in pr_migrations:
            target_versions.append((old_v, old_v, slug, old_mig_path))

    trailer = _extract_write_set_boundary_trailer(_git_text(repo, "show", "-s", "--format=%B", reviewed_head))

    if not already_merged:
        merge_proc = _run_git(repo, "merge", "--no-commit", "--no-ff", base_sha, check=False)
        if merge_proc.returncode != 0:
            conflicts = _git_text(
                repo, "diff", "--name-only", "--diff-filter=U"
            ).splitlines()
            other_conflicts = [path for path in conflicts if path != LATEST_PATH]
            if not conflicts or other_conflicts:
                _run_git(repo, "merge", "--abort", check=False)
                stderr = merge_proc.stderr.decode("utf-8", errors="replace").strip()
                stdout = merge_proc.stdout.decode("utf-8", errors="replace").strip()
                raise RenumberError(
                    "merging base caused conflicts outside renumber; resolve merge manually first: "
                    + (stderr or stdout)
                )
            # Only the LATEST pointer conflicts.  It is derived from the final
            # migration set below, so resolve it now and rewrite it after the moves.
            _run_git(repo, "checkout", "--ours", "--", LATEST_PATH, check=False)
            _run_git(repo, "add", "-A", "--", LATEST_PATH)

    # Two-phase git mv avoids destination collisions when version ranges overlap (e.g. 0834,0835 -> 0835,0836).
    moves: List[Tuple[str, str]] = []
    for old_v, new_v, slug, old_mig_path in target_versions:
        if old_v == new_v:
            continue
        new_mig_path = f"{MIGRATIONS_DIR}/{new_v:04d}_{slug}.sql"
        moves.append((old_mig_path, new_mig_path))
        if (slug, old_v) in pr_restores:
            old_res_path = pr_restores[(slug, old_v)]
            new_res_path = f"{RESTORES_DIR}/{new_v:04d}_{slug}_restore.sql"
            moves.append((old_res_path, new_res_path))

    staged_temps: List[Tuple[str, str]] = []
    for index, (src, dst) in enumerate(moves):
        dst_path = repo / dst
        # Allow moving onto another source path that is also moving in this batch.
        moving_sources = {s for s, _ in moves}
        if dst_path.exists() and dst not in moving_sources:
            if not already_merged:
                _run_git(repo, "merge", "--abort", check=False)
            raise RenumberError(f"target path already exists on disk: {dst}")
        tmp_rel = f"{Path(src).parent.as_posix()}/.__renumber_tmp_{index}_{Path(src).name}"
        _run_git(repo, "mv", src, tmp_rel)
        staged_temps.append((tmp_rel, dst))

    for tmp_rel, dst in staged_temps:
        _run_git(repo, "mv", tmp_rel, dst)

    # The pointer is derived from the final working-tree migration set: the
    # highest real (< 9000) migration filename, single line + newline.
    working_paths = [
        f"{MIGRATIONS_DIR}/{path.name}"
        for path in sorted((repo / MIGRATIONS_DIR).glob("*.sql"))
    ]
    latest_name = _highest_real_migration_name(working_paths)
    if latest_name is None:
        raise RenumberError(f"no real <NNNN>_<slug>.sql migration found to write into {LATEST_PATH}")
    latest_file = repo / LATEST_PATH
    latest_file.parent.mkdir(parents=True, exist_ok=True)
    latest_file.write_text(f"{latest_name}\n", encoding="utf-8")
    _run_git(repo, "add", "--", LATEST_PATH)

    changed_pairs = [f"{old_v:04d}->{new_v:04d}" for old_v, new_v, _, _ in target_versions if old_v != new_v]
    if changed_pairs:
        subject = f"chore(migrations): renumber {', '.join(changed_pairs)} on {base_sha[:12]}"
    else:
        subject = f"chore(migrations): merge {base_sha[:12]} (migration numbers already above {base_max:04d})"
    message = subject if trailer is None else f"{subject}\n\nWrite-Set-Boundary: {trailer}"
    _run_git(repo, "commit", "-m", message)

    new_head = _resolve_commit(repo, "HEAD")
    try:
        gate_violations = _run_immutability_gate(repo, base_sha, new_head)
        if gate_violations:
            raise RenumberError(
                "migration_immutability_gate failed after renumber: "
                + "; ".join(gate_violations)
            )

        if changed_pairs:
            result = verify_mechanical_renumber(repo, reviewed_head, new_head, base_ref=base_sha)
            print(format_markdown_report(result))
            if not result.ok:
                raise RenumberError("post-renumber mechanical verification failed")
            return 0, result
    except BaseException:
        # A failed verification must never leave the generated commit on the
        # checked-out branch: restore the exact reviewed head before re-raising.
        _run_git(repo, "reset", "--hard", reviewed_head, check=False)
        raise

    print(
        f"Merged {base_sha[:12]} cleanly; existing migration numbers remain above {base_max:04d} "
        "and migration_immutability_gate passed."
    )
    return 0, None


def parse_prerelease_review_record(body: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a HIREY-PRERELEASE-REVIEW v1 header block, matching prerelease_review_gate.py."""
    lines = (body or "").strip().splitlines()
    if not lines:
        return None
    fields: Dict[str, Any] = {}
    conflicts = set()
    head_values: List[str] = []
    m_fence = REVIEW_FENCE_RE.match(lines[0].strip())
    if m_fence:
        fence = m_fence.group(1) or m_fence.group(2)
        fence_char, fence_len = fence[0], len(fence)
        idx = 1
        while idx < len(lines) and not lines[idx].strip():
            idx += 1
        if idx >= len(lines) or lines[idx].strip() != REVIEW_HEADER:
            return None
        end_idx = next(
            (
                i
                for i in range(idx + 1, len(lines))
                if len(lines[i].strip()) >= fence_len and set(lines[i].strip()) == {fence_char}
            ),
            None,
        )
        unclosed = end_idx is None
        raw_lines = lines[idx + 1 :] if unclosed else lines[idx + 1 : end_idx]
    else:
        if lines[0].strip() != REVIEW_HEADER:
            return None
        unclosed = False
        raw_lines = lines[1:]
    for line in raw_lines:
        if not line.strip():
            break
        if line.lstrip().startswith(("```", "~~~")):
            break
        if not REVIEW_RECORD_LINE_RE.match(line):
            break
        match = REVIEW_RECORD_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if key == "head_sha":
            head_values.append(value)
        if key in conflicts:
            continue
        if key in fields and (fields[key] != value or not value):
            fields[key] = ""
            conflicts.add(key)
        else:
            fields[key] = value
    if "head_sha" in conflicts:
        fields["head_shas"] = tuple(
            dict.fromkeys(v if SHA40_RE.fullmatch(v) else "INVALID" for v in head_values)
        )
    if unclosed:
        fields["head_sha"] = ""
        fields["verdict"] = ""
        fields.pop("head_shas", None)
    return fields


def _checks_in(record: Dict[str, Any]) -> set[str]:
    return {c.strip() for c in (record.get("checks") or "").split(",") if c.strip()}


def github_api_request(
    path: str,
    *,
    method: str = "GET",
    data: Optional[Dict[str, Any]] = None,
) -> Any:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise RenumberError("GH_TOKEN or GITHUB_TOKEN is required for GitHub API requests")
    body_bytes = None if data is None else json.dumps(data).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body_bytes is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"https://api.github.com/{path.lstrip('/')}",
        data=body_bytes,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=GITHUB_API_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-300:]
        raise RenumberError(f"GitHub API {method} {path} failed ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise RenumberError(f"GitHub API {method} {path} failed: {exc.reason}") from exc


def _list_pr_comments(api: Callable[..., Any], github_repo: str, pr_number: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        batch = api(f"repos/{github_repo}/issues/{pr_number}/comments?per_page={GITHUB_PAGE_SIZE}&page={page}")
        out.extend(batch)
        if len(batch) < GITHUB_PAGE_SIZE:
            return out
        page += 1


def _can_write_repo(
    api: Callable[..., Any],
    github_repo: str,
    login: str,
    cache: Dict[str, bool],
) -> bool:
    if not login or login.endswith("[bot]"):
        return False
    if login not in cache:
        encoded_login = urllib.parse.quote(login, safe="")
        perm = api(f"repos/{github_repo}/collaborators/{encoded_login}/permission").get("permission")
        cache[login] = perm in WRITERS
    return cache[login]


def find_prior_writer_full_pass(
    comments: List[Dict[str, Any]],
    api: Callable[..., Any],
    github_repo: str,
    head_sha: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Return the latest valid human-writer PASS on an earlier head if head_sha needs carry-over."""
    writers: Dict[str, bool] = {}
    on_head: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    latest_writer: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
    latest_writer_malformed = False

    for comment in sorted(comments, key=lambda item: int(item.get("id", 0))):
        record = parse_prerelease_review_record(comment.get("body"))
        if record is None:
            continue
        login = (comment.get("user") or {}).get("login") or ""
        if login == BOT_LOGIN:
            if record.get("head_sha") == head_sha:
                on_head.append((comment, record))
            continue
        if not _can_write_repo(api, github_repo, login, writers):
            continue

        head_val = record.get("head_sha", "")
        conflicting = record.get("head_shas")
        if head_val == head_sha:
            on_head.append((comment, record))
            latest_writer = (comment, record)
            latest_writer_malformed = (
                record.get("verdict") not in REVIEW_VERDICTS or not record.get("reviewer")
            )
            continue
        if conflicting:
            latest_writer = (comment, record)
            latest_writer_malformed = True
            if head_sha in conflicting or "INVALID" in conflicting:
                on_head.append((comment, record))
            continue
        if not SHA40_RE.fullmatch(head_val):
            latest_writer = (comment, record)
            latest_writer_malformed = True
            on_head.append((comment, record))
            continue

        latest_writer = (comment, record)
        latest_writer_malformed = (
            record.get("verdict") not in REVIEW_VERDICTS or not record.get("reviewer")
        )

    if on_head:
        return None, f"head {head_sha[:12]} already has a review record; skipping automatic carry-over"
    if latest_writer is None:
        return None, "no prior writer HIREY-PRERELEASE-REVIEW v1 record found on PR"
    if latest_writer_malformed:
        return None, "latest writer HIREY-PRERELEASE-REVIEW v1 record on PR is malformed"
    prior_comment, prior_record = latest_writer
    if prior_record.get("verdict") != "PASS":
        return (
            None,
            f"latest writer review verdict on PR is {prior_record.get('verdict')}; skipping automatic carry-over",
        )
    if "cross-repo" not in _checks_in(prior_record):
        return (
            None,
            f"prior writer PASS on {prior_record['head_sha'][:12]} omits cross-repo in checks; skipping automatic carry-over",
        )
    return {
        "head_sha": prior_record["head_sha"],
        "comment": prior_comment,
        "record": prior_record,
    }, ""


def format_mechanical_review_comment(
    head_sha: str,
    prior_info: Dict[str, Any],
    result: VerificationResult,
) -> str:
    """Format a Mechanical new head HIREY-PRERELEASE-REVIEW v1 receipt comment for CI posting."""
    prior_head = prior_info["head_sha"]
    prior_record = prior_info["record"]
    prior_comment = prior_info["comment"]
    prior_reviewer = (prior_record.get("reviewer") or "").strip()
    reviewer = f"{prior_reviewer} (mechanical new head from {prior_head[:12]} verified by CI)"
    checks = (prior_record.get("checks") or "cross-repo").strip()
    tool_suite = (prior_record.get("tool_suite") or "not-applicable").strip()
    agent_eval = (prior_record.get("agent_eval") or "not-applicable").strip()
    prior_ref = prior_comment.get("html_url") or f"`{prior_head}`"
    return "\n".join(
        [
            REVIEW_HEADER,
            f"head_sha: {head_sha}",
            "verdict: PASS",
            f"reviewer: {reviewer}",
            f"checks: {checks}",
            f"tool_suite: {tool_suite}",
            f"agent_eval: {agent_eval}",
            "findings: none",
            "",
            (
                f"Mechanical new head carry-over from `{prior_head}` ({prior_ref}), "
                "verified and posted in CI by `scripts/renumber_migrations.py --ci-post-mechanical-review`:"
            ),
            "",
            format_markdown_report(result),
        ]
    )


def ci_post_mechanical_review(
    repo: Path,
    *,
    github_repo: str,
    pr_number: int,
    head_sha: str,
    base_ref: str = "origin/main",
    api: Optional[Callable[..., Any]] = None,
    allow_git_fetch: bool = True,
) -> Tuple[bool, str]:
    """Verify head_sha is a mechanical renumber of an earlier writer PASS and post CI receipt."""
    if not SHA40_RE.fullmatch(head_sha):
        raise RenumberError(f"--head must be a full 40-char SHA, got {head_sha!r}")
    client = github_api_request if api is None else api
    pr_comments = _list_pr_comments(client, github_repo, pr_number)
    prior_info, skip_reason = find_prior_writer_full_pass(pr_comments, client, github_repo, head_sha)
    if prior_info is None:
        return False, skip_reason

    prior_head = prior_info["head_sha"]
    have_prior = _run_git(repo, "cat-file", "-e", f"{prior_head}^{{commit}}", check=False)
    if have_prior.returncode != 0 and allow_git_fetch:
        _run_git(repo, "fetch", "--no-tags", "origin", prior_head, check=False)
        have_prior = _run_git(repo, "cat-file", "-e", f"{prior_head}^{{commit}}", check=False)
    if have_prior.returncode != 0:
        return (
            False,
            f"prior reviewed head {prior_head[:12]} is not present in git history; skipping automatic carry-over",
        )

    result = verify_mechanical_renumber(
        repo,
        reviewed_head=prior_head,
        new_head=head_sha,
        base_ref=base_ref,
    )
    if not result.ok:
        return (
            False,
            f"delta {prior_head[:12]} -> {head_sha[:12]} is not mechanical: {'; '.join(result.errors)}",
        )

    body = format_mechanical_review_comment(head_sha, prior_info, result)
    client(
        f"repos/{github_repo}/issues/{pr_number}/comments",
        method="POST",
        data={"body": body},
    )
    return True, f"posted Mechanical new head PASS for {head_sha[:12]} carried over from {prior_head[:12]}"


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="repository path (default: cwd)")
    parser.add_argument(
        "--base",
        default=None,
        help="base ref or SHA (default: origin/main in renumber mode; inferred in --verify-only)",
    )
    parser.add_argument("--no-fetch", action="store_true", help="skip fetching origin/<branch>")
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify mechanical renumber between --reviewed-head and --new-head without editing git state",
    )
    parser.add_argument("--reviewed-head", help="previously reviewed 40-char/ref head for --verify-only")
    parser.add_argument("--new-head", default="HEAD", help="new head ref/SHA for --verify-only (default: HEAD)")
    parser.add_argument(
        "--verify-base",
        help="optional explicit new base ref/SHA alias for --verify-only",
    )
    parser.add_argument(
        "--ci-post-mechanical-review",
        action="store_true",
        help="in CI, post a Mechanical new head HIREY-PRERELEASE-REVIEW v1 PASS comment when verified",
    )
    parser.add_argument("--github-repo", help="owner/repo slug for --ci-post-mechanical-review")
    parser.add_argument("--pr", type=int, help="PR number for --ci-post-mechanical-review")
    parser.add_argument("--head", help="PR head 40-char SHA for --ci-post-mechanical-review")
    args = parser.parse_args(list(argv) if argv is not None else None)
    repo = args.repo.resolve()

    try:
        if args.ci_post_mechanical_review:
            if not args.github_repo or not args.pr or not args.head:
                raise RenumberError("--github-repo, --pr, and --head are required with --ci-post-mechanical-review")
            posted, detail = ci_post_mechanical_review(
                repo,
                github_repo=args.github_repo,
                pr_number=args.pr,
                head_sha=args.head,
                base_ref=args.base or "origin/main",
                allow_git_fetch=not args.no_fetch,
            )
            print(detail)
            return 0

        if args.verify_only:
            if not args.reviewed_head:
                raise RenumberError("--reviewed-head is required with --verify-only")
            result = verify_mechanical_renumber(
                repo,
                reviewed_head=args.reviewed_head,
                new_head=args.new_head,
                base_ref=args.verify_base or args.base,
            )
            print(format_markdown_report(result))
            return 0 if result.ok else 1

        rc, _ = renumber_branch(repo, base_ref=args.base or "origin/main", fetch=not args.no_fetch)
        return rc
    except RenumberError as exc:
        print(f"RENUMBER ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
