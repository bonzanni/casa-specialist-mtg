"""CR parser + corpus builder unit tests (offline fixtures, no network)."""
import json
import sqlite3

import pytest

from scripts.build_corpus import (
    build,
    parse_cr,
    parse_glossary,
    split_cr_sections,
)

# Fixtures are INVENTED rules in the Comprehensive Rules' format, never
# excerpts of it. The parser cares about shape — numbering, subrule letters,
# Example: lines, continuation paragraphs, cross-references — and shape is
# reproducible without copying the text. This repository states that it
# contains no rules text; test fixtures are not an exception to that.
CR_FIXTURE = """\
702.2. Frobnicate

702.2a Frobnicate is a static ability.

702.2b A permanent with a frobnicate counter on it that has been targeted since the last time state-based actions were checked is sacrificed as a state-based action. See rule 704.
Example: A 1/1 widget with frobnicate meets a 9/9 widget. Both are sacrificed.

703.1. Turn-based widgets are resolved before any player receives priority.
"""

GLOSSARY_FIXTURE = """\
Frobnicate
An invented keyword ability used only to exercise the glossary parser. See rule 702.2, "Frobnicate."

Widget Pile
An invented zone used only to exercise the glossary parser. See rule 100.4.
"""


def test_rules_and_subrules_parsed():
    rules = parse_cr(CR_FIXTURE)
    ids = {r["rule_id"] for r in rules}
    assert {"702.2", "702.2a", "702.2b", "703.1"} <= ids


def test_subrule_parent_and_example():
    rules = {r["rule_id"]: r for r in parse_cr(CR_FIXTURE)}
    assert rules["702.2b"]["parent_id"] == "702.2"
    assert "1/1 widget with frobnicate" in rules["702.2b"]["examples"]
    assert "See rule 704" in rules["702.2b"]["text"]


def test_glossary_parsed():
    entries = dict(parse_glossary(GLOSSARY_FIXTURE))
    assert "Frobnicate" in entries and "702.2" in entries["Frobnicate"]


# --- continuation-paragraph rule: multi-line rule text joined --------------

CONTINUATION_FIXTURE = """\
100.1. These are the rules of the game.

100.2. To play, each player needs an invented widget pile, a handful of
small markers to stand in for frobnicate counters, and some agreed way to
record the score.
"""


def test_continuation_lines_joined_into_rule_text():
    rules = {r["rule_id"]: r for r in parse_cr(CONTINUATION_FIXTURE)}
    text = rules["100.2"]["text"]
    assert "small markers to stand in for frobnicate counters" in text
    assert "record the score." in text
    # continuation lines must not become their own rule rows
    assert "small markers to stand in for frobnicate counters" not in rules


# --- split_cr_sections: ToC "Glossary" early + real Glossary + Credits

SECTIONS_FIXTURE = """\
Contents
Introduction
1. Game Concepts
Glossary
Credits

These rules are effective as of June 19, 2026.

100.1. These are the rules of the game.

Glossary
Frobnicate
A keyword ability. See rule 702.2, "Frobnicate."

Credits
Magic head designer: Someone.
"""


def test_split_cr_sections_excludes_glossary_and_credits_from_rules():
    rules_text, glossary_text, date = split_cr_sections(SECTIONS_FIXTURE)
    assert date == "June 19, 2026"
    assert "100.1." in rules_text
    assert "Frobnicate" not in rules_text
    assert "Magic head designer" not in rules_text
    assert "Frobnicate" in glossary_text
    assert "Magic head designer" not in glossary_text


# --- corpus.sqlite fixtures for build()-level tests -------------------------

ORACLE_FIXTURE = [
    {
        "oracle_id": "oid-1",
        "name": "Voltaic Lash // Voltaic Lash",
        "type_line": "Sorcery",
        "oracle_text": "Spark a chosen widget for 3.",
        "keywords": [],
        "card_faces": [
            {"name": "Voltaic Lash", "oracle_text": "Spark a chosen widget for 3."},
            {"name": "Voltaic Lash", "oracle_text": "Spark a chosen widget for 3."},
        ],
    },
    {
        "oracle_id": "oid-2",
        "name": "Frobnicate Flats",
        "type_line": "Basic Land — Frob",
        "oracle_text": "({T}: Add {F}.)",
        "keywords": [],
    },
]


