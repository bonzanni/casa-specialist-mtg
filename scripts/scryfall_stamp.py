#!/usr/bin/env python3
"""Print Scryfall's current bulk snapshot timestamps.

Used to decide whether a corpus needs rebuilding. Three things move
independently: the Comprehensive Rules, Oracle card text, and rulings. The
builder downloads all three, so change detection has to watch all three —
watching oracle_cards alone meant a rulings-only update was invisible, and
production would keep answering from stale rulings until something unrelated
happened to move.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

BULK_DATA = "https://api.scryfall.com/bulk-data"
UA = {"User-Agent": "casa-mtg-corpus-builder/1.0", "Accept": "application/json"}
TIMEOUT_S = 60


class StampError(Exception):
    """Refusal, with a reason the operator can act on."""


# Every dataset the builder consumes. Missing any one of them is a refusal:
# a stamp that silently disappears would freeze change detection on that
# dataset, and the only symptom is staleness nobody notices.
# all_cards is only consumed by an Italian-alias build, so it is watched but
# not required: demanding it would fail every ordinary run, and omitting it
# entirely left two alias builds from different card dumps indistinguishable.
WATCHED = ("oracle_cards", "rulings")
OPTIONAL = ("all_cards",)

# FULLMATCH, and anchored at both ends. A prefix match accepted
# "2026-08-07T09:03:15; <anything>" — a value that arrives from a third-party
# API and was, until this was caught, evaluated by the workflow shell holding
# contents: write. Validation that only checks the beginning of a string is
# not validation.
_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?")


def extract(payload: dict, kind: str = "oracle_cards") -> str:
    """One dataset's updated_at, validated, or a refusal."""
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise StampError("bulk-data response has no data array")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == kind:
            stamp = entry.get("updated_at")
            # Not just truthiness. A blank or malformed value used to pass
            # through and then match a substring of almost any release body,
            # turning the change check into a permanent false "nothing new".
            if not isinstance(stamp, str) or not _ISO.fullmatch(stamp.strip()):
                raise StampError(
                    f"{kind} updated_at is not an ISO timestamp: {stamp!r}")
            return stamp.strip()
    raise StampError(f"no {kind} entry in the bulk-data response")


def extract_all(payload: dict) -> dict[str, str]:
    """Every watched dataset's timestamp."""
    return {kind: extract(payload, kind) for kind in WATCHED}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=WATCHED + OPTIONAL,
                    help="print one dataset's stamp instead of all of them")
    args = ap.parse_args()
    request = urllib.request.Request(BULK_DATA, headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload = json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        # Refuse rather than fall back to "now" or "unknown": a wrong stamp
        # makes the change-detection think nothing moved, and the corpus
        # silently stops being rebuilt.
        print(f"error: could not read {BULK_DATA}: {exc}", file=sys.stderr)
        return 1
    try:
        stamps = extract_all(payload)
    except StampError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.kind:
        if args.kind in OPTIONAL:
            try:
                print(extract(payload, args.kind))
            except StampError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 1
        else:
            print(stamps[args.kind])
    else:
        # Plain values, one per line, in WATCHED order. Deliberately NOT
        # `name=value` for a shell to eval: the workflow reads these
        # positionally instead, so nothing here is ever executed.
        for kind in WATCHED:
            print(stamps[kind])
    return 0


if __name__ == "__main__":
    sys.exit(main())
