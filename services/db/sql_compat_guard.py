from __future__ import annotations

"""Fail-closed guards for the legacy SQLite-to-PostgreSQL compatibility surface.

The project still accepts SQLite-flavoured statements from older services. This
module owns the narrow safety boundary around that compatibility layer: real
qmark parameters are identified lexically, and unsupported SQLite PRAGMA
statements are rejected before reaching the PostgreSQL driver.
"""

import re
import sqlite3


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TABLE_INFO_RE = re.compile(r"(?is)^PRAGMA\s+table_info\(([^)]+)\)\s*;?\s*$")


def rewrite_qmark_placeholders(
    sql: str,
    *,
    escape_psycopg_percent_literals: bool = False,
) -> tuple[str, int]:
    """Replace real SQLite ``?`` parameters while preserving SQL semantics.

    This is deliberately a small lexical scanner, not a general SQL parser. It
    distinguishes quoted strings, quoted identifiers and both SQL comment forms
    so a question mark in text or a comment cannot become a psycopg parameter.
    When requested for driver execution, original percent characters are doubled
    because psycopg treats every bare ``%`` as client-side placeholder syntax,
    even inside SQL literals and comments. Generated qmark replacements stay
    as the intended ``%s`` placeholders.
    """

    out: list[str] = []
    placeholders = 0
    state = "normal"
    index = 0

    while index < len(sql):
        char = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""

        if escape_psycopg_percent_literals and char == "%":
            out.append("%%")
            index += 1
            continue

        if state == "single":
            out.append(char)
            if char == "'":
                if following == "'":
                    out.append(following)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue

        if state == "double":
            out.append(char)
            if char == '"':
                if following == '"':
                    out.append(following)
                    index += 2
                    continue
                state = "normal"
            index += 1
            continue

        if state == "line_comment":
            out.append(char)
            if char in "\r\n":
                state = "normal"
            index += 1
            continue

        if state == "block_comment":
            out.append(char)
            if char == "*" and following == "/":
                out.append(following)
                index += 2
                state = "normal"
                continue
            index += 1
            continue

        if char == "'":
            out.append(char)
            state = "single"
        elif char == '"':
            out.append(char)
            state = "double"
        elif char == "-" and following == "-":
            out.extend((char, following))
            index += 2
            state = "line_comment"
            continue
        elif char == "/" and following == "*":
            out.extend((char, following))
            index += 2
            state = "block_comment"
            continue
        elif char == "?":
            out.append("%s")
            placeholders += 1
        else:
            out.append(char)
        index += 1

    return "".join(out), placeholders


def replace_qmark_placeholders(sql: str) -> str:
    return rewrite_qmark_placeholders(
        sql,
        escape_psycopg_percent_literals=True,
    )[0]


def count_qmark_placeholders(sql: str) -> int:
    return rewrite_qmark_placeholders(sql)[1]


def validate_sqlite_compat_statement(sql: str) -> None:
    """Reject unsupported or malformed PRAGMA statements before translation."""

    statement = str(sql or "").strip()
    if not statement.upper().startswith("PRAGMA "):
        return

    table_info = _TABLE_INFO_RE.match(statement)
    if table_info is not None:
        table = table_info.group(1).strip().strip('"`[]')
        if _IDENTIFIER_RE.fullmatch(table) is None:
            raise sqlite3.OperationalError(
                "invalid PRAGMA table_info identifier for PostgreSQL compatibility"
            )
        return

    pragma_name = statement[7:].split("(", 1)[0].split("=", 1)[0].strip().rstrip(";")
    safe_name = pragma_name if _IDENTIFIER_RE.fullmatch(pragma_name) is not None else "unknown"
    raise sqlite3.OperationalError(
        f"unsupported SQLite PRAGMA for PostgreSQL compatibility: {safe_name}"
    )


__all__ = [
    "count_qmark_placeholders",
    "replace_qmark_placeholders",
    "rewrite_qmark_placeholders",
    "validate_sqlite_compat_statement",
]
