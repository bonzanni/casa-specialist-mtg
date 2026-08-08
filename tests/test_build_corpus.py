"""CR parser + corpus builder unit tests (offline fixtures, no network)."""
import json
import sqlite3

import pytest

from scripts.build_corpus import (
    build,
    bulk_uris,
    parse_cr,
    parse_glossary,
    split_cr_sections,
)
from scripts.scryfall_stamp import StampError, extract

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


def _write_jsonl(path, objects):
    """Write bulk data the way Scryfall now serves it: one object per line.

    It used to be a single JSON array, and the fixtures wrote arrays long
    after the real format changed — so the tests passed against a shape the
    builder would never actually meet. A fixture that does not resemble the
    input is a test of nothing.
    """
    path.write_text("".join(json.dumps(o) + "\n" for o in objects),
                    encoding="utf-8")


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
        "mana_cost": "{1}{R} // {1}{R}",
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
        "mana_cost": "",
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
    _write_jsonl(oracle, ORACLE_FIXTURE)
    rulings = tmp_path / "rulings.json"
    _write_jsonl(rulings, [])
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
    _write_jsonl(all_cards, [
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
    ])
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
    _write_jsonl(oracle, [
        {
            "oracle_id": "oid-art",
            "name": "Delver of Secrets // Delver of Secrets",
            "layout": "art_series",
            "type_line": "Card",
            "oracle_text": "",
            "mana_cost": "",
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
                {"name": "Delver of Secrets", "mana_cost": "{U}",
                 "oracle_text": "At the beginning..."},
                {"name": "Insectile Aberration", "oracle_text": "Flying"},
            ],
        },
    ])
    rulings = tmp_path / "rulings.json"
    _write_jsonl(rulings, [])
    out = tmp_path / "corpus.sqlite"
    all_cards = tmp_path / "all_cards.json"
    _write_jsonl(all_cards, [
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
    ])

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


# --- mana cost: carried as a fact, with '' distinguished from absent -------

def _build_tiny_corpus(tmp_path, cards):
    """Build a corpus from `cards` alone, reusing the CR/rulings boilerplate."""
    cr = tmp_path / "cr.txt"
    cr.write_text(
        "These rules are effective as of June 19, 2026.\n\n"
        "100.1. These are the rules of the game.\n\n"
        "Glossary\nFrobnicate\nA keyword ability.\n\nCredits\nSomeone.\n",
        encoding="utf-8",
    )
    oracle = tmp_path / "oracle_cards.json"
    _write_jsonl(oracle, cards)
    rulings = tmp_path / "rulings.json"
    _write_jsonl(rulings, [])
    out = tmp_path / "corpus.sqlite"
    build(cr, oracle, rulings, None, out)
    return out


def test_mana_cost_is_carried_and_empty_is_preserved(tmp_path):
    """A land has no mana cost; that is a fact about the card, and it must
    survive as '' rather than NULL. 'absent from the data' and 'absent from
    the corpus' are different claims — the whole point of this change."""
    out = _build_tiny_corpus(tmp_path, cards=[
        {"oracle_id": "o1", "name": "Lightning Bolt", "type_line": "Instant",
         "oracle_text": "…", "mana_cost": "{R}"},
        {"oracle_id": "o2", "name": "Island", "type_line": "Land",
         "oracle_text": "", "mana_cost": ""},
    ])
    con = sqlite3.connect(out)
    rows = dict(con.execute("SELECT name, mana_cost FROM cards"))
    assert rows["Lightning Bolt"] == "{R}"
    assert rows["Island"] == ""          # not None
    assert con.execute(
        "SELECT count(*) FROM cards WHERE mana_cost IS NULL").fetchone()[0] == 0
    con.close()