@pytest.fixture
def corpus_fixture_paths(tmp_path):
    cr = tmp_path / "cr.txt"
    cr.write_text(
        "These rules are effective as of June 19, 2026.\n\n"
        "100.1. These are the rules of the game.\n\n"
        "Glossary\nFrobnicate\nA keyword ability.\n\nCredits\nSomeone.\n",
        encoding="utf-8",
    )
    oracle = tmp_path / "oracle_cards.json"
    oracle.write_text(json.dumps(ORACLE_FIXTURE), encoding="utf-8")
    rulings = tmp_path / "rulings.json"
    rulings.write_text("[]", encoding="utf-8")
    out = tmp_path / "corpus.sqlite"
    return {"cr": cr, "oracle": oracle, "rulings": rulings, "out": out,
            "tmp_path": tmp_path}


def test_build_cards_fts_deduped_per_card(corpus_fixture_paths):
    p = corpus_fixture_paths
    build(p["cr"], p["oracle"], p["rulings"], None, p["out"])
    db = sqlite3.connect(p["out"])
    rows = db.execute("SELECT name, oracle_id FROM cards_fts").fetchall()
    db.close()
    # oid-1's two card_faces are both literally named "Voltaic Lash" ->
    # exactly one row for that repeated face name, plus one for the distinct
    # canonical display name "Voltaic Lash // Voltaic Lash" (two rows,
    # not three).
    oid1_rows = [r for r in rows if r[1] == "oid-1"]
    assert sorted(oid1_rows) == sorted([
        ("Voltaic Lash // Voltaic Lash", "oid-1"),
        ("Voltaic Lash", "oid-1"),
    ])
    assert len(rows) == len(set(rows)), f"duplicate (name, oracle_id) pairs: {rows}"


def test_build_scryfall_updated_at_from_bulk_meta(corpus_fixture_paths):
    p = corpus_fixture_paths
    bulk_meta = p["tmp_path"] / "bulk_meta.json"
    bulk_meta.write_text(json.dumps({
        "object": "list",
        "data": [
            {"type": "rulings", "updated_at": "2020-01-01T00:00:00Z"},
            {"type": "oracle_cards", "updated_at": "2026-07-15T09:03:15.937+00:00"},
        ],
    }), encoding="utf-8")
    build(p["cr"], p["oracle"], p["rulings"], None, p["out"],
          bulk_meta_path=bulk_meta)
    db = sqlite3.connect(p["out"])
    value = db.execute(
        "SELECT value FROM meta WHERE key='scryfall_updated_at'").fetchone()[0]
    db.close()
    assert value == "2026-07-15T09:03:15.937+00:00"


def test_build_scryfall_updated_at_falls_back_without_bulk_meta(
        corpus_fixture_paths, capsys):
    p = corpus_fixture_paths
    missing = p["tmp_path"] / "no-such-bulk_meta.json"
    build(p["cr"], p["oracle"], p["rulings"], None, p["out"],
          bulk_meta_path=missing)
    db = sqlite3.connect(p["out"])
    value = db.execute(
        "SELECT value FROM meta WHERE key='scryfall_updated_at'").fetchone()[0]
    db.close()
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", value)
    assert "warning" in capsys.readouterr().err.lower()


# --- card_faces IT alias extraction incl. dedup -----------------------------

