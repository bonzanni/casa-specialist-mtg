#!/usr/bin/env python3
"""Print the current Comprehensive Rules .txt URL.

There is no stable URL. The file is published at

    https://media.wizards.com/<year>/downloads/MagicCompRules%20<YYYYMMDD>.txt

and the date changes with every release, so the only durable reference is the
rules landing page that links to the current one. This resolves that link, so
`build_corpus.py --cr-url "$(resolve_cr_url.py)"` needs no human to go and
look.

It is deliberately brittle in one direction: it refuses rather than guesses.
A page redesign, an unreachable host, no match, or more than one candidate all
exit non-zero with an explanation. Constructing a plausible-looking URL from
today's date, or picking one of several matches, would download something
nobody chose — and the corpus it produced would carry the wrong provenance
into every ruling that cited it.

Usage:
  python3 scripts/resolve_cr_url.py          # prints the URL
  python3 scripts/resolve_cr_url.py --date   # prints just the YYYYMMDD
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser

RULES_PAGE = "https://magic.wizards.com/en/rules"
UA = {"User-Agent": "casa-mtg-corpus-builder/1.0", "Accept": "text/html"}
TIMEOUT_S = 60
MAX_PAGE_BYTES = 8 * 1024 * 1024

# The space in the filename appears literally or percent-encoded depending on
# where the link was written, so accept both and normalise afterwards.
_CR_LINK = re.compile(
    r"https://media\.wizards\.com/\d{4}/downloads/"
    r"MagicCompRules(?:%20|\s|\+)+(\d{8})\.txt",
    re.IGNORECASE)


class ResolveError(Exception):
    """Refusal, with a reason the operator can act on."""


class _LinkHarvester(HTMLParser):
    """Collect href values from real anchors only.

    A regex over the whole response also matches URLs inside <script> blocks,
    HTML comments, embedded JSON state and example text. If the page ever
    renders the current link dynamically while an archived one survives in any
    of those, a raw scan would return the OLD url — valid, downloadable, and
    stale — and the corpus built from it would look perfectly healthy while
    citing superseded rules. Anchors are what a reader can actually follow.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []
        self._muted = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "template"):
            self._muted += 1
            return
        if tag == "a" and not self._muted:
            for name, value in attrs:
                if name.lower() == "href" and value:
                    self.hrefs.append(value)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "template") and self._muted:
            self._muted -= 1


def _fetch(url: str = RULES_PAGE) -> str:
    request = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return response.read(MAX_PAGE_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ResolveError(f"{url} returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise ResolveError(f"{url} unreachable: {exc.reason}") from None


def resolve(html: str) -> tuple[str, str]:
    """Return (url, YYYYMMDD) for the single CR .txt link on the page."""
    harvester = _LinkHarvester()
    try:
        harvester.feed(html)
    except Exception:  # noqa: BLE001 — malformed markup: treat as no links
        pass
    found = {}
    for href in harvester.hrefs:
        m = _CR_LINK.fullmatch(href.strip())
        if m:
            found[m.group(1)] = m.group(0)
    if not found:
        raise ResolveError(
            "no Comprehensive Rules .txt link found. The page layout has "
            "probably changed; check it by hand and fix the pattern.")
    if len(found) > 1:
        dates = ", ".join(sorted(found))
        raise ResolveError(
            f"several CR .txt links found ({dates}). Refusing to choose — "
            "pass --cr-url explicitly once you have decided which is current.")
    date, url = next(iter(found.items()))
    # Percent-encode the space so the URL survives being passed around.
    return re.sub(r"MagicCompRules(?:%20|\s|\+)+", "MagicCompRules%20", url), date


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="store_true",
                    help="print the YYYYMMDD stamp instead of the URL")
    ap.add_argument("--both", action="store_true",
                    help="print 'URL DATE' from ONE fetch, so a caller "
                         "needing both cannot straddle a page update")
    args = ap.parse_args()
    try:
        url, date = resolve(_fetch())
    except ResolveError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.both:
        print(f"{url} {date}")
    else:
        print(date if args.date else url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