def test_mana_cost_joins_faces_when_top_level_is_absent(tmp_path):
    """Checked against the live card object, not from memory: transform and
    modal_dfc cards OMIT top-level mana_cost entirely (it is absent, not '')
    and carry the real cost per face — while split/adventure carry the
    combined cost at top level and need no join at all. Both shapes are
    fixtured here so a builder that only handles one of them fails."""
    out = _build_tiny_corpus(tmp_path, cards=[
        # transform: no top-level mana_cost key at all, back face costs ''
        {"oracle_id": "o3", "name": "Front // Back", "type_line": "Creature",
         "layout": "transform", "oracle_text": "",
         "card_faces": [{"name": "Front", "mana_cost": "{1}{G}", "oracle_text": "a"},
                        {"name": "Back", "mana_cost": "", "oracle_text": "b"}]},
        # split: combined cost present at top level, faces also carry theirs
        {"oracle_id": "o4", "name": "Wear // Tear", "type_line": "Instant",
         "layout": "split", "oracle_text": "", "mana_cost": "{1}{R} // {W}",
         "card_faces": [{"name": "Wear", "mana_cost": "{1}{R}", "oracle_text": "a"},
                        {"name": "Tear", "mana_cost": "{W}", "oracle_text": "b"}]},
    ])
    con = sqlite3.connect(out)
    assert con.execute(
        "SELECT mana_cost FROM cards WHERE oracle_id='o3'").fetchone()[0] == "{1}{G}"
    assert con.execute(
        "SELECT mana_cost FROM cards WHERE oracle_id='o4'"
    ).fetchone()[0] == "{1}{R} // {W}"
    con.close()


def _bulk(*entries: dict) -> dict:
    return {"data": list(entries)}


def _entry(kind: str, uri: str) -> dict:
    return {"type": kind, "jsonl_download_uri": uri,
            "updated_at": "2026-08-07T21:03:29.000+00:00"}


def test_bulk_data_without_a_jsonl_uri_fails_loudly():
    """Scryfall replaced `download_uri` with `jsonl_download_uri`, and the
    builder died with a bare KeyError deep inside a comprehension — which
    reads as a bug in us rather than a schema change upstream. The next one
    should say what happened.

    This calls the real resolver. It used to re-implement the loop inline,
    which meant it went on passing after the production code it was standing
    in for had been rewritten."""
    old_shape = _bulk({"type": "oracle_cards",
                       "download_uri": "https://example/old.json"},
                      _entry("rulings", "https://example/r.jsonl"))
    with pytest.raises(SystemExit, match="jsonl_download_uri"):
        bulk_uris(old_shape)


def test_a_duplicated_dataset_type_is_refused_rather_than_resolved():
    """The URI table was last-wins and every timestamp reader was first-wins.
    A response carrying two `oracle_cards` entries therefore downloaded the
    second dataset and stamped it with the first one's timestamp — publishing
    a corpus under a release id describing data it does not contain, and
    passing the publish-time tag comparison because both sides agreed on the
    same wrong stamp. There is no honest way to pick, so neither side does."""
    two = _bulk(_entry("oracle_cards", "https://example/first.jsonl"),
                _entry("rulings", "https://example/r.jsonl"),
                _entry("oracle_cards", "https://example/second.jsonl"))
    with pytest.raises(SystemExit, match="2 'oracle_cards' entries"):
        bulk_uris(two)

    # The stamp side must refuse the same response, not merely differ from it.
    with pytest.raises(StampError, match="2 'oracle_cards' entries"):
        extract(two, "oracle_cards")


def test_a_missing_required_dataset_is_refused():
    with pytest.raises(SystemExit, match="no 'rulings' entry"):
        bulk_uris(_bulk(_entry("oracle_cards", "https://example/o.jsonl")))


def test_all_cards_stays_optional():
    """Only --with-it-aliases needs it; a response without it is normal."""
    uris = bulk_uris(_bulk(_entry("oracle_cards", "https://example/o.jsonl"),
                           _entry("rulings", "https://example/r.jsonl")))
    assert set(uris) == {"oracle_cards", "rulings"}


def test_jsonl_is_streamed_and_a_bad_line_is_fatal(tmp_path):
    """Skipping a malformed line would drop cards silently, and the corpus
    would answer "not found" for whatever fell out — a wrong answer delivered
    confidently, which is the failure this component exists to prevent."""
    from scripts.build_corpus import _iter_jsonl

    good = tmp_path / "good.jsonl"
    good.write_text('{"a": 1}\n\n{"a": 2}\n')
    assert [o["a"] for o in _iter_jsonl(good)] == [1, 2]

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"a": 1}\nnot json\n')
    with pytest.raises(ValueError, match="malformed JSONL"):
        list(_iter_jsonl(bad))


