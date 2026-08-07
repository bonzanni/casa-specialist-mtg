"""Guards for building the corpus unattended.

Nobody watches an automated build. A crash is fine — the workflow fails and
someone looks. The dangerous outcome is a build that succeeds while producing
something subtly wrong, because every other check in the pipeline asks about
integrity and identity, and none of them asks whether the corpus is any good.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from check_corpus_plausible import FLOORS, problems  # noqa: E402
from scryfall_stamp import StampError, extract  # noqa: E402


def _corpus(path: Path, *, rules=3000, glossary=600, cards=30000,
            rulings=60000, subrules=1500, effective="June 19, 2026",
            empty_text=0) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE rules(rule_id TEXT, parent_id TEXT, text TEXT, examples TEXT);"
        "CREATE TABLE glossary(term TEXT, definition TEXT);"
        "CREATE TABLE cards(oracle_id TEXT, name TEXT);"
        "CREATE TABLE rulings(oracle_id TEXT, comment TEXT);"
        "CREATE TABLE meta(key TEXT, value TEXT);")
    con.executemany(
        "INSERT INTO rules VALUES (?,?,?,?)",
        [(str(i), str(i) if i < subrules else None,
          "" if i < empty_text else "rule text", "") for i in range(rules)])
    con.executemany("INSERT INTO glossary VALUES (?,?)",
                    [(str(i), "d") for i in range(glossary)])
    con.executemany("INSERT INTO cards VALUES (?,?)",
                    [(str(i), "n") for i in range(cards)])
    con.executemany("INSERT INTO rulings VALUES (?,?)",
                    [(str(i), "c") for i in range(rulings)])
    con.execute("INSERT INTO meta VALUES ('cr_effective_date', ?)", (effective,))
    con.commit()
    con.close()
    return path


def test_a_healthy_corpus_passes(tmp_path):
    assert problems(_corpus(tmp_path / "c.sqlite")) == []


@pytest.mark.parametrize("table", sorted(FLOORS))
def test_a_collapsed_table_is_refused(tmp_path, table):
    """The parse most likely to succeed-but-fail leaves a table nearly empty."""
    kwargs = {table: FLOORS[table] // 2}
    if table == "rules":
        kwargs["subrules"] = 100
    found = problems(_corpus(tmp_path / f"{table}.sqlite", **kwargs))
    assert any(table in p for p in found), found


def test_an_unparsed_effective_date_is_refused(tmp_path):
    """The date comes out of the rules header. Losing it means the header
    changed shape, and every ruling would cite provenance it does not have."""
    found = problems(_corpus(tmp_path / "c.sqlite", effective="unknown"))
    assert any("cr_effective_date" in p for p in found), found


def test_rules_with_no_text_are_refused(tmp_path):
    """A row count says nothing about whether the rows contain anything."""
    found = problems(_corpus(tmp_path / "c.sqlite", empty_text=40))
    assert any("no text" in p for p in found), found


def test_lost_subrules_are_refused(tmp_path):
    """Subrules are the shape that breaks first when the CR parser drifts, and
    a corpus without them answers most real questions wrongly."""
    found = problems(_corpus(tmp_path / "c.sqlite", subrules=10))
    assert any("subrules" in p for p in found), found


# --- the Scryfall snapshot stamp -------------------------------------------

def test_extracts_the_oracle_cards_timestamp():
    payload = {"data": [
        {"type": "rulings", "updated_at": "2026-07-01T00:00:00+00:00"},
        {"type": "oracle_cards", "updated_at": "2026-08-07T09:03:15.937+00:00"},
    ]}
    assert extract(payload) == "2026-08-07T09:03:15.937+00:00"


@pytest.mark.parametrize("payload", [
    {},
    {"data": "not a list"},
    {"data": [{"type": "rulings", "updated_at": "x"}]},
    {"data": [{"type": "oracle_cards"}]},
])
def test_a_malformed_bulk_response_is_refused(payload):
    """Falling back to a placeholder would make change-detection believe
    nothing moved, and the corpus would quietly stop being rebuilt — a failure
    whose only symptom is staleness nobody notices."""
    with pytest.raises(StampError):
        extract(payload)


def test_the_stamp_is_what_the_real_api_shape_provides():
    """Pinned against the documented response shape rather than a live call,
    so the test stays offline while still describing the real contract."""
    sample = json.loads('{"object":"list","data":[{"object":"bulk_data",'
                        '"type":"oracle_cards","updated_at":"2026-08-07T09:03:15.937+00:00",'
                        '"download_uri":"https://example/x.json"}]}')
    assert extract(sample).startswith("2026-08-07T")
