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
    """'2026-08-07T09:03:15.937+00:00' -> '20260807T090315'.

    To the SECOND, not the day. Truncating to a date meant two snapshots
    published on one UTC day — which happens — produced the same tag, so the
    second build either skipped as already-done or collided with an existing
    asset and refused. Identity has to be at least as precise as the thing it
    identifies.
    """
    text = updated_at.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", text)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}T{m.group(4)}{m.group(5)}{m.group(6)}"
    # Older builds fell back to a bare build date for this field.
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y%m%d")
    except ValueError:
        raise ValueError(f"unparseable scryfall stamp {text!r}") from None


def tag_from_inputs(*, cr: str, oracle: str, rulings: str | None = None,
                    all_cards: str | None = None,
                    builder: str | None = None) -> str:
    """The release tag, from values known BEFORE a build.

    The skip check and the publish step must agree on the tag or the
    automation eats itself: predicting a different string than it later
    publishes means every run decides it has nothing, rebuilds, and then
    refuses on an existing asset. One function, used by both.

    `builder` is the revision of the build code. A parser fix with unchanged
    upstream data produces a DIFFERENT corpus from the same inputs, so
    without it there was no tag under which that corpus could ever be
    published — the fix was unshippable.
    """
    parts = [f"cr-{cr}", f"cards-{_scryfall_stamp(oracle)}"]
    if rulings:
        parts.append(f"r{_scryfall_stamp(rulings)}")
    if all_cards:
        parts.append(f"it{_scryfall_stamp(all_cards)}")
    if builder:
        parts.append(f"b{builder[:7]}")
    return "-".join(parts)


def release_id(path: Path, builder: str | None = None) -> dict[str, str]:
    """Name a release for a corpus that has already been built.

    Used to VERIFY that what came out of the build is what the workflow
    planned to publish; the tag itself is computed from inputs before the
    build, by tag_from_inputs, so the prediction and the publication cannot
    disagree.
    """
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
        try:
            aliases = con.execute(
                "SELECT count(*) FROM card_aliases").fetchone()[0]
        except sqlite3.Error:
            aliases = 0
    finally:
        con.close()

    for key in ("cr_effective_date", "scryfall_updated_at"):
        if not meta.get(key) or meta[key] == "unknown":
            raise ValueError(f"corpus meta is missing {key}")

    def _present(key: str) -> str | None:
        value = meta.get(key)
        return value if value and value != "unknown" else None

    cr = _cr_stamp(meta["cr_effective_date"])
    # Rulings move independently of Oracle text; a tag omitting them cannot
    # tell a rulings-only rebuild from the build before it.
    rulings = _present("scryfall_rulings_updated_at")
    all_cards = _present("scryfall_all_cards_updated_at")

    if aliases and not all_cards:
        # Aliases come from all_cards. Without its snapshot, two alias builds
        # from different card dumps would claim the same tag — so refuse to
        # name it rather than mint an identity that is not one.
        raise ValueError(
            "alias corpus has no scryfall_all_cards_updated_at; cannot name "
            "a release that another alias build could not also claim")

    return {
        "tag": tag_from_inputs(cr=cr, oracle=meta["scryfall_updated_at"],
                               rulings=rulings,
                               all_cards=all_cards if aliases else None,
                               builder=builder),
        "aliases": str(aliases),
        "cr": cr,
        "cards": _scryfall_stamp(meta["scryfall_updated_at"]),
        "cr_effective_date": meta["cr_effective_date"],
        "scryfall_updated_at": meta["scryfall_updated_at"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus", nargs="?",
                    help="a built corpus; omit with --from-inputs")
    ap.add_argument("--from-inputs", action="store_true",
                    help="compute the tag from upstream stamps known BEFORE "
                         "a build, so the skip check and the publish step "
                         "cannot disagree about what this release is called")
    ap.add_argument("--cr", help="CR effective date, YYYYMMDD")
    ap.add_argument("--oracle", help="oracle_cards updated_at")
    ap.add_argument("--rulings", help="rulings updated_at")
    ap.add_argument("--all-cards", dest="all_cards",
                    help="all_cards updated_at (Italian-alias builds only)")
    ap.add_argument("--field", default="tag")
    ap.add_argument("--builder", help="revision of the build code; a parser "
                                      "change makes a different corpus")
    args = ap.parse_args()

    if args.from_inputs:
        if not args.cr or not args.oracle:
            print("error: --from-inputs needs --cr and --oracle",
                  file=sys.stderr)
            return 1
        try:
            print(tag_from_inputs(cr=args.cr, oracle=args.oracle,
                                  rulings=args.rulings,
                                  all_cards=args.all_cards,
                                  builder=args.builder))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if not args.corpus:
        print("error: give a corpus path, or use --from-inputs",
              file=sys.stderr)
        return 1
    try:
        info = release_id(Path(args.corpus), builder=args.builder)
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