def test_the_builder_does_not_classify_a_missing_cost_field(tmp_path):
    """Rounds 24-26, and the reason the guard that used to live here is gone.

    Three rounds of findings landed on one per-row predicate: it refused
    transform cards, then refused a REAL card (Westvale Abbey // Ormendahl,
    Profane Prince), then drew two different proposed sharpenings from two
    reviewers. The cause is that "unknown for this row" has nowhere to live
    -- the column is NOT NULL and holds a string -- so no predicate can add
    the state the schema lacks.

    Whether the feed still carries costs is a property of the CORPUS, and
    check_corpus_plausible.py answers it there, pinned at 19,999/20,000.
    This test pins the builder's half: every shape builds, none raises."""
    out = _build_tiny_corpus(tmp_path, cards=[
        # no key anywhere
        {"oracle_id": "o5", "name": "Costless", "type_line": "Instant",
         "oracle_text": "x"},
        # a land declaring '' deliberately
        {"oracle_id": "o6", "name": "Island", "type_line": "Land",
         "oracle_text": "", "mana_cost": ""},
        # one face carries the key, one does not
        {"oracle_id": "o8", "name": "Partial // Drift", "type_line": "Creature",
         "layout": "transform", "oracle_text": "",
         "card_faces": [{"name": "Partial", "mana_cost": "", "oracle_text": "a"},
                        {"name": "Drift", "oracle_text": "b"}]},
        # a face carrying JSON null
        {"oracle_id": "o9", "name": "Nulled // Face", "type_line": "Creature",
         "layout": "transform", "oracle_text": "",
         "card_faces": [{"name": "Nulled", "mana_cost": None, "oracle_text": "a"},
                        {"name": "Face", "mana_cost": None, "oracle_text": "b"}]},
    ])
    con = sqlite3.connect(out)
    rows = dict(con.execute("SELECT oracle_id, mana_cost FROM cards"))
    con.close()
    assert rows == {"o5": "", "o6": "", "o8": "", "o9": ""}


def test_the_corpus_gate_owns_what_the_builder_stopped_asking(tmp_path):
    """The other half of the cut, asserted rather than promised. If the feed
    stops carrying costs, every row becomes '' -- and the corpus is refused
    by the gate that can see all of them at once. Without this the cut would
    be a removal with nothing put back."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
    from check_corpus_plausible import problems

    out = _build_tiny_corpus(tmp_path, cards=[
        {"oracle_id": f"o{i}", "name": f"Card {i}", "type_line": "Instant",
         "oracle_text": "x"} for i in range(3)])       # no costs anywhere
    assert any("mana_cost" in p for p in problems(out)), problems(out)


def test_a_card_whose_faces_all_declare_an_empty_cost_still_builds(tmp_path):
    """Round 25, Sol, against round 24's own fix. Westvale Abbey // Ormendahl,
    Profane Prince is a real transform card: no top-level mana_cost key, and
    BOTH faces carry the key with ''. Verified against the live card object,
    not reasoned about.

    Round 24 filtered faces by truthiness, so both real fields vanished and
    the card looked identical to one carrying no field anywhere -- and the
    guard added to protect the build would have killed the next one. The
    question the guard must ask is whether the KEY exists, not whether its
    value is non-empty; that distinction is the entire point of the column."""
    out = _build_tiny_corpus(tmp_path, cards=[
        {"oracle_id": "o7", "name": "Westvale Abbey // Ormendahl, Profane Prince",
         "layout": "transform", "oracle_text": "",
         "card_faces": [{"name": "Westvale Abbey", "mana_cost": "", "oracle_text": "a"},
                        {"name": "Ormendahl, Profane Prince", "mana_cost": "",
                         "oracle_text": "b"}]},
    ])
    con = sqlite3.connect(out)
    assert con.execute(
        "SELECT mana_cost FROM cards WHERE oracle_id='o7'").fetchone()[0] == ""
    con.close()
