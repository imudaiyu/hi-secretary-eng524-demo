#!/usr/bin/env python3
"""Migration safety gate (ENG-537): post-lock rules + contraction (L2a) gate.

Enforces two blocking rules on added or modified `.sql` migrations under
`hirey_core/migrations/` (evaluated on PR changes, without rewriting history):

1. Post-lock no-index / no-backfill rule:
   Once a migration acquires a table lock on an existing table (either via
   `LOCK [TABLE] ...` or `ALTER TABLE ...` on a table not created in the same
   migration), subsequent statements in that migration must not run
   `CREATE [UNIQUE] INDEX ... ON <tbl>` against that locked existing table.
   When the lock blocks concurrent reads or DML (`ALTER TABLE`, or
   `LOCK TABLE` without a non-DML-blocking mode such as `ACCESS EXCLUSIVE`,
   `EXCLUSIVE`, `SHARE ROW EXCLUSIVE`, or `SHARE`), subsequent `UPDATE [ONLY]
   <tbl>` backfills (including `WITH ... UPDATE [ONLY] <tbl>`) are also
   blocked; non-DML-blocking modes (`SHARE UPDATE EXCLUSIVE`, `ROW EXCLUSIVE`,
   `ROW SHARE`, `ACCESS SHARE`) allow `UPDATE` backfills when the table was
   not `ALTER TABLE`d. During an `ALTER TABLE` lock window, only metadata-only
   changes (such as `ADD COLUMN`) are permitted; index builds and data
   backfills must live in separate migrations. New tables (or TEMP tables)
   created earlier in the same migration are exempt.

2. Contraction migration gate (L2a):
   Contraction DDL on existing schema objects (`DROP TABLE`, `DROP VIEW`,
   `DROP MATERIALIZED VIEW`, `DROP INDEX`, `ALTER TABLE <existing> DROP
   [COLUMN|CONSTRAINT]`, `RENAME` of table/column/constraint, `ALTER COLUMN ...
   TYPE|SET NOT NULL`, and `ADD CONSTRAINT (CHECK|UNIQUE|FOREIGN KEY)` on an
   existing table; `DROP FUNCTION` is excluded) is blocked unless:
     a) Both `Contraction-Of:` and `Consumers-Cleared:` are non-empty,
        non-placeholder entries in the migration SQL header comments or PR body,
        AND
     b) An automated runtime consumer scan across `hirey_core/` (excluding
        `migrations/`, `migrations_restore/`, and test files) finds zero
        unresolved references to the contracted object.

Usage:
    python3 scripts/migration_safety_gate.py --base <base-sha> --head <head-sha>
    python3 scripts/migration_safety_gate.py --file <migration.sql> [--pr-body-file <md>] [--repo-root <dir>]

Exit codes: 0 pass, 1 gate violation, 2 unreadable input / git error.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tokenize
from typing import Iterable, Sequence

MIGRATIONS_PREFIX = "hirey_core/migrations/"

_HEADER_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:[*_`~]+\s*)?(Contraction-Of|Consumers-Cleared)\s*(?:[*_`~]+\s*)?:\s*(?:[*_`~]+\s*)?(.*)$",
    re.IGNORECASE,
)
_BARE_PLACEHOLDERS = frozenset({
    "",
    "-",
    "--",
    ".",
    "?",
    "n/a",
    "na",
    "none",
    "no",
    "todo",
    "tbd",
    "tba",
    "pending",
    "fill me",
    "not applicable",
})
_CONSUMER_SCAN_KINDS = frozenset({"table", "view", "index", "constraint", "column"})

_PLPGSQL_PREFIX_STARTERS = frozenset({
    "DECLARE",
    "BEGIN",
    "LOOP",
    "IF",
    "ELSIF",
    "ELSE",
    "CASE",
    "WHEN",
    "EXCEPTION",
    "FOR",
    "FOREACH",
    "WHILE",
})
_SQL_DIRECT_VERBS = frozenset({
    "ALTER",
    "COMMENT",
    "CONTINUE",
    "COPY",
    "CREATE",
    "DEALLOCATE",
    "DELETE",
    "DO",
    "DROP",
    "END",
    "EXECUTE",
    "EXIT",
    "GRANT",
    "INSERT",
    "LOCK",
    "MERGE",
    "PERFORM",
    "PREPARE",
    "RAISE",
    "REVOKE",
    "RETURN",
    "SELECT",
    "SET",
    "TRUNCATE",
    "UPDATE",
    "WITH",
})
_KNOWN_BENIGN_PLPGSQL_VERBS = frozenset({
    "ASSERT",
    "CALL",
    "CLOSE",
    "COMMIT",
    "FETCH",
    "GET",
    "MOVE",
    "NULL",
    "OPEN",
    "RELEASE",
    "ROLLBACK",
    "SAVEPOINT",
})

_DOLLAR_TAG_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")
_TABLE_KINDS = frozenset({"table", "view", "index", "constraint"})


class GateInputError(RuntimeError):
    """Raised when git/file/repo inputs cannot be read reliably (fail closed)."""


@dataclass(frozen=True)
class Token:
    kind: str  # WORD, QIDENT, STRING, DOLLAR, PUNCT
    value: str
    upper: str
    line: int


@dataclass(frozen=True)
class Statement:
    tokens: tuple[Token, ...]
    line: int
    unverified_reason: str = ""
    conditional: bool = False


@dataclass(frozen=True)
class SchemaInventory:
    tables: frozenset[tuple[str, str]]
    views: frozenset[tuple[str, str]]
    indexes: frozenset[tuple[str, str]]


@dataclass(frozen=True)
class LockInfo:
    line: int
    reason: str
    blocks_dml: bool = True


@dataclass(frozen=True)
class ContractionOp:
    kind: str  # table, view, index, constraint, column, column_type, column_not_null, add_constraint
    table_key: tuple[str, str]
    table_bare: str
    identifier: str
    line: int
    detail: str


@dataclass(frozen=True)
class ConsumerMatch:
    path: str
    line: int
    snippet: str


# ---------------------------------------------------------------------------
# SQL Lexing & Statement Splitting (pure stdlib, PostgreSQL-faithful)


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _decode_unicode_escapes(raw: str, esc: str) -> str | None:
    """Decode PostgreSQL `U&` string/identifier Unicode escapes under `esc`."""
    if len(esc) != 1 or esc in _HEX_DIGITS or esc in "+'\"" or esc.isspace():
        return None
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch != esc:
            out.append(ch)
            i += 1
            continue
        if i + 1 >= n:
            return None
        nxt = raw[i + 1]
        if nxt == esc:
            out.append(esc)
            i += 2
            continue
        if nxt == "+":
            if i + 8 > n or any(c not in _HEX_DIGITS for c in raw[i + 2:i + 8]):
                return None
            cp = int(raw[i + 2:i + 8], 16)
            if cp == 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                return None
            out.append(chr(cp))
            i += 8
            continue
        if i + 5 > n or any(c not in _HEX_DIGITS for c in raw[i + 1:i + 5]):
            return None
        cp = int(raw[i + 1:i + 5], 16)
        if 0xD800 <= cp <= 0xDBFF:
            if (
                i + 10 <= n
                and raw[i + 5] == esc
                and all(c in _HEX_DIGITS for c in raw[i + 6:i + 10])
            ):
                cp2 = int(raw[i + 6:i + 10], 16)
                if 0xDC00 <= cp2 <= 0xDFFF:
                    cp = 0x10000 + ((cp - 0xD800) << 10) + (cp2 - 0xDC00)
                    out.append(chr(cp))
                    i += 10
                    continue
            return None
        if cp == 0 or 0xDC00 <= cp <= 0xDFFF:
            return None
        out.append(chr(cp))
        i += 5
    return "".join(out)


def _resolve_unicode_escapes(tokens: list[Token]) -> list[Token]:
    """Resolve `U&'...' [UESCAPE 'x']` and `U&\"...\" [UESCAPE 'x']` tokens."""
    resolved: list[Token] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok.kind not in {"USTRING", "UQIDENT"}:
            resolved.append(tok)
            i += 1
            continue
        esc = "\\"
        invalid_uescape = False
        if (
            i + 1 < n
            and tokens[i + 1].kind == "WORD"
            and tokens[i + 1].upper == "UESCAPE"
        ):
            if i + 2 < n and tokens[i + 2].kind == "STRING":
                esc = tokens[i + 2].value
                i += 3
            else:
                invalid_uescape = True
                i += 2
        else:
            i += 1
        decoded = None if invalid_uescape else _decode_unicode_escapes(tok.value, esc)
        if decoded is None:
            kind = "UNSUPPORTED_STRING" if tok.kind == "USTRING" else "INVALID_IDENT"
            resolved.append(Token(kind, tok.value, "", tok.line))
        elif tok.kind == "USTRING":
            resolved.append(Token("STRING", decoded, "", tok.line))
        else:
            resolved.append(Token("QIDENT", decoded, decoded.upper(), tok.line))
    return resolved


def _tokenize_sql(text: str, start_line: int = 1) -> tuple[list[Token], list[str]]:
    """Tokenize PostgreSQL SQL while preserving exact line numbers and comments."""
    tokens: list[Token] = []
    comments: list[str] = []
    i, n = 0, len(text)
    line = start_line

    while i < n:
        ch = text[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue

        # Line comment: -- ...
        if ch == "-" and text.startswith("--", i):
            j = text.find("\n", i)
            if j < 0:
                comments.append(text[i + 2:])
                i = n
            else:
                comments.append(text[i + 2:j])
                line += 1
                i = j + 1
            continue

        # Nested block comment: /* ... */
        if ch == "/" and text.startswith("/*", i):
            depth = 1
            start = i + 2
            i += 2
            while i < n and depth > 0:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    if depth == 0:
                        comments.append(text[start:i])
                    i += 2
                else:
                    if text[i] == "\n":
                        line += 1
                    i += 1
            if depth > 0:
                comments.append(text[start:n])
            continue

        # Single-quoted string literal ('...' or E'...' or U&'...')
        if ch == "'":
            backslash = (
                bool(tokens)
                and tokens[-1].kind == "WORD"
                and tokens[-1].upper == "E"
                and i > 0
                and text[i - 1] in "eE"
            )
            unicode_str = (
                len(tokens) >= 2
                and tokens[-2].kind == "WORD"
                and tokens[-2].upper == "U"
                and tokens[-1].kind == "PUNCT"
                and tokens[-1].value == "&"
                and i >= 2
                and text[i - 2:i].upper() == "U&"
            )
            if backslash:
                tokens.pop()
            elif unicode_str:
                tokens.pop()
                tokens.pop()
            tok_line = line
            i += 1
            if backslash:
                byte_buf = bytearray()
                invalid_escape = False
                while i < n:
                    cur = text[i]
                    if cur == "\n":
                        line += 1
                    if cur == "\\" and i + 1 < n:
                        nxt = text[i + 1]
                        if nxt == "\n":
                            line += 1
                            byte_buf.append(ord("\n"))
                            i += 2
                        elif nxt in "bfrnt":
                            byte_buf.append({"b": 8, "f": 12, "n": 10, "r": 13, "t": 9}[nxt])
                            i += 2
                        elif nxt == "x":
                            if i + 2 < n and text[i + 2] in _HEX_DIGITS:
                                end_h = i + 3
                                if end_h < n and text[end_h] in _HEX_DIGITS:
                                    end_h += 1
                                byte_buf.append(int(text[i + 2:end_h], 16))
                                i = end_h
                            else:
                                invalid_escape = True
                                i += 2
                        elif nxt == "u":
                            if i + 6 <= n and all(c in _HEX_DIGITS for c in text[i + 2:i + 6]):
                                cp = int(text[i + 2:i + 6], 16)
                                if 0xD800 <= cp <= 0xDBFF:
                                    if (
                                        i + 12 <= n
                                        and text[i + 6:i + 8] == "\\u"
                                        and all(c in _HEX_DIGITS for c in text[i + 8:i + 12])
                                    ):
                                        cp2 = int(text[i + 8:i + 12], 16)
                                        if 0xDC00 <= cp2 <= 0xDFFF:
                                            cp = 0x10000 + ((cp - 0xD800) << 10) + (cp2 - 0xDC00)
                                            byte_buf.extend(chr(cp).encode("utf-8"))
                                            i += 12
                                            continue
                                    invalid_escape = True
                                    i += 6
                                elif cp == 0 or 0xDC00 <= cp <= 0xDFFF:
                                    invalid_escape = True
                                    i += 6
                                else:
                                    byte_buf.extend(chr(cp).encode("utf-8"))
                                    i += 6
                            else:
                                invalid_escape = True
                                i += 2
                        elif nxt == "U":
                            if i + 10 <= n and all(c in _HEX_DIGITS for c in text[i + 2:i + 10]):
                                cp = int(text[i + 2:i + 10], 16)
                                if cp == 0 or cp > 0x10FFFF or 0xD800 <= cp <= 0xDFFF:
                                    invalid_escape = True
                                else:
                                    byte_buf.extend(chr(cp).encode("utf-8"))
                                i += 10
                            else:
                                invalid_escape = True
                                i += 2
                        elif nxt in "01234567":
                            end_o = i + 2
                            while end_o < min(n, i + 4) and text[end_o] in "01234567":
                                end_o += 1
                            byte_buf.append(int(text[i + 1:end_o], 8) & 0xFF)
                            i = end_o
                        else:
                            byte_buf.extend(nxt.encode("utf-8"))
                            i += 2
                        continue
                    if cur == "'":
                        if text.startswith("''", i):
                            byte_buf.append(ord("'"))
                            i += 2
                            continue
                        i += 1
                        break
                    byte_buf.extend(cur.encode("utf-8"))
                    i += 1
                if invalid_escape or 0 in byte_buf:
                    tokens.append(Token("UNSUPPORTED_STRING", "", "", tok_line))
                else:
                    try:
                        decoded = byte_buf.decode("utf-8")
                    except UnicodeDecodeError:
                        tokens.append(Token("UNSUPPORTED_STRING", "", "", tok_line))
                    else:
                        tokens.append(Token("STRING", decoded, "", tok_line))
                continue

            buf: list[str] = []
            while i < n:
                cur = text[i]
                if cur == "\n":
                    line += 1
                if cur == "'":
                    if text.startswith("''", i):
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(cur)
                i += 1
            kind = "USTRING" if unicode_str else "STRING"
            tokens.append(Token(kind, "".join(buf), "", tok_line))
            continue

        # Double-quoted identifier ("..." or U&"...")
        if ch == '"':
            unicode_ident = (
                len(tokens) >= 2
                and tokens[-2].kind == "WORD"
                and tokens[-2].upper == "U"
                and tokens[-1].kind == "PUNCT"
                and tokens[-1].value == "&"
                and i >= 2
                and text[i - 2:i].upper() == "U&"
            )
            if unicode_ident:
                tokens.pop()
                tokens.pop()
            tok_line = line
            i += 1
            buf = []
            while i < n:
                cur = text[i]
                if cur == "\n":
                    line += 1
                if cur == '"':
                    if text.startswith('""', i):
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(cur)
                i += 1
            ident = "".join(buf)
            if unicode_ident:
                tokens.append(Token("UQIDENT", ident, "", tok_line))
            else:
                tokens.append(Token("QIDENT", ident, ident.upper(), tok_line))
            continue

        # Dollar-quoted string / DO block: $tag$...$tag$
        if ch == "$":
            m = _DOLLAR_TAG_RE.match(text, i)
            prev = text[i - 1] if i > 0 else " "
            if m and not (prev.isalnum() or prev in "_$"):
                tag = m.group(0)
                tok_line = line
                body_start = m.end()
                end = text.find(tag, body_start)
                if end < 0:
                    body = text[body_start:n]
                    line += body.count("\n")
                    i = n
                else:
                    body = text[body_start:end]
                    line += body.count("\n")
                    i = end + len(tag)
                tokens.append(Token("DOLLAR", body, "", tok_line))
                continue

        # Word / unquoted identifier / keyword
        if ch.isalpha() or ch == "_":
            tok_line = line
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "_$"):
                j += 1
            word = text[i:j]
            tokens.append(Token("WORD", word, word.upper(), tok_line))
            i = j
            continue

        # Numeric literal or operator/punctuation
        if ch.isdigit():
            tok_line = line
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "._"):
                j += 1
            num = text[i:j]
            tokens.append(Token("STRING", num, "", tok_line))
            i = j
            continue

        tokens.append(Token("PUNCT", ch, ch, line))
        i += 1

    return _resolve_unicode_escapes(tokens), comments


def _split_token_statements(tokens: Sequence[Token]) -> list[Statement]:
    """Split a flat token stream on top-level semicolons (ignoring parens)."""
    stmts: list[Statement] = []
    cur: list[Token] = []
    depth = 0
    for tok in tokens:
        if tok.kind == "PUNCT":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")" and depth > 0:
                depth -= 1
            elif tok.value == ";" and depth == 0:
                if cur:
                    stmts.append(Statement(tuple(cur), cur[0].line))
                    cur = []
                continue
        cur.append(tok)
    if cur:
        stmts.append(Statement(tuple(cur), cur[0].line))
    return stmts


def _is_ident(tok: Token) -> bool:
    return tok.kind in {"WORD", "QIDENT"}


def _is_plpgsql_assignment(tokens: Sequence[Token]) -> bool:
    """Return True for `var[.field][idx] := expr` or `var = expr` inside DO blocks."""
    if not tokens or not _is_ident(tokens[0]):
        return False
    if tokens[0].kind == "WORD" and tokens[0].upper in (_PLPGSQL_PREFIX_STARTERS | _SQL_DIRECT_VERBS):
        return False
    idx = 1
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok.kind == "PUNCT" and tok.value == "." and idx + 1 < n and _is_ident(tokens[idx + 1]):
            idx += 2
            continue
        if tok.kind == "PUNCT" and tok.value == "[":
            depth = 1
            idx += 1
            while idx < n and depth > 0:
                if tokens[idx].kind == "PUNCT":
                    if tokens[idx].value == "[":
                        depth += 1
                    elif tokens[idx].value == "]":
                        depth -= 1
                idx += 1
            continue
        break
    if idx + 1 < n and tokens[idx].kind == "PUNCT" and tokens[idx].value == ":" and tokens[idx + 1].kind == "PUNCT" and tokens[idx + 1].value == "=":
        return True
    if idx < n and tokens[idx].kind == "PUNCT" and tokens[idx].value == "=":
        return True
    return False


_FOR_HEADER_QUERY_STARTERS = frozenset({
    "UPDATE",
    "WITH",
    "EXECUTE",
    "SELECT",
    "INSERT",
    "DELETE",
    "MERGE",
})


def _strip_wrapping_parens(tokens: Sequence[Token]) -> Sequence[Token]:
    """Strip balanced outer parentheses wrapping the entire token slice."""
    cur = tokens
    while (
        len(cur) >= 2
        and cur[0].kind == "PUNCT"
        and cur[0].value == "("
        and cur[-1].kind == "PUNCT"
        and cur[-1].value == ")"
    ):
        depth = 0
        wraps_whole = True
        for idx, tok in enumerate(cur):
            if tok.kind == "PUNCT":
                if tok.value == "(":
                    depth += 1
                elif tok.value == ")":
                    depth -= 1
                    if depth == 0 and idx < len(cur) - 1:
                        wraps_whole = False
                        break
        if not wraps_whole or depth != 0:
            break
        cur = cur[1:-1]
    return cur


def _extract_for_header_query(header: Sequence[Token]) -> Sequence[Token] | None:
    """Extract executable query or `EXECUTE ...` following `FOR <target> IN`."""
    in_idx: int | None = None
    depth = 0
    for idx, tok in enumerate(header):
        if tok.kind == "PUNCT":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")" and depth > 0:
                depth -= 1
        elif depth == 0 and tok.kind == "WORD" and tok.upper == "IN":
            in_idx = idx
            break
    if in_idx is None:
        return None
    rhs = _strip_wrapping_parens(header[in_idx + 1:])
    if rhs and rhs[0].kind == "WORD" and rhs[0].upper in _FOR_HEADER_QUERY_STARTERS:
        return rhs
    return None


_CONDITIONAL_PLPGSQL_WRAPPERS = frozenset({
    "IF",
    "ELSIF",
    "ELSE",
    "CASE",
    "WHEN",
    "EXCEPTION",
    "FOR",
    "FOREACH",
    "WHILE",
    "LOOP",
})


def _strip_plpgsql_wrapper(
    tokens: Sequence[Token],
) -> tuple[list[Sequence[Token]], list[Sequence[Token]], list[str], Sequence[Token], bool]:
    """Unwrap PL/pgSQL headers and return `(header_queries, stripped_headers, events, embedded, conditional)`."""
    header_queries: list[Sequence[Token]] = []
    stripped_headers: list[Sequence[Token]] = []
    events: list[str] = []
    conditional = False
    cur = tokens
    while cur:
        first = cur[0]
        if first.kind == "WORD" and first.upper in _SQL_DIRECT_VERBS:
            return header_queries, stripped_headers, events, cur, conditional
        if first.kind == "PUNCT" and first.value == "<" and len(cur) >= 5:
            # <<label>>
            if (
                cur[1].kind == "PUNCT"
                and cur[1].value == "<"
                and cur[3].kind == "PUNCT"
                and cur[3].value == ">"
                and cur[4].kind == "PUNCT"
                and cur[4].value == ">"
            ):
                cur = cur[5:]
                continue
        if not (first.kind == "WORD" and first.upper in _PLPGSQL_PREFIX_STARTERS):
            return header_queries, stripped_headers, events, cur, conditional

        kw = first.upper
        if kw in _CONDITIONAL_PLPGSQL_WRAPPERS:
            conditional = True

        if kw in {"BEGIN", "LOOP", "ELSE"}:
            events.append(kw)
            cur = cur[1:]
            continue

        # DECLARE ... BEGIN <stmt>
        # IF / ELSIF / CASE / WHEN / EXCEPTION WHEN ... THEN <stmt>
        # FOR / FOREACH / WHILE ... LOOP <stmt>
        stop_kw = {
            "DECLARE": {"BEGIN"},
            "IF": {"THEN"},
            "ELSIF": {"THEN"},
            "CASE": {"THEN"},
            "WHEN": {"THEN"},
            "EXCEPTION": {"THEN"},
            "FOR": {"LOOP"},
            "FOREACH": {"LOOP"},
            "WHILE": {"LOOP"},
        }[kw]
        depth = 0
        expr_case_depth = 0
        advanced = False
        for idx in range(1, len(cur)):
            tok = cur[idx]
            prev_is_dot = cur[idx - 1].kind == "PUNCT" and cur[idx - 1].value == "."
            if tok.kind == "PUNCT":
                if tok.value == "(":
                    depth += 1
                elif tok.value == ")" and depth > 0:
                    depth -= 1
            elif depth == 0 and not prev_is_dot and tok.kind == "WORD":
                if tok.upper == "CASE":
                    expr_case_depth += 1
                elif tok.upper == "END" and expr_case_depth > 0:
                    expr_case_depth -= 1
                elif expr_case_depth == 0 and tok.upper in stop_kw:
                    hdr = cur[1:idx]
                    stripped_headers.append(hdr)
                    if kw == "FOR":
                        for_query = _extract_for_header_query(hdr)
                        if for_query is not None:
                            header_queries.append(for_query)
                    if kw == "DECLARE":
                        events.append("BEGIN")
                    elif kw in {"FOR", "FOREACH", "WHILE"}:
                        events.append("LOOP")
                    else:
                        events.append(kw)
                    cur = cur[idx + 1:]
                    advanced = True
                    break
        if not advanced:
            return header_queries, stripped_headers, events, cur, conditional
    return header_queries, stripped_headers, events, cur, conditional


_PREPARABLE_MIN_TOKENS = {
    "TABLE": 2,
    "VALUES": 2,
    "SELECT": 2,
    "DELETE": 3,
    "UPDATE": 4,
    "INSERT": 4,
    "WITH": 4,
    "MERGE": 4,
}


def _is_balanced_paren_group(tokens: Sequence[Token]) -> bool:
    """Return True when `tokens` starts with `(` and closes at the final token."""
    if len(tokens) < 2 or tokens[0].kind != "PUNCT" or tokens[0].value != "(":
        return False
    depth = 0
    for idx, tok in enumerate(tokens):
        if tok.kind == "PUNCT":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")":
                depth -= 1
                if depth == 0:
                    return idx == len(tokens) - 1
    return False


def _has_nonempty_comma_args(tokens: Sequence[Token]) -> bool:
    """Ensure comma-separated arguments inside `EXECUTE name(...)` contain no empty slots."""
    if not tokens:
        return False
    depth = 0
    seen_in_slot = False
    for tok in tokens:
        if tok.kind == "PUNCT":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")" and depth > 0:
                depth -= 1
            elif tok.value == "," and depth == 0:
                if not seen_in_slot:
                    return False
                seen_in_slot = False
                continue
        seen_in_slot = True
    return seen_in_slot


def _emit_do_executable(
    toks: Sequence[Token],
    expanded: list[Statement],
    prepared: dict[str, Sequence[Token]],
    active_prep: frozenset[str],
    *,
    conditional: bool = False,
) -> None:
    """Append checked `Statement` entries for a single unwrapped DO-block unit."""
    if not toks:
        return
    if toks[0].kind == "WORD" and toks[0].upper == "DO":
        body_tok = next(
            (t for t in toks[1:] if t.kind in {"DOLLAR", "STRING", "UNSUPPORTED_STRING"}),
            None,
        )
        if body_tok is not None:
            if body_tok.kind == "UNSUPPORTED_STRING":
                expanded.append(Statement(
                    tuple(toks),
                    toks[0].line,
                    unverified_reason=(
                        "dynamic EXECUTE inside DO block cannot be statically verified; "
                        "use static SQL statements so post-lock and contraction gates can audit them."
                    ),
                    conditional=conditional,
                ))
                return
            inner_tokens, _ = _tokenize_sql(body_tok.value, start_line=body_tok.line)
            nested = _expand_statements(
                inner_tokens,
                in_do_block=True,
                prepared=prepared,
                active_prep=active_prep,
            )
            if conditional:
                expanded.extend(
                    Statement(s.tokens, s.line, s.unverified_reason, conditional=True)
                    for s in nested
                )
            else:
                expanded.extend(nested)
            return
    if toks[0].kind == "WORD" and toks[0].upper == "EXECUTE":
        rest = toks[1:]
        if (
            rest
            and rest[0].kind in {"STRING", "DOLLAR"}
            and (
                len(rest) == 1
                or (rest[1].kind == "WORD" and rest[1].upper in {"INTO", "USING"})
            )
        ):
            inner_tokens, _ = _tokenize_sql(rest[0].value, start_line=rest[0].line)
            nested = _expand_statements(
                inner_tokens,
                in_do_block=False,
                prepared=prepared,
                active_prep=active_prep,
            )
            if conditional:
                expanded.extend(
                    Statement(s.tokens, s.line, s.unverified_reason, conditional=True)
                    for s in nested
                )
            else:
                expanded.extend(nested)
            return
        expanded.append(Statement(
            tuple(toks),
            toks[0].line,
            unverified_reason=(
                "dynamic EXECUTE inside DO block cannot be statically verified; "
                "use static SQL statements so post-lock and contraction gates can audit them."
            ),
            conditional=conditional,
        ))
        return
    if toks[0].kind == "WORD" and toks[0].upper == "OPEN":
        for_idx = next(
            (idx for idx, t in enumerate(toks[1:], start=1) if t.kind == "WORD" and t.upper == "FOR"),
            None,
        )
        if for_idx is not None:
            open_query = _strip_wrapping_parens(toks[for_idx + 1:])
            if open_query and open_query[0].kind == "WORD" and open_query[0].upper in _FOR_HEADER_QUERY_STARTERS:
                _emit_do_executable(open_query, expanded, prepared, active_prep, conditional=conditional)
                return
        expanded.append(Statement(tuple(toks), toks[0].line, conditional=conditional))
        return
    if not (
        (toks[0].kind == "WORD" and toks[0].upper in (_SQL_DIRECT_VERBS | _KNOWN_BENIGN_PLPGSQL_VERBS))
        or _is_plpgsql_assignment(toks)
    ):
        expanded.append(Statement(
            tuple(toks),
            toks[0].line,
            unverified_reason=(
                f"unsupported or unverified PL/pgSQL construct starting at '{toks[0].value}' "
                "inside DO block; extract DDL/DML to top-level static SQL so safety gates can audit it."
            ),
            conditional=conditional,
        ))
        return
    expanded.append(Statement(tuple(toks), toks[0].line, conditional=conditional))


def _apply_plpgsql_events(block_stack: list[str], events: Sequence[str]) -> None:
    """Update persistent PL/pgSQL control-flow stack across semicolon-delimited statements."""
    for ev in events:
        if ev in {"BEGIN", "IF", "CASE", "LOOP"}:
            block_stack.append(ev)
        elif ev == "EXCEPTION":
            if block_stack and block_stack[-1] == "BEGIN":
                block_stack[-1] = "EXCEPTION"
            else:
                block_stack.append("EXCEPTION")


def _pop_plpgsql_end(block_stack: list[str], toks: Sequence[Token]) -> None:
    """Pop matching control-flow frame on `END [IF | CASE | LOOP | label]`."""
    if not toks or toks[0].kind != "WORD" or toks[0].upper != "END" or not block_stack:
        return
    if len(toks) >= 2 and toks[1].kind == "WORD" and toks[1].upper in {"IF", "CASE", "LOOP"}:
        target = toks[1].upper
        for idx in range(len(block_stack) - 1, -1, -1):
            if block_stack[idx] == target:
                del block_stack[idx:]
                return
        return
    if len(toks) >= 2 and toks[1].kind == "WORD" and toks[1].upper in {"TRANSACTION", "WORK"}:
        return
    for idx in range(len(block_stack) - 1, -1, -1):
        if block_stack[idx] in {"BEGIN", "EXCEPTION"}:
            del block_stack[idx:]
            return


def _append_if_embedded_search_path(
    tokens: Sequence[Token],
    fallback_line: int,
    expanded: list[Statement],
    context_label: str,
) -> None:
    """Emit a fail-closed violation if a stripped header/declaration mutates `search_path`."""
    if tokens and _detect_search_path_change(tokens) is not None:
        expanded.append(Statement(
            tuple(tokens),
            tokens[0].line if tokens else fallback_line,
            unverified_reason=(
                f"search_path mutation inside {context_label} cannot be statically verified; "
                "keep SET search_path at top-level migration scope."
            ),
            conditional=True,
        ))


def _expand_statements(
    tokens: Sequence[Token],
    *,
    in_do_block: bool = False,
    prepared: dict[str, Sequence[Token]] | None = None,
    active_prep: frozenset[str] = frozenset(),
) -> list[Statement]:
    """Produce ordered executable statements, unwrapping `DO`, `PREPARE`/`EXECUTE`, and static SQL."""
    expanded: list[Statement] = []
    if prepared is None:
        prepared = {}
    in_declare = False
    block_stack: list[str] = []
    for raw_stmt in _split_token_statements(tokens):
        if in_do_block and in_declare:
            raw_toks = raw_stmt.tokens
            if (
                raw_toks
                and raw_toks[0].kind == "WORD"
                and raw_toks[0].upper in {"ALTER", "CREATE", "DROP", "LOCK", "EXECUTE", "DO"}
            ):
                in_declare = False
                header_queries, stripped_headers, events, toks, wrapper_cond = _strip_plpgsql_wrapper(raw_toks)
            else:
                depth = 0
                begin_idx: int | None = None
                for idx, tok in enumerate(raw_toks):
                    if tok.kind == "PUNCT":
                        if tok.value == "(":
                            depth += 1
                        elif tok.value == ")" and depth > 0:
                            depth -= 1
                    elif depth == 0 and tok.kind == "WORD" and tok.upper == "BEGIN":
                        begin_idx = idx
                        break
                if begin_idx is None:
                    _append_if_embedded_search_path(raw_toks, raw_stmt.line, expanded, "DO declaration")
                    continue
                _append_if_embedded_search_path(raw_toks[:begin_idx], raw_stmt.line, expanded, "DO declaration")
                in_declare = False
                header_queries, stripped_headers, events, toks, wrapper_cond = _strip_plpgsql_wrapper(raw_toks[begin_idx:])
        elif in_do_block:
            header_queries, stripped_headers, events, toks, wrapper_cond = _strip_plpgsql_wrapper(raw_stmt.tokens)
        else:
            header_queries = []
            stripped_headers = []
            events = []
            toks = raw_stmt.tokens
            wrapper_cond = False

        if in_do_block:
            _apply_plpgsql_events(block_stack, events)
            for hdr in stripped_headers:
                _append_if_embedded_search_path(hdr, raw_stmt.line, expanded, "DO control header")
            conditional = wrapper_cond or any(frame != "BEGIN" for frame in block_stack)
            for hq in header_queries:
                _emit_do_executable(hq, expanded, prepared, active_prep, conditional=conditional)
            if not toks:
                continue
            if toks[0].kind == "WORD" and toks[0].upper == "DECLARE":
                _append_if_embedded_search_path(toks, raw_stmt.line, expanded, "DO declaration")
                in_declare = True
                continue
            _emit_do_executable(toks, expanded, prepared, active_prep, conditional=conditional)
            _pop_plpgsql_end(block_stack, toks)
            continue

        if not toks:
            continue
        if toks[0].kind == "WORD":
            head = toks[0].upper
            if head == "DO":
                _emit_do_executable(toks, expanded, prepared, active_prep, conditional=False)
                continue
            if head == "PREPARE":
                if len(toks) >= 4 and _is_ident(toks[1]):
                    depth = 0
                    as_idx: int | None = None
                    for idx in range(2, len(toks)):
                        if toks[idx].kind == "PUNCT":
                            if toks[idx].value == "(":
                                depth += 1
                            elif toks[idx].value == ")" and depth > 0:
                                depth -= 1
                        elif depth == 0 and toks[idx].kind == "WORD" and toks[idx].upper == "AS":
                            as_idx = idx
                            break
                    if (
                        as_idx is not None
                        and as_idx + 1 < len(toks)
                        and (as_idx == 2 or _is_balanced_paren_group(toks[2:as_idx]))
                        and toks[as_idx + 1].kind == "WORD"
                        and toks[as_idx + 1].upper in _PREPARABLE_MIN_TOKENS
                        and len(toks) - (as_idx + 1) >= _PREPARABLE_MIN_TOKENS[toks[as_idx + 1].upper]
                    ):
                        prepared[_ident_name(toks[1])] = toks[as_idx + 1:]
                        continue
                expanded.append(Statement(
                    tuple(toks),
                    toks[0].line,
                    unverified_reason=(
                        "malformed PREPARE statement cannot be statically verified; "
                        "use top-level static SQL statements so safety gates can audit them."
                    ),
                ))
                continue
            if head == "EXECUTE":
                if (
                    len(toks) >= 2
                    and _is_ident(toks[1])
                    and (
                        len(toks) == 2
                        or (
                            len(toks) >= 5
                            and _is_balanced_paren_group(toks[2:])
                            and _has_nonempty_comma_args(toks[3:-1])
                        )
                    )
                ):
                    pname = _ident_name(toks[1])
                    if pname in prepared and pname not in active_prep:
                        expanded.extend(_expand_statements(
                            prepared[pname],
                            in_do_block=False,
                            prepared=prepared,
                            active_prep=active_prep | {pname},
                        ))
                        continue
                expanded.append(Statement(
                    tuple(toks),
                    toks[0].line,
                    unverified_reason=(
                        "top-level EXECUTE references an unknown or unverified prepared statement; "
                        "inline static SQL statements so safety gates can audit them."
                    ),
                ))
                continue
            if head == "DEALLOCATE":
                pos = 1
                if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "PREPARE":
                    pos += 1
                if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "ALL":
                    prepared.clear()
                elif pos < len(toks) and _is_ident(toks[pos]):
                    prepared.pop(_ident_name(toks[pos]), None)
                continue
        expanded.append(Statement(tuple(toks), toks[0].line))
    return expanded


# ---------------------------------------------------------------------------
# DDL & Lock/Backfill/Contraction Analysis


def _ident_name(tok: Token) -> str:
    return tok.value if tok.kind == "QIDENT" else tok.value.lower()


def _read_object_name(tokens: Sequence[Token], idx: int) -> tuple[str | None, str, int] | None:
    """Parse optional `schema.` + `name` starting at `idx`."""
    if idx >= len(tokens) or not _is_ident(tokens[idx]):
        return None
    first = _ident_name(tokens[idx])
    if (
        idx + 2 < len(tokens)
        and tokens[idx + 1].kind == "PUNCT"
        and tokens[idx + 1].value == "."
        and _is_ident(tokens[idx + 2])
    ):
        return first, _ident_name(tokens[idx + 2]), idx + 3
    return None, first, idx + 1


def _read_relation_target(tokens: Sequence[Token], idx: int) -> tuple[str | None, str, int] | None:
    """Parse PostgreSQL `relation_expr`: `[ONLY] name | ONLY ( name )` with optional `*`."""
    has_only = False
    if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == "ONLY":
        has_only = True
        idx += 1
    if (
        has_only
        and idx < len(tokens)
        and tokens[idx].kind == "PUNCT"
        and tokens[idx].value == "("
    ):
        parsed = _read_object_name(tokens, idx + 1)
        if parsed is None:
            return None
        schema, name, end_idx = parsed
        if end_idx >= len(tokens) or tokens[end_idx].kind != "PUNCT" or tokens[end_idx].value != ")":
            return None
        return schema, name, end_idx + 1
    parsed = _read_object_name(tokens, idx)
    if parsed is None:
        return None
    schema, name, end_idx = parsed
    if not has_only and end_idx < len(tokens) and tokens[end_idx].kind == "PUNCT" and tokens[end_idx].value == "*":
        end_idx += 1
    return schema, name, end_idx


def _skip_words(tokens: Sequence[Token], idx: int, *words: str) -> int:
    for word in words:
        if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == word:
            idx += 1
        else:
            return idx
    return idx


def _skip_if_exists(tokens: Sequence[Token], idx: int) -> int:
    if (
        idx + 1 < len(tokens)
        and tokens[idx].kind == "WORD"
        and tokens[idx].upper == "IF"
        and tokens[idx + 1].kind == "WORD"
        and tokens[idx + 1].upper == "EXISTS"
    ):
        return idx + 2
    return idx


def _skip_if_not_exists(tokens: Sequence[Token], idx: int) -> int:
    if (
        idx + 2 < len(tokens)
        and tokens[idx].kind == "WORD"
        and tokens[idx].upper == "IF"
        and tokens[idx + 1].kind == "WORD"
        and tokens[idx + 1].upper == "NOT"
        and tokens[idx + 2].kind == "WORD"
        and tokens[idx + 2].upper == "EXISTS"
    ):
        return idx + 3
    return idx


def _resolve_table_key(
    schema: str | None,
    name: str,
    created_bare_temp: set[str],
    default_schema: str | None = "core",
    *,
    temp_first: bool = True,
) -> tuple[str, str] | None:
    if schema is None:
        if temp_first and name in created_bare_temp:
            return ("pg_temp", name)
        if default_schema is None:
            return None
        return (default_schema, name)
    return (schema, name)


def _resolve_object_key(
    schema: str | None,
    name: str,
    default_schema: str | None = "core",
) -> tuple[str, str] | None:
    if schema is None:
        if default_schema is None:
            return None
        return (default_schema, name)
    return (schema, name)


def _classify_search_path_entries(entries: Sequence[str]) -> tuple[str | None, bool]:
    """Return `(single_user_schema_or_none, temp_first)` from raw schema list entries."""
    temp_first = True
    seen_non_system = False
    user_schemas: list[str] = []
    for raw in entries:
        low = raw.lower()
        if low in {"pg_catalog", ""}:
            continue
        if low == "pg_temp":
            if seen_non_system:
                temp_first = False
            continue
        seen_non_system = True
        user_schemas.append(raw)
    if len(user_schemas) == 1 and user_schemas[0].lower() != "$user":
        return user_schemas[0], temp_first
    return None, temp_first


def _parse_set_config_csv(raw: str) -> list[str]:
    """Split PostgreSQL `set_config('search_path', ...)` CSV schema list."""
    out: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if len(item) >= 2 and item.startswith('"') and item.endswith('"'):
            out.append(item[1:-1].replace('""', '"'))
        else:
            out.append(item.lower())
    return out


def _is_bare_set_config_stmt(toks: Sequence[Token], call_idx: int, end_idx: int) -> bool:
    """Return True when `toks` is solely `(SELECT|PERFORM) [pg_catalog.]set_config(...)`."""
    if not toks or toks[0].kind != "WORD" or toks[0].upper not in {"SELECT", "PERFORM"}:
        return False
    if end_idx != len(toks) - 1:
        return False
    prefix = toks[1:call_idx]
    if not prefix:
        return True
    return (
        len(prefix) == 2
        and _is_ident(prefix[0])
        and _ident_name(prefix[0]) == "pg_catalog"
        and prefix[1].kind == "PUNCT"
        and prefix[1].value == "."
    )


def _detect_search_path_change(toks: Sequence[Token]) -> tuple[bool, str | None, bool, str] | None:
    """If `toks` alters `search_path`, return `(is_local, single_schema_or_none, temp_first, error_reason)`."""
    if not toks:
        return None
    head = toks[0].upper if toks[0].kind == "WORD" else ""
    if head == "RESET" and len(toks) >= 2 and (
        (toks[1].kind == "WORD" and toks[1].upper in {"SEARCH_PATH", "ALL"})
        or _ident_name(toks[1]) == "search_path"
    ):
        return False, "core", True, ""
    if head == "SET":
        pos = 1
        is_local = False
        if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper in {"LOCAL", "SESSION"}:
            is_local = toks[pos].upper == "LOCAL"
            pos += 1
        if pos >= len(toks) or not _is_ident(toks[pos]) or _ident_name(toks[pos]) != "search_path":
            return None
        pos += 1
        if pos >= len(toks):
            return is_local, None, True, ""
        if (toks[pos].kind == "PUNCT" and toks[pos].value == "=") or (toks[pos].kind == "WORD" and toks[pos].upper == "TO"):
            pos += 1
        else:
            return is_local, None, True, ""
        rhs = toks[pos:]
        if not rhs:
            return is_local, None, True, ""
        if len(rhs) == 1 and rhs[0].kind == "WORD" and rhs[0].upper == "DEFAULT":
            return is_local, "core", True, ""
        entries: list[str] = []
        expect_item = True
        for tok in rhs:
            if expect_item:
                if _is_ident(tok):
                    entries.append(_ident_name(tok))
                elif tok.kind == "STRING":
                    entries.append(tok.value)
                else:
                    return is_local, None, True, ""
                expect_item = False
            elif tok.kind == "PUNCT" and tok.value == ",":
                expect_item = True
            else:
                return is_local, None, True, ""
        if expect_item:
            return is_local, None, True, ""
        schema, temp_first = _classify_search_path_entries(entries)
        return is_local, schema, temp_first, ""

    sp_calls: list[tuple[bool, str | None, bool, int, int]] = []
    for i, tok in enumerate(toks):
        if (
            _is_ident(tok)
            and _ident_name(tok) == "set_config"
            and i + 1 < len(toks)
            and toks[i + 1].kind == "PUNCT"
            and toks[i + 1].value == "("
        ):
            depth = 0
            end_idx = None
            for j in range(i + 1, len(toks)):
                if toks[j].kind == "PUNCT":
                    if toks[j].value == "(":
                        depth += 1
                    elif toks[j].value == ")":
                        depth -= 1
                        if depth == 0:
                            end_idx = j
                            break
            if end_idx is None:
                continue
            args = _split_top_level_commas(toks[i + 2:end_idx])
            if not args:
                continue
            arg0 = args[0]
            is_sp_arg = len(arg0) == 1 and arg0[0].kind == "STRING" and arg0[0].value.lower() == "search_path"
            is_literal_other = len(arg0) == 1 and arg0[0].kind == "STRING" and arg0[0].value.lower() != "search_path"
            if is_literal_other:
                continue
            is_local = (
                len(args) >= 3
                and len(args[2]) == 1
                and args[2][0].kind == "WORD"
                and args[2][0].upper == "TRUE"
            )
            if not is_sp_arg:
                sp_calls.append((False, None, True, i, end_idx))
                continue
            if len(args) >= 2 and len(args[1]) == 1 and args[1][0].kind == "STRING":
                schema, temp_first = _classify_search_path_entries(_parse_set_config_csv(args[1][0].value))
                sp_calls.append((is_local, schema, temp_first, i, end_idx))
            else:
                sp_calls.append((is_local, None, True, i, end_idx))
    if not sp_calls:
        return None
    if len(sp_calls) == 1:
        is_local, schema, temp_first, call_idx, end_idx = sp_calls[0]
        if _is_bare_set_config_stmt(toks, call_idx, end_idx):
            return is_local, schema, temp_first, ""
    any_session = any(not loc for loc, _, _, _, _ in sp_calls)
    return (
        not any_session,
        None,
        True,
        "conditional, embedded, or multi-call set_config('search_path', ...) cannot be statically verified; "
        "use a standalone SET [LOCAL] search_path = <schema> statement.",
    )


@dataclass(frozen=True)
class _TxOutcome:
    skip: bool
    error_reason: str = ""


class _TxSchemaState:
    """Track active transaction and `search_path` schema state across statements."""

    def __init__(self) -> None:
        self.session_default_schema: str | None = "core"
        self.session_temp_first = True
        self.tx_entry_schema: str | None = "core"
        self.tx_entry_temp_first = True
        self.tx_entry_extra: tuple[set[tuple[str, str]], ...] | None = None
        self.tx_active = False
        self.savepoints: list[
            tuple[
                str,
                tuple[str | None, bool, str | None, bool, tuple[set[tuple[str, str]], ...] | None],
            ]
        ] = []
        self.default_schema: str | None = "core"
        self.temp_first = True

    def _copy_extra(
        self,
        extra_sets: tuple[set[tuple[str, str]], ...] | None,
    ) -> tuple[set[tuple[str, str]], ...] | None:
        if extra_sets is None:
            return None
        return tuple(set(s) for s in extra_sets)

    def _restore_extra(
        self,
        extra_sets: tuple[set[tuple[str, str]], ...] | None,
        saved: tuple[set[tuple[str, str]], ...] | None,
    ) -> None:
        if extra_sets is None or saved is None:
            return
        for dst, src in zip(extra_sets, saved):
            dst.clear()
            dst.update(src)

    def advance(
        self,
        toks: Sequence[Token],
        *,
        allow_rollback: bool = False,
        conditional: bool = False,
        extra_sets: tuple[set[tuple[str, str]], ...] | None = None,
    ) -> _TxOutcome:
        """Update state from `toks`; return `_TxOutcome` indicating skip / fail-closed error."""
        if not toks:
            return _TxOutcome(skip=False)
        head = toks[0].upper if toks[0].kind == "WORD" else ""
        if head == "BEGIN" or (
            head == "START" and len(toks) >= 2 and toks[1].kind == "WORD" and toks[1].upper == "TRANSACTION"
        ):
            if conditional:
                return _TxOutcome(
                    skip=True,
                    error_reason="conditional transaction control inside DO block cannot be statically verified.",
                )
            if not self.tx_active:
                self.tx_active = True
                self.tx_entry_schema = self.session_default_schema
                self.tx_entry_temp_first = self.session_temp_first
                self.tx_entry_extra = self._copy_extra(extra_sets)
                self.default_schema = self.session_default_schema
                self.temp_first = self.session_temp_first
                self.savepoints.clear()
            return _TxOutcome(skip=True)
        if head == "SAVEPOINT" and len(toks) >= 2 and _is_ident(toks[1]):
            self.savepoints.append((
                _ident_name(toks[1]),
                (
                    self.default_schema,
                    self.temp_first,
                    self.session_default_schema,
                    self.session_temp_first,
                    self._copy_extra(extra_sets),
                ),
            ))
            if not allow_rollback:
                return _TxOutcome(
                    skip=True,
                    error_reason="SAVEPOINT control inside candidate migrations cannot be statically audited; "
                    "migrations execute atomically as a single transaction.",
                )
            return _TxOutcome(skip=True)
        if head == "RELEASE":
            pos = 1
            if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "SAVEPOINT":
                pos += 1
            if pos < len(toks) and _is_ident(toks[pos]):
                sp_name = _ident_name(toks[pos])
                for idx in range(len(self.savepoints) - 1, -1, -1):
                    if self.savepoints[idx][0] == sp_name:
                        del self.savepoints[idx:]
                        break
            if not allow_rollback:
                return _TxOutcome(
                    skip=True,
                    error_reason="RELEASE SAVEPOINT inside candidate migrations cannot be statically audited; "
                    "migrations execute atomically as a single transaction.",
                )
            return _TxOutcome(skip=True)
        if head == "ROLLBACK":
            pos = 1
            if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper in {"WORK", "TRANSACTION"}:
                pos += 1
            if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "TO":
                sp_pos = pos + 1
                if sp_pos < len(toks) and toks[sp_pos].kind == "WORD" and toks[sp_pos].upper == "SAVEPOINT":
                    sp_pos += 1
                matched_idx: int | None = None
                if sp_pos < len(toks) and _is_ident(toks[sp_pos]):
                    sp_name = _ident_name(toks[sp_pos])
                    for idx in range(len(self.savepoints) - 1, -1, -1):
                        if self.savepoints[idx][0] == sp_name:
                            matched_idx = idx
                            break
                if matched_idx is not None:
                    (
                        self.default_schema,
                        self.temp_first,
                        self.session_default_schema,
                        self.session_temp_first,
                        saved_extra,
                    ) = self.savepoints[matched_idx][1]
                    del self.savepoints[matched_idx + 1:]
                    self._restore_extra(extra_sets, saved_extra)
                else:
                    self.default_schema = None
                    self.session_default_schema = None
            else:
                self.default_schema = self.tx_entry_schema
                self.temp_first = self.tx_entry_temp_first
                self.session_default_schema = self.tx_entry_schema
                self.session_temp_first = self.tx_entry_temp_first
                self._restore_extra(extra_sets, self.tx_entry_extra)
                self.tx_active = False
                self.tx_entry_extra = None
                self.savepoints.clear()
            if not allow_rollback:
                return _TxOutcome(
                    skip=True,
                    error_reason="ROLLBACK inside candidate migrations cannot be statically audited; "
                    "migrations execute atomically as a single transaction.",
                )
            return _TxOutcome(skip=True)
        if head == "COMMIT" or (
            head == "END"
            and len(toks) >= 2
            and toks[1].kind == "WORD"
            and toks[1].upper in {"TRANSACTION", "WORK"}
        ):
            if conditional:
                return _TxOutcome(
                    skip=True,
                    error_reason="conditional COMMIT inside DO block cannot be statically verified.",
                )
            self.default_schema = self.session_default_schema
            self.temp_first = self.session_temp_first
            self.tx_entry_schema = self.session_default_schema
            self.tx_entry_temp_first = self.session_temp_first
            self.tx_entry_extra = None
            self.tx_active = False
            self.savepoints.clear()
            return _TxOutcome(skip=True)

        sp = _detect_search_path_change(toks)
        if sp is not None:
            is_local, next_schema, next_temp_first, sp_err = sp
            if conditional:
                self.default_schema = None
                self.session_default_schema = None
                return _TxOutcome(
                    skip=True,
                    error_reason="conditional search_path change inside DO control flow cannot be statically verified; "
                    "keep SET search_path at top-level migration scope.",
                )
            if sp_err:
                self.default_schema = None
                if not is_local:
                    self.session_default_schema = None
                return _TxOutcome(skip=(head in {"SET", "RESET"}), error_reason=sp_err)
            self.default_schema = next_schema
            self.temp_first = next_temp_first
            if not is_local:
                self.session_default_schema = next_schema
                self.session_temp_first = next_temp_first
            return _TxOutcome(skip=(head in {"SET", "RESET"}))
        return _TxOutcome(skip=False)


def _format_table(key: tuple[str, str]) -> str:
    return f"{key[0]}.{key[1]}"


def _split_top_level_commas(tokens: Sequence[Token]) -> list[tuple[Token, ...]]:
    parts: list[tuple[Token, ...]] = []
    cur: list[Token] = []
    depth = 0
    for tok in tokens:
        if tok.kind == "PUNCT":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")" and depth > 0:
                depth -= 1
            elif tok.value == "," and depth == 0:
                if cur:
                    parts.append(tuple(cur))
                    cur = []
                continue
        cur.append(tok)
    if cur:
        parts.append(tuple(cur))
    return parts


_NON_DML_LOCK_MODES = frozenset({
    ("ACCESS", "SHARE"),
    ("ROW", "SHARE"),
    ("ROW", "EXCLUSIVE"),
    ("SHARE", "UPDATE", "EXCLUSIVE"),
})
_DML_LOCK_MODES = frozenset({
    ("SHARE",),
    ("SHARE", "ROW", "EXCLUSIVE"),
    ("EXCLUSIVE",),
    ("ACCESS", "EXCLUSIVE"),
})


def _scan_comma_separated_names(
    tokens: Sequence[Token],
    idx: int,
    stop_words: frozenset[str],
) -> tuple[list[tuple[str | None, str, int]], int]:
    items: list[tuple[str | None, str, int]] = []
    while idx < len(tokens):
        if tokens[idx].kind == "WORD" and tokens[idx].upper in stop_words:
            break
        line = tokens[idx].line
        parsed = _read_relation_target(tokens, idx)
        if parsed is None:
            break
        schema, name, next_idx = parsed
        items.append((schema, name, line))
        idx = next_idx
        if idx < len(tokens) and tokens[idx].kind == "PUNCT" and tokens[idx].value == ",":
            idx += 1
            continue
        break
    return items, idx


def _read_comma_separated_names(
    tokens: Sequence[Token],
    idx: int,
    stop_words: frozenset[str],
) -> list[tuple[str | None, str, int]]:
    items, _ = _scan_comma_separated_names(tokens, idx, stop_words)
    return items


def _parse_lock_mode(tokens: Sequence[Token], idx: int) -> tuple[bool, str]:
    """Return ``(blocks_dml, reason)`` for the trailing ``IN <mode> MODE`` clause."""
    if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == "IN":
        mode_words: list[str] = []
        pos = idx + 1
        while pos < len(tokens) and tokens[pos].kind == "WORD" and tokens[pos].upper != "MODE":
            mode_words.append(tokens[pos].upper)
            pos += 1
        mode_tuple = tuple(mode_words)
        if pos < len(tokens) and tokens[pos].kind == "WORD" and tokens[pos].upper == "MODE" and mode_tuple:
            label = f"LOCK TABLE ... IN {' '.join(mode_tuple)} MODE"
            if mode_tuple in _NON_DML_LOCK_MODES:
                return False, label
            if mode_tuple in _DML_LOCK_MODES:
                return True, label
    return True, "LOCK TABLE"


def _record_table_lock(
    locked_tables: dict[tuple[str, str], LockInfo],
    key: tuple[str, str],
    *,
    line: int,
    reason: str,
    blocks_dml: bool,
) -> LockInfo:
    """Record a lock on an existing table, upgrading non-DML locks when DML-locked later."""
    existing = locked_tables.get(key)
    if existing is None or (blocks_dml and not existing.blocks_dml):
        info = LockInfo(line=line, reason=reason, blocks_dml=blocks_dml)
        locked_tables[key] = info
        return info
    return existing


def _analyze_alter_table_clauses(
    table_key: tuple[str, str],
    clauses: Sequence[tuple[Token, ...]],
) -> list[ContractionOp]:
    """Identify L2a contraction actions inside an ALTER TABLE on an existing table."""
    contractions: list[ContractionOp] = []
    qualified = _format_table(table_key)
    table_bare = table_key[1]

    for clause in clauses:
        if not clause:
            continue
        c_line = clause[0].line
        tok0 = clause[0]
        if tok0.kind != "WORD":
            continue
        v0 = tok0.upper

        if v0 == "DROP":
            if len(clause) >= 2 and clause[1].kind == "WORD" and clause[1].upper == "CONSTRAINT":
                pos = _skip_if_exists(clause, 2)
                if pos < len(clause) and _is_ident(clause[pos]):
                    cname = _ident_name(clause[pos])
                    contractions.append(ContractionOp(
                        kind="constraint",
                        table_key=table_key,
                        table_bare=table_bare,
                        identifier=cname,
                        line=clause[pos].line,
                        detail=f"ALTER TABLE {qualified} DROP CONSTRAINT {cname}",
                    ))
                continue

            # DROP [COLUMN] [IF EXISTS] <col>
            pos = 1
            if pos < len(clause) and clause[pos].kind == "WORD" and clause[pos].upper == "COLUMN":
                pos += 1
            pos = _skip_if_exists(clause, pos)
            if pos < len(clause) and _is_ident(clause[pos]):
                col = _ident_name(clause[pos])
                contractions.append(ContractionOp(
                    kind="column",
                    table_key=table_key,
                    table_bare=table_bare,
                    identifier=col,
                    line=clause[pos].line,
                    detail=f"ALTER TABLE {qualified} DROP COLUMN {col}",
                ))
            continue

        if v0 == "RENAME":
            if len(clause) >= 2 and clause[1].kind == "WORD" and clause[1].upper == "TO":
                parsed = _read_object_name(clause, 2)
                new_tbl = parsed[1] if parsed else "?"
                contractions.append(ContractionOp(
                    kind="table",
                    table_key=table_key,
                    table_bare=table_bare,
                    identifier=table_bare,
                    line=c_line,
                    detail=f"ALTER TABLE {qualified} RENAME TO {new_tbl}",
                ))
                continue
            if len(clause) >= 3 and clause[1].kind == "WORD" and clause[1].upper == "CONSTRAINT":
                if _is_ident(clause[2]):
                    old_c = _ident_name(clause[2])
                    contractions.append(ContractionOp(
                        kind="constraint",
                        table_key=table_key,
                        table_bare=table_bare,
                        identifier=old_c,
                        line=clause[2].line,
                        detail=f"ALTER TABLE {qualified} RENAME CONSTRAINT {old_c}",
                    ))
                continue
            # RENAME [COLUMN] <old> TO <new>
            pos = 1
            if pos < len(clause) and clause[pos].kind == "WORD" and clause[pos].upper == "COLUMN":
                pos += 1
            if pos < len(clause) and _is_ident(clause[pos]):
                old_col = _ident_name(clause[pos])
                contractions.append(ContractionOp(
                    kind="column",
                    table_key=table_key,
                    table_bare=table_bare,
                    identifier=old_col,
                    line=clause[pos].line,
                    detail=f"ALTER TABLE {qualified} RENAME COLUMN {old_col}",
                ))
            continue

        if v0 == "SET":
            if (
                len(clause) >= 3
                and clause[1].kind == "WORD"
                and clause[1].upper == "SCHEMA"
                and _is_ident(clause[2])
            ):
                new_schema = _ident_name(clause[2])
                contractions.append(ContractionOp(
                    kind="table",
                    table_key=table_key,
                    table_bare=table_bare,
                    identifier=table_bare,
                    line=c_line,
                    detail=f"ALTER TABLE {qualified} SET SCHEMA {new_schema}",
                ))
            continue

        if v0 == "ALTER":
            pos = 1
            if pos < len(clause) and clause[pos].kind == "WORD" and clause[pos].upper == "COLUMN":
                pos += 1
            if pos >= len(clause) or not _is_ident(clause[pos]):
                continue
            col = _ident_name(clause[pos])
            rest = [t.upper for t in clause[pos + 1:] if t.kind == "WORD"]
            if len(rest) >= 3 and rest[0] == "SET" and rest[1] == "NOT" and rest[2] == "NULL":
                contractions.append(ContractionOp(
                    kind="column_not_null",
                    table_key=table_key,
                    table_bare=table_bare,
                    identifier=col,
                    line=c_line,
                    detail=f"ALTER TABLE {qualified} ALTER COLUMN {col} SET NOT NULL",
                ))
            elif (
                (len(rest) >= 1 and rest[0] == "TYPE")
                or (len(rest) >= 3 and rest[0] == "SET" and rest[1] == "DATA" and rest[2] == "TYPE")
            ):
                contractions.append(ContractionOp(
                    kind="column_type",
                    table_key=table_key,
                    table_bare=table_bare,
                    identifier=col,
                    line=c_line,
                    detail=f"ALTER TABLE {qualified} ALTER COLUMN {col} TYPE",
                ))
            continue

        if v0 == "ADD":
            # Only table-level constraints (`ADD CONSTRAINT ... CHECK|UNIQUE|FOREIGN KEY` or
            # anonymous `ADD CHECK|UNIQUE|FOREIGN KEY`), NOT `ADD COLUMN ... CHECK (...)`.
            cname = ""
            pos = 1
            if pos < len(clause) and clause[pos].kind == "WORD" and clause[pos].upper == "CONSTRAINT":
                pos += 1
                if pos < len(clause) and _is_ident(clause[pos]):
                    cname = _ident_name(clause[pos])
                    pos += 1
            elif pos < len(clause) and clause[pos].kind == "WORD" and clause[pos].upper not in {
                "CHECK",
                "UNIQUE",
                "FOREIGN",
            }:
                # ADD [COLUMN] <name> <type> ... -> pure expansion (unless NOT NULL? Handled separately).
                continue

            if pos < len(clause) and clause[pos].kind == "WORD":
                ctype = clause[pos].upper
                matched_type = ""
                if ctype in {"CHECK", "UNIQUE"}:
                    matched_type = ctype
                elif (
                    ctype == "FOREIGN"
                    and pos + 1 < len(clause)
                    and clause[pos + 1].kind == "WORD"
                    and clause[pos + 1].upper == "KEY"
                ):
                    matched_type = "FOREIGN KEY"
                if matched_type:
                    contractions.append(ContractionOp(
                        kind="add_constraint",
                        table_key=table_key,
                        table_bare=table_bare,
                        identifier=cname,
                        line=c_line,
                        detail=(
                            f"ALTER TABLE {qualified} ADD CONSTRAINT "
                            f"{cname or '<unnamed>'} {matched_type}"
                        ),
                    ))

    return contractions


def _cte_main_verb(tokens: Sequence[Token]) -> Token | None:
    """For `WITH [RECURSIVE] cte AS (...), ... <VERB> ...`, return `<VERB>` token."""
    idx = 1
    if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == "RECURSIVE":
        idx += 1
    depth = 0
    while idx < len(tokens):
        tok = tokens[idx]
        if tok.kind == "PUNCT":
            if tok.value == "(":
                depth += 1
            elif tok.value == ")" and depth > 0:
                depth -= 1
            idx += 1
            continue
        if depth == 0 and tok.kind == "WORD" and tok.upper in {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "MERGE",
        }:
            return tok
        idx += 1
    return None


@dataclass(frozen=True)
class AnalysisResult:
    post_lock_violations: tuple[str, ...]
    contractions: tuple[ContractionOp, ...]
    annotations: dict[str, str]


def _strip_markdown_wrappers(value: str) -> str:
    cleaned = value.strip()
    while cleaned and cleaned[0] in "*_`~'\"":
        cleaned = cleaned[1:].strip()
    while cleaned and cleaned[-1] in "*_`~'\"":
        cleaned = cleaned[:-1].strip()
    return cleaned


def _extract_annotations(lines: Iterable[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in lines:
        m = _HEADER_RE.match(line)
        if not m:
            continue
        key = m.group(1).lower()
        cleaned = _strip_markdown_wrappers(m.group(2))
        if cleaned.lower().rstrip(".:") in _BARE_PLACEHOLDERS:
            continue
        if len(re.sub(r"\W", "", cleaned)) < 3:
            continue
        found[key] = cleaned
    return found


def _is_set_after_update_target(tokens: Sequence[Token], idx: int) -> bool:
    """Confirm `SET` follows `UPDATE [ONLY] table [[AS] alias]` (filters out non-DML mentions)."""
    if idx < len(tokens) and tokens[idx].kind == "PUNCT" and tokens[idx].value == "*":
        idx += 1
    if idx < len(tokens) and tokens[idx].kind == "WORD" and tokens[idx].upper == "SET":
        return True
    if idx + 1 < len(tokens) and _is_ident(tokens[idx]) and tokens[idx + 1].kind == "WORD" and tokens[idx + 1].upper == "SET":
        return True
    if (
        idx + 2 < len(tokens)
        and tokens[idx].kind == "WORD"
        and tokens[idx].upper == "AS"
        and _is_ident(tokens[idx + 1])
        and tokens[idx + 2].kind == "WORD"
        and tokens[idx + 2].upper == "SET"
    ):
        return True
    return False


def _scan_update_targets(
    path: str,
    toks: Sequence[Token],
    created_tables: set[tuple[str, str]],
    created_bare_temp: set[str],
    locked_tables: dict[tuple[str, str], LockInfo],
    post_lock_violations: list[str],
    default_schema: str | None = "core",
    *,
    temp_first: bool = True,
) -> None:
    for j, tok in enumerate(toks):
        if tok.kind != "WORD" or tok.upper != "UPDATE":
            continue
        if (
            j > 0
            and toks[j - 1].kind == "WORD"
            and toks[j - 1].upper in {"FOR", "KEY", "ON", "DO", "BEFORE", "AFTER", "OF"}
        ):
            continue
        target_pos = j + 1
        parsed = _read_relation_target(toks, target_pos)
        if not parsed:
            continue
        schema, name, after_name_idx = parsed
        if not _is_set_after_update_target(toks, after_name_idx):
            continue
        key = _resolve_table_key(schema, name, created_bare_temp, default_schema, temp_first=temp_first)
        if key is None:
            post_lock_violations.append(
                f"{path}:{tok.line}: ambiguous or empty search_path cannot resolve unqualified table '{name}'; "
                "schema-qualify table names or set a single-schema search_path."
            )
            continue
        if key not in created_tables and key in locked_tables:
            lock = locked_tables[key]
            if lock.blocks_dml:
                post_lock_violations.append(
                    f"{path}:{tok.line}: post-lock UPDATE backfill on existing table "
                    f"'{_format_table(key)}' (table locked at line {lock.line} by {lock.reason}); "
                    "backfill data outside locked DDL migrations."
                )


def _update_schema_inventory(
    sql_text: str,
    tables: set[tuple[str, str]],
    views: set[tuple[str, str]],
    indexes: set[tuple[str, str]],
) -> None:
    """Accumulate schema objects declared in an earlier migration file."""
    raw_tokens, _ = _tokenize_sql(sql_text, start_line=1)
    tx_state = _TxSchemaState()
    for stmt in _expand_statements(raw_tokens, in_do_block=False):
        toks = stmt.tokens
        if not toks or toks[0].kind != "WORD":
            continue
        head = toks[0].upper
        outcome = tx_state.advance(
            toks,
            allow_rollback=True,
            extra_sets=(tables, views, indexes),
        )
        default_schema = tx_state.default_schema
        if outcome.skip:
            continue
        if head == "CREATE":
            pos = 1
            is_temp = False
            while pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper in {
                "OR",
                "REPLACE",
                "TEMP",
                "TEMPORARY",
                "GLOBAL",
                "LOCAL",
                "UNLOGGED",
                "RECURSIVE",
                "UNIQUE",
            }:
                if toks[pos].upper in {"TEMP", "TEMPORARY"}:
                    is_temp = True
                pos += 1
            if pos >= len(toks) or toks[pos].kind != "WORD" or is_temp:
                continue
            obj = toks[pos].upper
            pos += 1
            if obj == "TABLE":
                pos = _skip_if_not_exists(toks, pos)
                parsed = _read_object_name(toks, pos)
                if parsed and parsed[0] != "pg_temp":
                    key = _resolve_object_key(parsed[0], parsed[1], default_schema)
                    if key is not None:
                        tables.add(key)
            elif obj == "VIEW" or (
                obj == "MATERIALIZED" and pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "VIEW"
            ):
                if obj == "MATERIALIZED":
                    pos += 1
                pos = _skip_if_not_exists(toks, pos)
                parsed = _read_object_name(toks, pos)
                if parsed and parsed[0] != "pg_temp":
                    key = _resolve_object_key(parsed[0], parsed[1], default_schema)
                    if key is not None:
                        views.add(key)
            elif obj == "INDEX":
                if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "CONCURRENTLY":
                    pos += 1
                pos = _skip_if_not_exists(toks, pos)
                on_idx = None
                depth = 0
                for j in range(pos, len(toks)):
                    if toks[j].kind == "PUNCT":
                        if toks[j].value == "(":
                            depth += 1
                        elif toks[j].value == ")" and depth > 0:
                            depth -= 1
                    elif depth == 0 and toks[j].kind == "WORD" and toks[j].upper == "ON":
                        on_idx = j
                        break
                if on_idx is not None and on_idx > pos:
                    idx_parsed = _read_object_name(toks, pos)
                    if idx_parsed:
                        idx_key = _resolve_object_key(idx_parsed[0], idx_parsed[1], default_schema)
                        if idx_key is not None:
                            indexes.add(idx_key)
        elif head == "DROP" and len(toks) >= 2 and toks[1].kind == "WORD":
            obj = toks[1].upper
            pos = 2
            if obj == "MATERIALIZED" and pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "VIEW":
                obj = "VIEW"
                pos += 1
            if obj not in {"TABLE", "VIEW", "INDEX"}:
                continue
            if obj == "INDEX" and pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "CONCURRENTLY":
                pos += 1
            pos = _skip_if_exists(toks, pos)
            for schema, name, _ in _read_comma_separated_names(toks, pos, frozenset({"CASCADE", "RESTRICT"})):
                key = _resolve_object_key(schema, name, default_schema)
                if key is None:
                    continue
                if obj == "TABLE":
                    tables.discard(key)
                elif obj == "VIEW":
                    views.discard(key)
                elif obj == "INDEX":
                    indexes.discard(key)
        elif head == "ALTER" and len(toks) >= 2 and toks[1].kind == "WORD":
            obj = toks[1].upper
            pos = 2
            if obj == "MATERIALIZED" and pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "VIEW":
                obj = "VIEW"
                pos += 1
            if obj not in {"TABLE", "VIEW", "INDEX"}:
                continue
            pos = _skip_if_exists(toks, pos)
            parsed = _read_relation_target(toks, pos) if obj == "TABLE" else _read_object_name(toks, pos)
            if not parsed:
                continue
            schema, name, rest_idx = parsed
            key = _resolve_object_key(schema, name, default_schema)
            if key is None:
                continue
            rest = toks[rest_idx:]
            new_key: tuple[str, str] | None = None
            if (
                len(rest) >= 3
                and rest[0].kind == "WORD"
                and rest[0].upper == "RENAME"
                and rest[1].kind == "WORD"
                and rest[1].upper == "TO"
                and _is_ident(rest[2])
            ):
                new_key = (key[0], _ident_name(rest[2]))
            elif (
                len(rest) >= 3
                and rest[0].kind == "WORD"
                and rest[0].upper == "SET"
                and rest[1].kind == "WORD"
                and rest[1].upper == "SCHEMA"
                and _is_ident(rest[2])
            ):
                new_key = (_ident_name(rest[2]), key[1])
            if new_key is not None:
                if obj == "TABLE":
                    tables.discard(key)
                    tables.add(new_key)
                elif obj == "VIEW":
                    views.discard(key)
                    views.add(new_key)
                elif obj == "INDEX":
                    indexes.discard(key)
                    indexes.add(new_key)


def analyze_migration(
    path: str,
    sql_text: str,
    *,
    prior_objects: SchemaInventory | None = None,
) -> AnalysisResult:
    """Analyze a single migration file for post-lock rules and contractions."""
    raw_tokens, comments = _tokenize_sql(sql_text, start_line=1)
    comment_lines: list[str] = []
    for block in comments:
        comment_lines.extend(block.splitlines())
    annotations = _extract_annotations(comment_lines)

    statements = _expand_statements(raw_tokens, in_do_block=False)

    created_tables: set[tuple[str, str]] = set()
    created_bare_temp: set[str] = set()
    created_views: set[tuple[str, str]] = set()
    created_indexes: set[tuple[str, str]] = set()
    known_existing_tables: set[tuple[str, str]] = (
        set(prior_objects.tables) if prior_objects is not None else set()
    )
    known_existing_views: set[tuple[str, str]] = (
        set(prior_objects.views) if prior_objects is not None else set()
    )
    known_existing_indexes: set[tuple[str, str]] = (
        set(prior_objects.indexes) if prior_objects is not None else set()
    )
    locked_tables: dict[tuple[str, str], LockInfo] = {}
    tx_state = _TxSchemaState()

    post_lock_violations: list[str] = []
    contractions: list[ContractionOp] = []

    def _ambiguous_schema(line_no: int, rel_name: str) -> None:
        post_lock_violations.append(
            f"{path}:{line_no}: ambiguous or empty search_path cannot resolve unqualified relation '{rel_name}'; "
            "schema-qualify object names or set a single-schema search_path."
        )

    for stmt in statements:
        toks = stmt.tokens
        invalid_tok = next((t for t in toks if t.kind == "INVALID_IDENT"), None)
        if invalid_tok is not None:
            post_lock_violations.append(
                f"{path}:{invalid_tok.line}: invalid Unicode escape in U&\"...\" identifier; "
                "cannot verify lock or contraction safety."
            )
            continue
        if stmt.unverified_reason:
            post_lock_violations.append(f"{path}:{stmt.line}: {stmt.unverified_reason}")
            continue
        if not toks:
            continue
        outcome = tx_state.advance(toks, allow_rollback=False, conditional=stmt.conditional)
        if outcome.error_reason:
            post_lock_violations.append(f"{path}:{stmt.line}: {outcome.error_reason}")
        default_schema = tx_state.default_schema
        temp_first = tx_state.temp_first
        if outcome.skip or toks[0].kind != "WORD":
            continue
        head = toks[0].upper

        if head == "CREATE":
            pos = 1
            is_temp = False
            has_or_replace = False
            while pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper in {
                "OR",
                "REPLACE",
                "TEMP",
                "TEMPORARY",
                "GLOBAL",
                "LOCAL",
                "UNLOGGED",
                "RECURSIVE",
                "UNIQUE",
            }:
                if toks[pos].upper in {"TEMP", "TEMPORARY"}:
                    is_temp = True
                elif toks[pos].upper == "REPLACE":
                    has_or_replace = True
                pos += 1
            if pos >= len(toks) or toks[pos].kind != "WORD":
                continue
            obj = toks[pos].upper
            pos += 1

            if obj == "TABLE":
                pos_after_if = _skip_if_not_exists(toks, pos)
                has_if_not_exists = pos_after_if != pos
                parsed = _read_object_name(toks, pos_after_if)
                if parsed:
                    schema, name, rest_idx = parsed
                    if is_temp or schema == "pg_temp":
                        created_bare_temp.add(name)
                        created_tables.add(("pg_temp", name))
                    else:
                        key = _resolve_object_key(schema, name, default_schema)
                        if key is None:
                            _ambiguous_schema(stmt.line, name)
                            continue
                        if has_if_not_exists:
                            if (
                                prior_objects is not None
                                and key not in known_existing_tables
                                and key not in locked_tables
                            ):
                                created_tables.add(key)
                        elif key not in known_existing_tables:
                            created_tables.add(key)
                    # Inspect `CREATE TABLE ... AS <query>` tails for embedded CTE updates.
                    for k in range(rest_idx, len(toks)):
                        if toks[k].kind == "WORD" and toks[k].upper == "AS":
                            _scan_update_targets(
                                path, toks[k + 1:], created_tables, created_bare_temp,
                                locked_tables, post_lock_violations, default_schema,
                                temp_first=temp_first,
                            )
                            break
                continue

            if obj == "MATERIALIZED" and pos < len(toks) and toks[pos].upper == "VIEW":
                obj = "VIEW"
                pos += 1
            if obj == "VIEW":
                pos_after_if = _skip_if_not_exists(toks, pos)
                has_if_not_exists = pos_after_if != pos
                parsed = _read_object_name(toks, pos_after_if)
                if parsed:
                    schema, name, _ = parsed
                    if is_temp or schema == "pg_temp":
                        created_views.add(("pg_temp", name))
                    else:
                        key = _resolve_object_key(schema, name, default_schema)
                        if key is None:
                            _ambiguous_schema(stmt.line, name)
                            continue
                        if has_or_replace or has_if_not_exists:
                            if prior_objects is not None and key not in known_existing_views:
                                created_views.add(key)
                        elif key not in known_existing_views:
                            created_views.add(key)
                continue

            if obj == "INDEX":
                if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "CONCURRENTLY":
                    pos += 1
                pos_after_if = _skip_if_not_exists(toks, pos)
                has_if_not_exists = pos_after_if != pos
                pos = pos_after_if
                # Find top-level ON
                on_idx = None
                depth = 0
                for j in range(pos, len(toks)):
                    if toks[j].kind == "PUNCT":
                        if toks[j].value == "(":
                            depth += 1
                        elif toks[j].value == ")" and depth > 0:
                            depth -= 1
                    elif depth == 0 and toks[j].kind == "WORD" and toks[j].upper == "ON":
                        on_idx = j
                        break
                if on_idx is None:
                    continue
                if on_idx > pos:
                    idx_parsed = _read_object_name(toks, pos)
                    if idx_parsed:
                        idx_key = _resolve_object_key(idx_parsed[0], idx_parsed[1], default_schema)
                        if idx_key is None:
                            _ambiguous_schema(stmt.line, idx_parsed[1])
                            continue
                        if has_if_not_exists:
                            if prior_objects is not None and idx_key not in known_existing_indexes:
                                created_indexes.add(idx_key)
                        elif idx_key not in known_existing_indexes:
                            created_indexes.add(idx_key)
                tbl_parsed = _read_relation_target(toks, on_idx + 1)
                if tbl_parsed:
                    key = _resolve_table_key(
                        tbl_parsed[0], tbl_parsed[1], created_bare_temp, default_schema, temp_first=temp_first,
                    )
                    if key is None:
                        _ambiguous_schema(stmt.line, tbl_parsed[1])
                        continue
                    if key not in created_tables and key in locked_tables:
                        lock = locked_tables[key]
                        post_lock_violations.append(
                            f"{path}:{toks[0].line}: post-lock CREATE INDEX on existing table "
                            f"'{_format_table(key)}' (table locked at line {lock.line} by {lock.reason}); "
                            "create indexes in a separate migration without holding a table lock."
                        )
                continue

            continue

        if head == "LOCK":
            pos = 1
            if pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "TABLE":
                pos += 1
            targets, next_idx = _scan_comma_separated_names(toks, pos, frozenset({"IN", "NOWAIT"}))
            blocks_dml, lock_reason = _parse_lock_mode(toks, next_idx)
            for schema, name, line in targets:
                key = _resolve_table_key(schema, name, created_bare_temp, default_schema, temp_first=temp_first)
                if key is None:
                    _ambiguous_schema(line, name)
                    continue
                if key not in created_tables:
                    _record_table_lock(
                        locked_tables,
                        key,
                        line=line,
                        reason=lock_reason,
                        blocks_dml=blocks_dml,
                    )
            continue

        if head in {"UPDATE", "WITH", "SELECT", "INSERT", "DELETE", "MERGE", "COPY"}:
            _scan_update_targets(
                path, toks, created_tables, created_bare_temp,
                locked_tables, post_lock_violations, default_schema, temp_first=temp_first,
            )
            continue

        if head == "DROP":
            if len(toks) < 2 or toks[1].kind != "WORD":
                continue
            obj = toks[1].upper
            pos = 2
            if obj in {"FUNCTION", "PROCEDURE", "AGGREGATE"}:
                continue
            if obj == "MATERIALIZED" and pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "VIEW":
                obj = "MATERIALIZED VIEW"
                pos += 1
            if obj not in {"TABLE", "VIEW", "MATERIALIZED VIEW", "INDEX"}:
                continue
            if obj == "INDEX" and pos < len(toks) and toks[pos].kind == "WORD" and toks[pos].upper == "CONCURRENTLY":
                pos += 1
            pos = _skip_if_exists(toks, pos)
            targets = _read_comma_separated_names(toks, pos, frozenset({"CASCADE", "RESTRICT"}))
            for schema, name, line in targets:
                if obj in {"VIEW", "MATERIALIZED VIEW", "INDEX"}:
                    if obj != "INDEX" and temp_first and schema is None and ("pg_temp", name) in created_views:
                        created_views.discard(("pg_temp", name))
                        continue
                    key = _resolve_object_key(schema, name, default_schema)
                else:
                    key = _resolve_table_key(schema, name, created_bare_temp, default_schema, temp_first=temp_first)
                if key is None:
                    _ambiguous_schema(line, name)
                    continue
                if obj == "TABLE":
                    if key in created_tables:
                        created_tables.discard(key)
                        if key[0] == "pg_temp":
                            created_bare_temp.discard(key[1])
                        continue
                    known_existing_tables.discard(key)
                    contractions.append(ContractionOp(
                        kind="table",
                        table_key=key,
                        table_bare=key[1],
                        identifier=key[1],
                        line=line,
                        detail=f"DROP TABLE {_format_table(key)}",
                    ))
                elif obj in {"VIEW", "MATERIALIZED VIEW"}:
                    if key in created_views:
                        created_views.discard(key)
                        continue
                    if schema is None and ("pg_temp", name) in created_views:
                        created_views.discard(("pg_temp", name))
                        continue
                    known_existing_views.discard(key)
                    contractions.append(ContractionOp(
                        kind="view",
                        table_key=key,
                        table_bare=key[1],
                        identifier=key[1],
                        line=line,
                        detail=f"DROP {obj} {_format_table(key)}",
                    ))
                elif obj == "INDEX":
                    if key in created_indexes:
                        created_indexes.discard(key)
                        continue
                    known_existing_indexes.discard(key)
                    contractions.append(ContractionOp(
                        kind="index",
                        table_key=key,
                        table_bare=key[1],
                        identifier=key[1],
                        line=line,
                        detail=f"DROP INDEX {_format_table(key)}",
                    ))
            continue

        if head == "ALTER":
            if len(toks) < 2 or toks[1].kind != "WORD":
                continue
            obj = toks[1].upper
            pos = 2
            if obj == "TABLE":
                pos = _skip_if_exists(toks, pos)
                parsed = _read_relation_target(toks, pos)
                if not parsed:
                    continue
                schema, name, rest_idx = parsed
                key = _resolve_table_key(schema, name, created_bare_temp, default_schema, temp_first=temp_first)
                if key is None:
                    _ambiguous_schema(stmt.line, name)
                    continue
                clauses = _split_top_level_commas(toks[rest_idx:])
                renamed_key: tuple[str, str] | None = None
                for clause in clauses:
                    if (
                        len(clause) >= 3
                        and clause[0].kind == "WORD"
                        and clause[0].upper == "RENAME"
                        and clause[1].kind == "WORD"
                        and clause[1].upper == "TO"
                    ):
                        r_parsed = _read_object_name(clause, 2)
                        if r_parsed:
                            renamed_key = (key[0], r_parsed[1])
                    elif (
                        len(clause) >= 3
                        and clause[0].kind == "WORD"
                        and clause[0].upper == "SET"
                        and clause[1].kind == "WORD"
                        and clause[1].upper == "SCHEMA"
                        and _is_ident(clause[2])
                    ):
                        renamed_key = (_ident_name(clause[2]), key[1])
                if key in created_tables:
                    if renamed_key is not None:
                        if key[0] == "pg_temp":
                            if renamed_key[0] == "pg_temp":
                                created_bare_temp.discard(key[1])
                                created_tables.discard(key)
                                created_bare_temp.add(renamed_key[1])
                                created_tables.add(renamed_key)
                        else:
                            created_tables.discard(key)
                            created_tables.add(renamed_key)
                    continue
                contractions.extend(_analyze_alter_table_clauses(key, clauses))
                lock_info = _record_table_lock(
                    locked_tables,
                    key,
                    line=stmt.line,
                    reason=f"ALTER TABLE {_format_table(key)}",
                    blocks_dml=True,
                )
                if renamed_key is not None:
                    _record_table_lock(
                        locked_tables,
                        renamed_key,
                        line=lock_info.line,
                        reason=lock_info.reason,
                        blocks_dml=True,
                    )
                    known_existing_tables.discard(key)
                    known_existing_tables.add(renamed_key)
            elif obj == "INDEX":
                pos = _skip_if_exists(toks, pos)
                parsed = _read_object_name(toks, pos)
                if not parsed:
                    continue
                schema, name, rest_idx = parsed
                key = _resolve_object_key(schema, name, default_schema)
                if key is None:
                    _ambiguous_schema(stmt.line, name)
                    continue
                rest = toks[rest_idx:]
                new_index_key: tuple[str, str] | None = None
                if (
                    len(rest) >= 3
                    and rest[0].kind == "WORD"
                    and rest[0].upper == "RENAME"
                    and rest[1].kind == "WORD"
                    and rest[1].upper == "TO"
                    and _is_ident(rest[2])
                ):
                    new_name = _ident_name(rest[2])
                    new_index_key = (key[0], new_name)
                    if key not in created_indexes:
                        contractions.append(ContractionOp(
                            kind="index",
                            table_key=key,
                            table_bare=key[1],
                            identifier=key[1],
                            line=stmt.line,
                            detail=f"ALTER INDEX {_format_table(key)} RENAME TO {new_name}",
                        ))
                elif (
                    len(rest) >= 3
                    and rest[0].kind == "WORD"
                    and rest[0].upper == "SET"
                    and rest[1].kind == "WORD"
                    and rest[1].upper == "SCHEMA"
                    and _is_ident(rest[2])
                ):
                    new_schema = _ident_name(rest[2])
                    new_index_key = (new_schema, key[1])
                    if key not in created_indexes:
                        contractions.append(ContractionOp(
                            kind="index",
                            table_key=key,
                            table_bare=key[1],
                            identifier=key[1],
                            line=stmt.line,
                            detail=f"ALTER INDEX {_format_table(key)} SET SCHEMA {new_schema}",
                        ))
                if new_index_key is not None:
                    if key in created_indexes:
                        created_indexes.discard(key)
                        created_indexes.add(new_index_key)
                    else:
                        known_existing_indexes.discard(key)
                        known_existing_indexes.add(new_index_key)
            elif obj in {"VIEW", "MATERIALIZED"}:
                is_mat = obj == "MATERIALIZED"
                if is_mat:
                    if pos >= len(toks) or toks[pos].kind != "WORD" or toks[pos].upper != "VIEW":
                        continue
                    pos += 1
                pos = _skip_if_exists(toks, pos)
                parsed = _read_object_name(toks, pos)
                if not parsed:
                    continue
                schema, name, rest_idx = parsed
                is_created_view = False
                if temp_first and schema is None and ("pg_temp", name) in created_views:
                    key: tuple[str, str] = ("pg_temp", name)
                    is_created_view = True
                else:
                    resolved = _resolve_object_key(schema, name, default_schema)
                    if resolved is None:
                        _ambiguous_schema(stmt.line, name)
                        continue
                    key = resolved
                    is_created_view = key in created_views
                rest = toks[rest_idx:]
                label = "MATERIALIZED VIEW" if is_mat else "VIEW"
                new_view_key: tuple[str, str] | None = None
                if (
                    len(rest) >= 3
                    and rest[0].kind == "WORD"
                    and rest[0].upper == "SET"
                    and rest[1].kind == "WORD"
                    and rest[1].upper == "SCHEMA"
                    and _is_ident(rest[2])
                ):
                    new_schema = _ident_name(rest[2])
                    new_view_key = (new_schema, key[1])
                    if not is_created_view:
                        contractions.append(ContractionOp(
                            kind="view",
                            table_key=key,
                            table_bare=key[1],
                            identifier=key[1],
                            line=stmt.line,
                            detail=f"ALTER {label} {_format_table(key)} SET SCHEMA {new_schema}",
                        ))
                elif rest and rest[0].kind == "WORD" and rest[0].upper == "RENAME":
                    if (
                        len(rest) >= 3
                        and rest[1].kind == "WORD"
                        and rest[1].upper == "TO"
                        and _is_ident(rest[2])
                    ):
                        new_name = _ident_name(rest[2])
                        new_view_key = (key[0], new_name)
                        if not is_created_view:
                            contractions.append(ContractionOp(
                                kind="view",
                                table_key=key,
                                table_bare=key[1],
                                identifier=key[1],
                                line=stmt.line,
                                detail=f"ALTER {label} {_format_table(key)} RENAME TO {new_name}",
                            ))
                    elif not is_created_view and not is_mat:
                        r_pos = 1
                        if (
                            r_pos < len(rest)
                            and rest[r_pos].kind == "WORD"
                            and rest[r_pos].upper == "COLUMN"
                        ):
                            r_pos += 1
                        if (
                            r_pos + 2 < len(rest)
                            and _is_ident(rest[r_pos])
                            and rest[r_pos + 1].kind == "WORD"
                            and rest[r_pos + 1].upper == "TO"
                            and _is_ident(rest[r_pos + 2])
                        ):
                            old_col = _ident_name(rest[r_pos])
                            new_col = _ident_name(rest[r_pos + 2])
                            contractions.append(ContractionOp(
                                kind="column",
                                table_key=key,
                                table_bare=key[1],
                                identifier=old_col,
                                line=stmt.line,
                                detail=f"ALTER VIEW {_format_table(key)} RENAME COLUMN {old_col} TO {new_col}",
                            ))
                if new_view_key is not None:
                    if is_created_view:
                        if key[0] != "pg_temp" or new_view_key[0] == "pg_temp":
                            created_views.discard(key)
                            created_views.add(new_view_key)
                    else:
                        known_existing_views.discard(key)
                        known_existing_views.add(new_view_key)
            continue

    return AnalysisResult(
        post_lock_violations=tuple(post_lock_violations),
        contractions=tuple(contractions),
        annotations=annotations,
    )


# ---------------------------------------------------------------------------
# Runtime Python Consumer Scanner (`hirey_core/` excluding migrations & tests)


def _python_code_lines(source: str) -> list[tuple[int, str]]:
    """Return `(line_no, text)` with `#` comments and Python docstrings stripped."""
    docstring_starts: set[tuple[int, int]] = set()
    try:
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    val = body[0].value
                    docstring_starts.add((val.lineno, val.col_offset))
    except (SyntaxError, IndentationError):
        # Fail closed cleanly by scanning raw non-comment lines if Python is unparseable.
        pass

    by_line: dict[int, list[str]] = {}
    try:
        tok_iter = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tok_iter:
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.start in docstring_starts:
                continue
            if tok.type in {tokenize.ENCODING, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER}:
                continue
            if "\n" in tok.string:
                base_line = tok.start[0]
                for offset, part in enumerate(tok.string.splitlines()):
                    if part.strip():
                        by_line.setdefault(base_line + offset, []).append(part)
            elif tok.string.strip():
                by_line.setdefault(tok.start[0], []).append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return [(idx + 1, line) for idx, line in enumerate(source.splitlines())]

    return [(ln, " ".join(parts)) for ln, parts in sorted(by_line.items())]


def _collect_disk_runtime_sources(repo_root: Path) -> dict[str, str]:
    core_dir = repo_root / "hirey_core"
    if not core_dir.is_dir():
        return {}
    sources: dict[str, str] = {}
    for py_path in sorted(core_dir.rglob("*.py")):
        rel = py_path.relative_to(repo_root).as_posix()
        parts = py_path.relative_to(core_dir).parts
        if any(part in {"migrations", "migrations_restore", "__pycache__"} for part in parts):
            continue
        name = py_path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        try:
            sources[rel] = py_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise GateInputError(f"cannot read runtime file {rel}: {exc}") from exc
    return sources


def _collect_git_runtime_sources(repo_root: Path, rev: str) -> dict[str, str]:
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--name-only", rev, "--", "hirey_core/"],
        cwd=str(repo_root),
        capture_output=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GateInputError(f"git ls-tree {rev} failed: {stderr}")
    paths = [
        p.decode("utf-8", errors="strict")
        for p in proc.stdout.split(b"\x00")
        if p
    ]
    sources: dict[str, str] = {}
    for rel in sorted(paths):
        if not rel.endswith(".py"):
            continue
        sub = rel[len("hirey_core/"):]
        parts = Path(sub).parts
        if any(part in {"migrations", "migrations_restore", "__pycache__"} for part in parts):
            continue
        name = Path(rel).name
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        show = subprocess.run(
            ["git", "show", f"{rev}:{rel}"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if show.returncode != 0:
            raise GateInputError(f"git show {rev}:{rel} failed: {show.stderr.strip()}")
        sources[rel] = show.stdout
    return sources


_WORD_SCAN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


@dataclass(frozen=True)
class ConsumerFileIndex:
    lines: tuple[tuple[int, str], ...]
    words: frozenset[str]


def build_consumer_index(
    runtime_sources: dict[str, str],
    *,
    required_words: Iterable[str] | None = None,
) -> dict[str, ConsumerFileIndex]:
    needed = {w.lower() for w in (required_words or ()) if w}
    index: dict[str, ConsumerFileIndex] = {}
    for path, text in sorted(runtime_sources.items()):
        if required_words is not None:
            if not needed:
                continue
            text_lower = text.lower()
            if not any(word in text_lower for word in needed):
                continue
        code_lines = tuple(_python_code_lines(text))
        words: set[str] = set()
        for _, line_text in code_lines:
            words.update(w.lower() for w in _WORD_SCAN_RE.findall(line_text))
        index[path] = ConsumerFileIndex(lines=code_lines, words=frozenset(words))
    return index


def find_unresolved_consumers(
    op: ContractionOp,
    consumer_index: dict[str, ConsumerFileIndex],
) -> list[ConsumerMatch]:
    """Scan runtime `hirey_core/` Python sources for unresolved removals/renames."""
    if not op.identifier or op.kind not in _CONSUMER_SCAN_KINDS:
        return []
    ident_lower = op.identifier.lower()
    ident_re = re.compile(rf"\b{re.escape(op.identifier)}\b", re.IGNORECASE)
    matches: list[ConsumerMatch] = []

    if op.kind in _TABLE_KINDS:
        for path, entry in consumer_index.items():
            if ident_lower not in entry.words:
                continue
            for line_no, text in entry.lines:
                if ident_re.search(text):
                    matches.append(ConsumerMatch(path, line_no, text.strip()[:120]))
                    break
        return matches

    # Column-level contraction: check files that also reference the owning table
    # so generic column names (e.g. `status`, `kind`) in unrelated modules do not
    # trigger false positives.
    table_lower = op.table_bare.lower()
    table_re = re.compile(rf"\b{re.escape(op.table_bare)}\b", re.IGNORECASE)
    for path, entry in consumer_index.items():
        if table_lower not in entry.words or ident_lower not in entry.words:
            continue
        if not any(table_re.search(text) for _, text in entry.lines):
            continue
        for line_no, text in entry.lines:
            if ident_re.search(text):
                matches.append(ConsumerMatch(path, line_no, text.strip()[:120]))
                break
    return matches


# ---------------------------------------------------------------------------
# Gate Evaluation & CLI


_MIGRATION_VERSION_RE = re.compile(r"^(?P<version>\d{4})_.*\.sql$")


def _migration_sort_key(path: str) -> tuple[int, str]:
    name = path.rsplit("/", 1)[-1]
    m = _MIGRATION_VERSION_RE.match(name)
    return (int(m.group("version")) if m else 10**9, name)


def _collect_git_migrations(repo_root: Path, rev: str) -> list[tuple[str, str]]:
    out = _git_output(repo_root, "ls-tree", "--name-only", rev, MIGRATIONS_PREFIX)
    paths = sorted(
        (p for p in out.splitlines() if p.startswith(MIGRATIONS_PREFIX) and p.endswith(".sql")),
        key=_migration_sort_key,
    )
    return [(rel, _git_output(repo_root, "show", f"{rev}:{rel}")) for rel in paths]


def _collect_disk_migrations(repo_root: Path) -> list[tuple[str, str]]:
    mig_dir = repo_root / "hirey_core" / "migrations"
    if not mig_dir.is_dir():
        return []
    files: list[tuple[str, str]] = []
    for sql_path in sorted(mig_dir.glob("*.sql"), key=lambda p: _migration_sort_key(p.name)):
        rel = sql_path.relative_to(repo_root).as_posix()
        try:
            files.append((rel, sql_path.read_text(encoding="utf-8")))
        except (OSError, UnicodeError) as exc:
            raise GateInputError(f"cannot read migration file {rel}: {exc}") from exc
    return files


def evaluate_migrations(
    migration_files: Sequence[tuple[str, str]],
    consumer_index: dict[str, ConsumerFileIndex] | None = None,
    *,
    pr_body: str = "",
    runtime_source_loader=None,
    prior_migrations: Sequence[tuple[str, str]] | None = None,
    prior_objects: SchemaInventory | None = None,
) -> list[str]:
    """Evaluate candidate migrations against Rule 1 and Rule 2; return violation messages."""
    violations: list[str] = []
    pr_annotations = _extract_annotations(pr_body.splitlines()) if pr_body else {}

    analyses: list[tuple[str, AnalysisResult]] = []
    if prior_migrations is not None:
        sorted_prior = sorted(prior_migrations, key=lambda item: _migration_sort_key(item[0]))
        prior_idx = 0
        acc_tables: set[tuple[str, str]] = set()
        acc_views: set[tuple[str, str]] = set()
        acc_indexes: set[tuple[str, str]] = set()
        for path, sql_text in sorted(migration_files, key=lambda item: _migration_sort_key(item[0])):
            target_key = _migration_sort_key(path)
            while prior_idx < len(sorted_prior) and _migration_sort_key(sorted_prior[prior_idx][0]) < target_key:
                _update_schema_inventory(sorted_prior[prior_idx][1], acc_tables, acc_views, acc_indexes)
                prior_idx += 1
            inv = SchemaInventory(
                tables=frozenset(acc_tables),
                views=frozenset(acc_views),
                indexes=frozenset(acc_indexes),
            )
            analyses.append((path, analyze_migration(path, sql_text, prior_objects=inv)))
            _update_schema_inventory(sql_text, acc_tables, acc_views, acc_indexes)
    else:
        for path, sql_text in migration_files:
            analyses.append((path, analyze_migration(path, sql_text, prior_objects=prior_objects)))

    contraction_words = {
        op.identifier
        for _, res in analyses
        for op in res.contractions
        if op.identifier and op.kind in _CONSUMER_SCAN_KINDS
    }
    if consumer_index is None:
        if contraction_words and runtime_source_loader is not None:
            consumer_index = build_consumer_index(
                runtime_source_loader(),
                required_words=contraction_words,
            )
        else:
            consumer_index = {}

    for path, result in analyses:
        violations.extend(result.post_lock_violations)

        if not result.contractions:
            continue

        merged_annotations = dict(pr_annotations)
        merged_annotations.update(result.annotations)
        missing_headers = [
            hdr
            for hdr, key in (
                ("Contraction-Of:", "contraction-of"),
                ("Consumers-Cleared:", "consumers-cleared"),
            )
            if key not in merged_annotations
        ]

        if missing_headers:
            ops_summary = "; ".join(f"L{op.line}: {op.detail}" for op in result.contractions[:5])
            if len(result.contractions) > 5:
                ops_summary += f" (+{len(result.contractions) - 5} more)"
            violations.append(
                f"{path}:{result.contractions[0].line}: contraction DDL (L2a) detected [{ops_summary}] "
                f"without non-empty {' and '.join(missing_headers)} in migration SQL header comments or PR body."
            )

        for op in result.contractions:
            if op.kind not in _CONSUMER_SCAN_KINDS:
                continue
            hits = find_unresolved_consumers(op, consumer_index)
            if hits:
                sample = ", ".join(f"{h.path}:{h.line}" for h in hits[:5])
                extra = f" (+{len(hits) - 5} more)" if len(hits) > 5 else ""
                violations.append(
                    f"{path}:{op.line}: contraction '{op.detail}' still has {len(hits)} "
                    f"unresolved runtime reference(s) under hirey_core/: {sample}{extra}"
                )

    return violations


def _git_output(repo_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise GateInputError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _find_repo_root(start: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(start),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip())
    return start


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--base", help="Base git commit/ref to diff against --head")
    mode.add_argument("--file", nargs="+", help="Check one or more migration .sql files directly")
    parser.add_argument("--head", default="HEAD", help="Head git commit/ref (default: HEAD)")
    parser.add_argument("--pr-body-file", type=Path, help="Optional file containing PR body markdown")
    parser.add_argument("--repo-root", type=Path, help="Repository root for hirey_core/ consumer scan")
    args = parser.parse_args(argv)

    try:
        pr_body = os.environ.get("PR_BODY", "")
        if args.pr_body_file is not None:
            pr_body = args.pr_body_file.read_text(encoding="utf-8")

        if args.base is not None:
            repo_root = args.repo_root.resolve() if args.repo_root else _find_repo_root(Path.cwd())
            diff_out = _git_output(
                repo_root,
                "diff",
                "--name-status",
                "--find-renames",
                f"{args.base}...{args.head}",
                "--",
                MIGRATIONS_PREFIX,
            )
            migration_files: list[tuple[str, str]] = []
            for raw_line in diff_out.splitlines():
                if not raw_line.strip():
                    continue
                fields = raw_line.split("\t")
                status = fields[0]
                rel_path = fields[-1]
                if status.startswith("D"):
                    continue
                if not (rel_path.startswith(MIGRATIONS_PREFIX) and rel_path.endswith(".sql")):
                    continue
                sql_text = _git_output(repo_root, "show", f"{args.head}:{rel_path}")
                migration_files.append((rel_path, sql_text))
            if not migration_files:
                print("Migration safety gate: no added/modified migrations in diff.")
                return 0
            source_loader = lambda: _collect_git_runtime_sources(repo_root, args.head)
            prior_migrations = _collect_git_migrations(repo_root, args.base)
        else:
            file_paths = [Path(p) for p in args.file]
            first_parent = file_paths[0].resolve().parent
            repo_root = (
                args.repo_root.resolve()
                if args.repo_root
                else _find_repo_root(first_parent if first_parent.exists() else Path.cwd())
            )
            migration_files = []
            for fpath in file_paths:
                try:
                    sql_text = fpath.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise GateInputError(f"cannot read migration file {fpath}: {exc}") from exc
                try:
                    display_path = fpath.resolve().relative_to(repo_root).as_posix()
                except ValueError:
                    display_path = str(fpath)
                migration_files.append((display_path, sql_text))
            source_loader = lambda: _collect_disk_runtime_sources(repo_root)
            prior_migrations = _collect_disk_migrations(repo_root) if (repo_root / "hirey_core" / "migrations").is_dir() else None

        violations = evaluate_migrations(
            migration_files,
            pr_body=pr_body,
            runtime_source_loader=source_loader,
            prior_migrations=prior_migrations,
        )
    except (GateInputError, OSError, UnicodeError) as exc:
        print(f"::error::migration safety gate error: {exc}", file=sys.stderr)
        return 2

    if violations:
        for violation in violations:
            print(f"::error::{violation}", file=sys.stderr)
        print(f"{len(violations)} migration safety violation(s) found.", file=sys.stderr)
        return 1

    print(f"Migration safety gate: {len(migration_files)} migration(s) passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
