#!/usr/bin/env python3
"""Print Scryfall's current oracle_cards snapshot timestamp.

Used to decide whether a corpus needs rebuilding. The Comprehensive Rules and
the card data move independently — a set release changes Oracle text without
touching the rules — so "is there anything new?" needs both answers, and this
is the card half.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BULK_DATA = "https://api.scryfall.com/bulk-data"
UA = {"User-Agent": "casa-mtg-corpus-builder/1.0", "Accept": "application/json"}
TIMEOUT_S = 60


class StampError(Exception):
    """Refusal, with a reason the operator can act on."""


def extract(payload: dict) -> str:
    """The oracle_cards entry's updated_at, or a refusal."""
    entries = payload.get("data")
    if not isinstance(entries, list):
        raise StampError("bulk-data response has no data array")
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "oracle_cards":
            stamp = entry.get("updated_at")
            if not stamp:
                raise StampError("oracle_cards entry has no updated_at")
            return str(stamp)
    raise StampError("no oracle_cards entry in the bulk-data response")


def main() -> int:
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
        print(extract(payload))
    except StampError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
