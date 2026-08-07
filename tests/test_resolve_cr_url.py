"""The CR URL resolver must refuse rather than guess.

Everything downstream trusts the URL this returns: the corpus is built from
it, and every ruling cites the effective date it carries. A resolver that
guessed on a bad page would produce a corpus with confident, wrong provenance
— which is the precise failure this whole component exists to prevent. So the
interesting tests are the refusals.

No network: `resolve()` takes HTML, so the parsing is exercised directly.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from resolve_cr_url import ResolveError, resolve  # noqa: E402

LINK = ("https://media.wizards.com/2026/downloads/"
        "MagicCompRules%2020260807.txt")


def test_finds_the_link_and_its_date():
    url, date = resolve(f'<a href="{LINK}">Text</a>')
    assert url == LINK
    assert date == "20260807"


def test_accepts_a_literal_space_and_normalises_it():
    """The link is written both ways depending on where it appears."""
    raw = "https://media.wizards.com/2026/downloads/MagicCompRules 20260807.txt"
    url, date = resolve(f'<a href="{raw}">Text</a>')
    assert url == LINK, "the space must be percent-encoded on the way out"
    assert date == "20260807"


def test_ignores_the_pdf_and_docx_alongside_it():
    """The page offers all three formats; only the .txt is parseable."""
    html = "".join(
        f'<a href="https://media.wizards.com/2026/downloads/'
        f'MagicCompRules%2020260807.{ext}">x</a>'
        for ext in ("pdf", "docx", "txt"))
    url, _ = resolve(html)
    assert url.endswith(".txt")


def test_refuses_when_the_page_has_no_link():
    """A redesign must fail loudly. Constructing a URL from today's date would
    download something nobody chose."""
    with pytest.raises(ResolveError, match="no Comprehensive Rules"):
        resolve("<html><body>Rules have moved!</body></html>")


def test_refuses_when_several_versions_are_linked():
    """An archive page, or a release day with old and new both present, is
    ambiguous — and picking one silently is how the wrong corpus ships."""
    html = "".join(
        f'<a href="https://media.wizards.com/2026/downloads/'
        f'MagicCompRules%20{d}.txt">x</a>' for d in ("20260619", "20260807"))
    with pytest.raises(ResolveError, match="several CR"):
        resolve(html)


def test_the_same_version_linked_twice_is_not_ambiguous():
    """A page that repeats one link in a nav and a body is still unambiguous.
    Refusing there would make the resolver useless on an ordinary page."""
    url, date = resolve(f'<a href="{LINK}">a</a><a href="{LINK}">b</a>')
    assert date == "20260807"


def test_a_lookalike_host_is_not_matched():
    """Only the official media host counts."""
    html = ('<a href="https://media.wizards.example/2026/downloads/'
            'MagicCompRules%2020260807.txt">x</a>')
    with pytest.raises(ResolveError):
        resolve(html)


def test_a_link_only_in_a_script_block_is_not_used():
    """A raw regex over the response also matches URLs in scripts, comments
    and embedded state. If the page ever renders the current link dynamically
    while an archived one survives in a script, scanning raw text would return
    the OLD url — valid, downloadable, stale — and the corpus built from it
    would look healthy while citing superseded rules."""
    html = ('<script>var old = "https://media.wizards.com/2026/downloads/'
            'MagicCompRules%2020260101.txt";</script>'
            '<p>The rules are being updated.</p>')
    with pytest.raises(ResolveError, match="no Comprehensive Rules"):
        resolve(html)


def test_a_link_only_in_a_comment_is_not_used():
    html = ('<!-- https://media.wizards.com/2026/downloads/'
            'MagicCompRules%2020260101.txt -->')
    with pytest.raises(ResolveError, match="no Comprehensive Rules"):
        resolve(html)


def test_a_stale_link_in_a_script_does_not_shadow_the_real_anchor():
    """The dangerous case is not refusing — it is choosing the wrong one."""
    html = ('<script>var old="https://media.wizards.com/2026/downloads/'
            'MagicCompRules%2020260101.txt";</script>'
            f'<a href="{LINK}">Comprehensive Rules (txt)</a>')
    url, date = resolve(html)
    assert date == "20260807"


def test_a_href_with_trailing_text_is_not_matched():
    """fullmatch, not search: an href that merely contains the pattern is not
    the rules file."""
    html = f'<a href="{LINK}?utm_source=x&amp;redirect=evil">x</a>'
    with pytest.raises(ResolveError):
        resolve(html)
