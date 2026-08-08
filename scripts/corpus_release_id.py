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
import hashlib
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# One grammar for every timestamp this repository accepts, so the readable
# stamp and the identity digest can never disagree about what a string means.
_INSTANT = re.compile(
    r"(?:(?P<date>\d{4}-\d{2}-\d{2})"
    r"|(?P<whole>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?P<frac>\.\d+)?(?P<off>Z|z|[+-]\d{2}:?\d{2})?)")

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

    In UTC. Reading the wall-clock digits out of the string meant an offset
    change alone renamed the release: 11:03:15+02:00 and 09:03:15Z are one
    moment and must not produce two tags for one snapshot.

    A bare date reads as midnight UTC rather than as its own shape, so the
    fallback and the same instant written out in full name one release.

    Derived from _canonical_instant so the readable stamp and the identity
    digest cannot disagree about what a string means.
    """
    return _canonical_instant(updated_at).replace("-", "").replace(":", "")[:15]


def _canonical_instant(stamp: str | None) -> str:
    """One timestamp as an unambiguous UTC instant, for identity purposes.

    Two things the readable stamp cannot express: fractional seconds, and
    the offset. '2026-08-07T11:03:15+02:00' and '2026-08-07T09:03:15Z' are
    the same moment and must produce the same identity; '…15.100Z' and
    '…15.900Z' are different moments and must not.

    Fractional digits are carried through as text, not through datetime.
    datetime truncates at microseconds, so '.1000001Z' and '.1000009Z' —
    both accepted by the validator — became one instant and one tag.
    Upstream is free to serve more precision than Python happens to model,
    and identity must not depend on that coincidence.
    """
    if not stamp:
        return ""
    text = stamp.strip()
    m = _INSTANT.fullmatch(text)
    if not m:
        raise ValueError(f"unparseable scryfall stamp {text!r}")
    whole, fraction, offset = m.group("whole"), m.group("frac"), m.group("off")
    if whole is None:
        # The bare-date fallback older builds wrote for this field. Read as
        # midnight UTC, which is what it means, so it cannot name a different
        # release than the same instant written out in full.
        whole, fraction, offset = f"{m.group('date')}T00:00:00", None, "Z"
    parsed = datetime.fromisoformat(
        whole + ("+00:00" if offset in (None, "Z", "z") else offset))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    # Trailing zeros carry no information: '.100' and '.1' are one moment.
    digits = (fraction or "").lstrip(".").rstrip("0")
    return parsed.strftime("%Y-%m-%dT%H:%M:%S") + (f".{digits}" if digits else "")


def _stamp_digest(oracle: str, rulings: str | None,
                  all_cards: str | None, builder: str | None = None) -> str:
    """Sixteen hex characters over the exact instants of every dataset.

    Eight was 32 bits, and a reviewer found a real collision by deterministic
    search after 126,930 candidates — two snapshots, one tag, which is the
    precise failure this suffix exists to prevent. Sixty-four bits puts that
    out of reach without making the tag meaningfully harder to read.

    The builder revision goes in WHOLE. The readable part carries only a
    prefix of it, and two revisions sharing that prefix produced one tag —
    which is the exact property the builder component was added to provide,
    since a parser fix over unchanged upstream data yields a different
    corpus and needs a different name.
    """
    joined = "\n".join(
        [_canonical_instant(s) for s in (oracle, rulings, all_cards)]
        + [builder or ""])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


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
        parts.append(f"b{builder[:12]}")
    # The readable part above is truncated to whole seconds and drops the UTC
    # offset, so two genuinely different snapshots — .100+00:00 and
    # .900+00:00 — produced one tag. That is not cosmetic: the plan reserves
    # snapshot A's tag, the builder downloads snapshot B, and the
    # publish-time comparison agrees because both sides computed the same
    # wrong name. This suffix carries the full instants the readable part
    # threw away, so identity is exact while the tag stays legible.
    parts.append(f"s{_stamp_digest(oracle, rulings, all_cards, builder)}")
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
