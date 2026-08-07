#!/usr/bin/env python3
"""Print the release tag for a BUILT corpus, derived from the corpus itself.

Two properties, both learned the hard way.

**The tag identifies a build, not a rules date.** Tagging by CR date alone
means a Scryfall-only update — new Oracle text, unchanged rules, which is the
common case around a set release — lands on the existing tag and replaces its
asset. Any deployment that pinned the old archive's sha256 can then never
reinstall: the bytes at that URL hash to something else now. Including the
card-data date makes every distinct build its own release, so older ones keep
their assets and an existing pin goes on working.

**The stamps come from the corpus, not from a separate lookup.** Reading the
rules page and the Scryfall API again to label a release samples upstream at a
different moment than the build did; if either moved in between, the release
would describe a corpus that was never built. The corpus records what it was
actually made from, so that is what names it.

Usage:
  python3 scripts/corpus_release_id.py plugins/mtg/data/corpus.sqlite
  python3 scripts/corpus_release_id.py <path> --field cr_effective_date
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_MONTHS = ("january february march april may june july august september "
           "october november december").split()


def _cr_stamp(effective: str) -> str:
    """'June 19, 2026' -> '20260619'."""
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})\s*$", effective)
    if not m:
        raise ValueError(f"unparseable cr_effective_date {effective!r}")
    month = m.group(1).lower()
    if month not in _MONTHS:
        raise ValueError(f"unknown month in {effective!r}")
    return f"{int(m.group(3)):04d}{_MONTHS.index(month) + 1:02d}{int(m.group(2)):02d}"


def _scryfall_stamp(updated_at: str) -> str:
    """'2026-08-07T09:03:15.937+00:00' -> '20260807'."""
    text = updated_at.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        # Older builds fell back to a bare build date for this field.
        try:
            return datetime.strptime(text, "%Y-%m-%d").strftime("%Y%m%d")
        except ValueError:
            raise ValueError(f"unparseable scryfall_updated_at {text!r}") from None
    return m.group(1) + m.group(2) + m.group(3)


def release_id(path: Path) -> dict[str, str]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()
    for key in ("cr_effective_date", "scryfall_updated_at"):
        if not meta.get(key) or meta[key] == "unknown":
            raise ValueError(f"corpus meta is missing {key}")
    cr = _cr_stamp(meta["cr_effective_date"])
    sf = _scryfall_stamp(meta["scryfall_updated_at"])
    # An Italian-alias build is a DIFFERENT corpus from the same upstream
    # data, so it needs its own release. Without this, dispatching with
    # aliases against an otherwise-current release reported "nothing to do"
    # and quietly never produced the thing that was asked for.
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        aliases = con.execute("SELECT count(*) FROM card_aliases").fetchone()[0]
    except sqlite3.Error:
        aliases = 0
    finally:
        con.close()
    suffix = "-it" if aliases else ""
    return {
        "tag": f"cr-{cr}-cards-{sf}{suffix}",
        "aliases": str(aliases),
        "cr": cr,
        "cards": sf,
        "cr_effective_date": meta["cr_effective_date"],
        "scryfall_updated_at": meta["scryfall_updated_at"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--field", default="tag")
    args = ap.parse_args()
    try:
        info = release_id(Path(args.corpus))
    except (sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.field not in info:
        print(f"error: no such field {args.field!r}", file=sys.stderr)
        return 1
    print(info[args.field])
    return 0


if __name__ == "__main__":
    sys.exit(main())