def test_build_it_aliases_include_card_faces_printed_names(corpus_fixture_paths):
    p = corpus_fixture_paths
    all_cards = p["tmp_path"] / "all_cards.json"
    all_cards.write_text(json.dumps([
        {
            "lang": "it",
            "oracle_id": "oid-3",
            "printed_name": "Monica Rambeau",
            "card_faces": [
                {"printed_name": "Monica Rambeau"},
                {"printed_name": "Monica Rambeau, Fotone"},
            ],
        },
        # dedup: same (printed_lower, oracle_id) pair appears twice
        {
            "lang": "it",
            "oracle_id": "oid-3",
            "printed_name": "Monica Rambeau",
        },
        # non-IT card must be ignored entirely
        {
            "lang": "en",
            "oracle_id": "oid-4",
            "printed_name": "English Name",
        },
        {
            "lang": "it",
            "oracle_id": "oid-5",
            "card_faces": [{"printed_name": "Scopritore di Segreti"}],
        },
    ]), encoding="utf-8")
    build(p["cr"], p["oracle"], p["rulings"], all_cards, p["out"])
    db = sqlite3.connect(p["out"])
    rows = set(db.execute(
        "SELECT printed_lower, lang, oracle_id FROM card_aliases").fetchall())
    db.close()
    assert ("monica rambeau", "it", "oid-3") in rows
    assert ("monica rambeau, fotone", "it", "oid-3") in rows
    assert ("scopritore di segreti", "it", "oid-5") in rows
    assert not any(r[2] == "oid-4" for r in rows)
    # dedup: exactly one row for the repeated (name, oracle_id) pair
    assert len([r for r in rows if r == ("monica rambeau", "it", "oid-3")]) == 1


# --- art_series exclusion: non-playable ghost objects must not enter the
# corpus at all (name-colliding "Delver of Secrets // Delver of Secrets" with
# empty oracle text was producing spurious lookup_card ambiguity) -----------

def test_build_excludes_art_series_layout(tmp_path):
    cr = tmp_path / "cr.txt"
    cr.write_text(
        "These rules are effective as of June 19, 2026.\n\n"
        "100.1. These are the rules of the game.\n\n"
        "Glossary\nFrobnicate\nA keyword ability.\n\nCredits\nSomeone.\n",
        encoding="utf-8",
    )
    oracle = tmp_path / "oracle_cards.json"
    oracle.write_text(json.dumps([
        {
            "oracle_id": "oid-art",
            "name": "Delver of Secrets // Delver of Secrets",
            "layout": "art_series",
            "type_line": "Card",
            "oracle_text": "",
            "keywords": [],
        },
        {
            "oracle_id": "oid-real",
            "name": "Delver of Secrets // Insectile Aberration",
            "layout": "transform",
            "type_line": "Creature — Human Wizard",
            "oracle_text": "",
            "keywords": [],
            "card_faces": [
                {"name": "Delver of Secrets", "oracle_text": "At the beginning..."},
                {"name": "Insectile Aberration", "oracle_text": "Flying"},
            ],
        },
    ]), encoding="utf-8")
    rulings = tmp_path / "rulings.json"
    rulings.write_text("[]", encoding="utf-8")
    out = tmp_path / "corpus.sqlite"
    all_cards = tmp_path / "all_cards.json"
    all_cards.write_text(json.dumps([
        {
            "lang": "it",
            "oracle_id": "oid-art",
            "layout": "art_series",
            "printed_name": "Scrutatore di Segreti",
        },
        {
            "lang": "it",
            "oracle_id": "oid-real",
            "layout": "transform",
            "printed_name": "Scrutatore di Segreti",
        },
    ]), encoding="utf-8")

    build(cr, oracle, rulings, all_cards, out)
    db = sqlite3.connect(out)
    cards = db.execute("SELECT oracle_id FROM cards").fetchall()
    fts = db.execute("SELECT oracle_id FROM cards_fts").fetchall()
    aliases = db.execute("SELECT oracle_id FROM card_aliases").fetchall()
    db.close()

    assert ("oid-art",) not in cards
    assert ("oid-real",) in cards
    assert not any(r[0] == "oid-art" for r in fts)
    assert any(r[0] == "oid-real" for r in fts)
    assert not any(r[0] == "oid-art" for r in aliases)
    assert any(r[0] == "oid-real" for r in aliases)
