#!/usr/bin/env python3
"""Refuse to ship a corpus that parsed badly.

The real hazard of building unattended is not a crash — a crash fails the
workflow and someone looks. It is a parser that half-works: the build
succeeds, the sidecar matches, setup_corpus verifies the schema, and the
result answers "no rules match" to questions it should answer, or cites a
rule that is missing half its text. Every check downstream is about integrity
and identity; none of them asks whether the thing is any good.

So this asks. The floors are deliberately far below the real numbers (~3,150
rules, ~735 glossary entries, ~36,000 cards, ~76,800 rulings): they exist to
catch a parse that collapsed, not to track upstream growth, and a threshold
that needs adjusting every set would soon be raised until it meant nothing.

Usage:
  python3 scripts/check_corpus_plausible.py plugins/mtg/data/corpus.sqlite
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

FLOORS = {
    "rules": 2500,
    "glossary": 500,
    "cards": 25000,
    "rulings": 50000,
}


def problems(path: Path) -> list[str]:
    """Every reason this corpus should not be published. Empty means fine."""
    found: list[str] = []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, floor in FLOORS.items():
            try:
                n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                found.append(f"{table}: unreadable ({exc})")
                continue
            if n < floor:
                found.append(f"{table}: {n} rows, expected at least {floor}")

        meta = dict(con.execute("SELECT key, value FROM meta"))
        if meta.get("cr_effective_date", "unknown") == "unknown":
            # The date is parsed out of the rules text. Losing it means the
            # header changed shape, which usually means the rest did too —
            # and every ruling would cite provenance it does not have.
            found.append("cr_effective_date did not parse")

        # A rules table full of empty text passes a row count.
        empty = con.execute(
            "SELECT count(*) FROM rules WHERE text IS NULL OR trim(text) = ''"
        ).fetchone()[0]
        if empty:
            found.append(f"{empty} rules have no text")

        # Row counts say nothing about whether the rows are usable. A bulk
        # response of objects carrying oracle_id but no name yields plenty of
        # `cards` rows, an empty cards_fts, and no card that can be looked up
        # — and setup_corpus only checks that cards_fts EXISTS, not that
        # anything is in it.
        for table, column in (("cards", "name"), ("cards", "oracle_text"),
                              ("rulings", "comment")):
            try:
                blank = con.execute(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE {column} IS NULL OR trim({column}) = ''"
                ).fetchone()[0]
                total = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                found.append(f"{table}.{column}: unreadable ({exc})")
                continue
            # Some cards legitimately have no rules text (vanilla creatures),
            # so this is a "most of them are empty" check, not "any".
            if total and blank > total // 2:
                found.append(
                    f"{table}.{column}: {blank} of {total} rows are empty")

        for fts, base in (("cards_fts", "cards"), ("rules_fts", "rules")):
            try:
                n = con.execute(f"SELECT count(*) FROM {fts}").fetchone()[0]
            except sqlite3.Error as exc:
                found.append(f"{fts}: unreadable ({exc}) — search would fail")
                continue
            if n == 0:
                found.append(f"{fts} is empty; every search would return nothing")

        # Subrules are the shape that breaks first when the parser drifts.
        subrules = con.execute(
            "SELECT count(*) FROM rules WHERE parent_id IS NOT NULL AND parent_id != ''"
        ).fetchone()[0]
        if subrules < 1000:
            found.append(f"only {subrules} subrules; the parser likely lost them")
    finally:
        con.close()
    return found


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"error: no corpus at {path}", file=sys.stderr)
        return 1
    found = problems(path)
    if found:
        print("implausible corpus, refusing to publish:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"{path} looks like a real corpus")
    return 0


if __name__ == "__main__":
    sys.exit(main())
