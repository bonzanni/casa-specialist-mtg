"""Guards for building the corpus unattended.

Nobody watches an automated build. A crash is fine — the workflow fails and
someone looks. The dangerous outcome is a build that succeeds while producing
something subtly wrong, because every other check in the pipeline asks about
integrity and identity, and none of them asks whether the corpus is any good.
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import pathlib  # noqa: E402
from check_corpus_plausible import FLOORS, problems  # noqa: E402
from scryfall_stamp import StampError, extract  # noqa: E402


def _corpus(path: Path, *, rules=3000, glossary=600, cards=30000,
            rulings=60000, subrules=1500, effective="June 19, 2026",
            empty_text=0) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE rules(rule_id TEXT, parent_id TEXT, text TEXT, examples TEXT);"
        "CREATE TABLE glossary(term TEXT, definition TEXT);"
        "CREATE TABLE cards(oracle_id TEXT, name TEXT, oracle_text TEXT);"
        "CREATE TABLE rulings(oracle_id TEXT, comment TEXT);"
        "CREATE TABLE meta(key TEXT, value TEXT);"
        # The search indexes belong in a fixture standing in for a healthy
        # corpus: without them the plausibility check has nothing to inspect,
        # and a fixture that omits what the checker looks for can only ever
        # exercise the failure path while appearing to test the happy one.
        "CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text);"
        "CREATE VIRTUAL TABLE cards_fts USING fts5(name, oracle_id UNINDEXED);")
    # The indexes must index the TABLES, not merely have the right number of
    # rows: a reviewer built a corpus whose rules_fts was correctly sized and
    # full of unrelated text, and the server answered "no rules match" for a
    # phrase present in every rule. A fixture standing in for a healthy corpus
    # has to be searchable the way a real one is.
    names = ["Island", "Forest", "Mountain", "Plains", "Swamp",
             "Lightning Bolt", "Counterspell", "Llanowar Elves"]

    def _card_name(i: int) -> str:
        return names[i] if i < len(names) else f"Card {i}"

    con.executemany("INSERT INTO rules_fts VALUES (?,?)",
                    [(str(i), "rule text") for i in range(rules)])
    con.executemany("INSERT INTO rules_fts VALUES (?,?)",
                    [(rid, "anchor rule text") for rid in ("100.1", "702.2")])
    con.executemany("INSERT INTO cards_fts VALUES (?,?)",
                    [(_card_name(i), str(i)) for i in range(cards)])
    con.executemany(
        "INSERT INTO rules VALUES (?,?,?,?)",
        [(str(i), str(i) if i < subrules else None,
          "" if i < empty_text else "rule text", "") for i in range(rules)])
    # The anchor rules the checker requires. A fixture standing in for a real
    # corpus has to contain what a real corpus contains, or the happy-path
    # test is only asserting that the checker is lenient.
    con.executemany("INSERT INTO rules VALUES (?,?,?,?)",
                    [(rid, None, "anchor rule text", "")
                     for rid in ("100.1", "702.2")])
    con.executemany("INSERT INTO glossary VALUES (?,?)",
                    [(str(i), "d") for i in range(glossary)])
    con.executemany(
        "INSERT INTO cards VALUES (?,?,?)",
        [(str(i), _card_name(i), "rules text") for i in range(cards)])
    # Every ruling points at a card that exists: an unreachable ruling is the
    # exact defect the linkage check exists to catch, so the healthy fixture
    # must not contain any.
    con.executemany("INSERT INTO rulings VALUES (?,?)",
                    [(str(i % cards) if cards else None, "c")
                     for i in range(rulings)])
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


# --- naming a release from the corpus itself -------------------------------

def test_the_tag_distinguishes_a_cards_only_rebuild(tmp_path):
    """The claim "older releases keep their assets" was FALSE while the tag
    was the CR date alone: a Scryfall-only update — new Oracle text, unchanged
    rules, the common case at a set release — landed on the existing tag and
    clobbered its asset, so any deployment pinning the old archive could never
    reinstall. The card date belongs in the tag."""
    from corpus_release_id import release_id

    a = _corpus(tmp_path / "a.sqlite")
    b = _corpus(tmp_path / "b.sqlite")
    for path, stamp in ((a, "2026-08-07T09:03:15.937+00:00"),
                        (b, "2026-08-21T09:03:15.937+00:00")):
        con = sqlite3.connect(path)
        con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)", (stamp,))
        con.commit()
        con.close()

    assert release_id(a)["tag"] != release_id(b)["tag"]
    assert release_id(a)["tag"] == "cr-20260619-cards-20260807T090315-sb047af9cb00fd108"


def test_the_release_id_comes_from_the_corpus_not_a_fresh_lookup(tmp_path):
    """Re-reading upstream to label a release samples it at a different moment
    than the build did; if either source moved in between, the release would
    describe a corpus that was never built."""
    from corpus_release_id import release_id

    path = _corpus(tmp_path / "c.sqlite", effective="August 7, 2026")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
                ("2026-08-07T09:03:15.937+00:00",))
    con.commit()
    con.close()
    info = release_id(path)
    assert info["cr_effective_date"] == "August 7, 2026"
    assert info["tag"] == "cr-20260807-cards-20260807T090315-sb047af9cb00fd108"


@pytest.mark.parametrize("effective", ["unknown", "", "sometime in June"])
def test_an_unusable_effective_date_refuses_to_name_a_release(tmp_path, effective):
    from corpus_release_id import release_id

    path = _corpus(tmp_path / "c.sqlite", effective=effective)
    con = sqlite3.connect(path)
    con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
                ("2026-08-07T09:03:15.937+00:00",))
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        release_id(path)


def test_a_missing_card_stamp_refuses_to_name_a_release(tmp_path):
    from corpus_release_id import release_id

    with pytest.raises(ValueError, match="scryfall_updated_at"):
        release_id(_corpus(tmp_path / "c.sqlite"))


def test_all_watched_datasets_must_be_present():
    """The builder consumes oracle_cards AND rulings. Watching only the first
    made a rulings-only update invisible, so production kept answering from
    stale rulings until something unrelated happened to move."""
    from scryfall_stamp import WATCHED, extract_all

    assert "rulings" in WATCHED and "oracle_cards" in WATCHED
    payload = {"data": [{"type": k, "updated_at": "2026-08-07T09:00:00+00:00"}
                        for k in WATCHED]}
    assert set(extract_all(payload)) == set(WATCHED)

    partial = {"data": [{"type": "oracle_cards",
                         "updated_at": "2026-08-07T09:00:00+00:00"}]}
    with pytest.raises(StampError, match="rulings"):
        extract_all(partial)


@pytest.mark.parametrize("bad", [" ", "", "not-a-date", 20260807, None,
                                 {"t": 1}, "2026/08/07"])
def test_a_malformed_timestamp_is_refused_not_stringified(bad):
    """Accepting any truthy value let a blank through, and a blank matches a
    substring of almost any release body — turning the change check into a
    permanent, silent "nothing new"."""
    with pytest.raises(StampError):
        extract({"data": [{"type": "oracle_cards", "updated_at": bad}]})


def test_an_italian_build_gets_its_own_release(tmp_path):
    """It is a different corpus from the same upstream data. Sharing a tag
    meant asking for aliases against a current release reported 'nothing to
    do' and silently never produced what was requested."""
    from corpus_release_id import release_id

    def build(name, aliases):
        path = _corpus(tmp_path / name)
        con = sqlite3.connect(path)
        con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
                    ("2026-08-07T09:03:15.937+00:00",))
        if aliases:
            # An alias build must record the card dump it drew them from;
            # naming one that did not is refused, and rightly.
            con.execute(
                "INSERT INTO meta VALUES ('scryfall_all_cards_updated_at', ?)",
                ("2026-08-07T04:00:00.000+00:00",))
        con.execute("CREATE TABLE card_aliases(printed_lower, lang, oracle_id)")
        con.executemany("INSERT INTO card_aliases VALUES (?,?,?)",
                        [("x", "it", str(i)) for i in range(aliases)])
        con.commit()
        con.close()
        return release_id(path)["tag"]

    plain, italian = build("plain.sqlite", 0), build("it.sqlite", 500)
    assert plain != italian
    assert "-it" in italian and "-it" not in plain


def test_cards_without_names_are_refused(tmp_path):
    """Row counts pass while the corpus is unusable: objects with oracle_id
    and no name give plenty of rows, an empty cards_fts, and nothing findable.
    setup_corpus checks cards_fts EXISTS, not that anything is in it."""
    path = _corpus(tmp_path / "c.sqlite")
    con = sqlite3.connect(path)
    con.execute("UPDATE cards SET name = ''")
    con.commit()
    con.close()
    found = problems(path)
    assert any("cards.name" in p for p in found), found


def test_an_empty_search_index_is_refused(tmp_path):
    path = _corpus(tmp_path / "c.sqlite")
    con = sqlite3.connect(path)
    con.execute("DELETE FROM cards_fts")
    con.execute("DELETE FROM rules_fts")
    con.commit()
    con.close()
    found = problems(path)
    assert any("cards_fts holds 0 rows" in p for p in found), found


def test_two_snapshots_on_one_day_do_not_collide(tmp_path):
    """Truncating the card stamp to a date meant two snapshots published on one
    UTC day — which happens — shared a tag: the second either skipped as
    already-done or collided with an existing asset and was refused. Identity
    must be at least as precise as the thing it identifies."""
    from corpus_release_id import release_id

    tags = set()
    for n, stamp in enumerate(("2026-08-07T09:03:15.937+00:00",
                               "2026-08-07T18:41:02.004+00:00")):
        path = _corpus(tmp_path / f"c{n}.sqlite")
        con = sqlite3.connect(path)
        con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)", (stamp,))
        con.commit()
        con.close()
        tags.add(release_id(path)["tag"])
    assert len(tags) == 2, tags


def test_a_rulings_only_rebuild_gets_its_own_tag(tmp_path):
    """Rulings move independently of Oracle text. A tag omitting them could not
    distinguish the rebuild from the build before it, so it skipped as
    already-done — or, when forced, collided and was refused."""
    from corpus_release_id import release_id

    tags = []
    for n, rulings in enumerate(("2026-08-07T09:00:36.125+00:00",
                                 "2026-08-14T09:00:36.125+00:00")):
        path = _corpus(tmp_path / f"r{n}.sqlite")
        con = sqlite3.connect(path)
        con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
                    ("2026-08-07T09:03:15.937+00:00",))
        con.execute("INSERT INTO meta VALUES ('scryfall_rulings_updated_at', ?)",
                    (rulings,))
        con.commit()
        con.close()
        tags.append(release_id(path)["tag"])
    assert tags[0] != tags[1], tags


def test_a_corpus_predating_the_rulings_field_still_names_a_release(tmp_path):
    """Older corpora have no scryfall_rulings_updated_at. Refusing them would
    strand exactly the artefacts this change was meant to keep working."""
    from corpus_release_id import release_id

    path = _corpus(tmp_path / "old.sqlite")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
                ("2026-08-07T09:03:15.937+00:00",))
    con.commit()
    con.close()
    assert release_id(path)["tag"].startswith("cr-20260619-cards-20260807T")


def test_an_italian_build_records_the_card_dump_it_used(tmp_path):
    """Aliases come from all_cards, which updates on its own schedule. Two
    Italian builds from different dumps looked identical without it."""
    from corpus_release_id import release_id

    tags = []
    for n, ac in enumerate(("2026-08-07T09:03:15.937+00:00",
                            "2026-08-20T09:03:15.937+00:00")):
        path = _corpus(tmp_path / f"it{n}.sqlite")
        con = sqlite3.connect(path)
        con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
                    ("2026-08-07T09:03:15.937+00:00",))
        con.execute("INSERT INTO meta VALUES ('scryfall_all_cards_updated_at', ?)",
                    (ac,))
        con.execute("CREATE TABLE card_aliases(printed_lower, lang, oracle_id)")
        con.execute("INSERT INTO card_aliases VALUES ('x','it','1')")
        con.commit()
        con.close()
        tags.append(release_id(path)["tag"])
    assert tags[0] != tags[1], tags


def test_a_barely_populated_search_index_is_refused(tmp_path):
    """One row passed a "not empty" check while almost every lookup failed —
    the same shape as accepting a corpus because it has rows."""
    path = _corpus(tmp_path / "c.sqlite")
    con = sqlite3.connect(path)
    con.execute("DELETE FROM cards_fts")
    con.execute("INSERT INTO cards_fts VALUES ('one', '1')")
    con.commit()
    con.close()
    found = problems(path)
    assert any("cards_fts holds 1 rows" in p for p in found), found


def test_half_the_cards_missing_names_is_refused(tmp_path):
    """A blanket half-tolerance let 15,000 nameless cards through. Names are
    how cards are found; the tolerance for them is near zero."""
    path = _corpus(tmp_path / "c.sqlite")
    con = sqlite3.connect(path)
    con.execute("UPDATE cards SET name = '' WHERE CAST(oracle_id AS INTEGER) % 2 = 0")
    con.commit()
    con.close()
    found = problems(path)
    assert any("cards.name" in p for p in found), found


def test_cards_without_rules_text_are_tolerated(tmp_path):
    """Plenty of real cards have no rules text — vanilla creatures, basic
    lands. A tolerance that forbade them would reject every real corpus."""
    path = _corpus(tmp_path / "c.sqlite")
    con = sqlite3.connect(path)
    # ~5%: vanilla creatures and basic lands, comfortably inside the
    # tolerance and well above the 2% the real corpus shows.
    con.execute("UPDATE cards SET oracle_text = '' "
                "WHERE CAST(oracle_id AS INTEGER) % 20 = 0")
    con.commit()
    con.close()
    assert not any("oracle_text" in p for p in problems(path))


# --- the producer's output, run through the CONSUMER that reads it ---------

CONSUMER = ROOT / "tests" / "fixtures" / "workflow_consumer.sh"


def test_the_canonical_consumer_reads_what_the_producer_prints():
    """The regression test for the bug that slipped past two hundred tests.

    The producer's output format changed while its consumer kept calling eval,
    and every scheduled build would have died executing a timestamp. The first
    version of this test fed a hard-coded string to a transcribed shell
    fragment — proving only that the copy matched the copy. The second read the
    private workflow, which does not exist in the public repository or its CI,
    so it silently skipped exactly where it needed to run.

    So the contract lives HERE, next to the producer, and is always executed.
    """
    import subprocess

    from scryfall_stamp import WATCHED, format_output

    stamps = {"oracle_cards": "2026-08-07T09:02:54.151+00:00",
              "rulings": "2026-08-07T09:00:36.125+00:00"}
    produced = format_output(stamps)

    # Line ORDER is the contract: the consumer reads positionally.
    assert produced.splitlines() == [stamps[k] for k in WATCHED]
    assert "=" not in produced, "plain values, never shell assignments"

    # Run the canonical snippet with the producer's real output, then check it
    # bound the values the right way round. Through a FILE: interpolating the
    # bytes into the script escaped the newlines, and the consumer then read
    # one line and hung on the second — a harness bug that would have read as
    # a contract failure.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        stamp_file = pathlib.Path(d) / "stamps"
        stamp_file.write_text(produced)
        stub = pathlib.Path(d) / "stub.py"
        stub.write_text("import sys, pathlib\n"
                        f"sys.stdout.write(pathlib.Path({str(stamp_file)!r})"
                        ".read_text())\n")
        out = subprocess.run(
            ["bash", "-c",
             f"set -euo pipefail\nSTAMPS={stub}\n{CONSUMER.read_text()}\n"
             'printf "%s|%s" "$oracle_cards" "$rulings"'],
            check=True, capture_output=True, text=True).stdout
    assert out == f"{stamps['oracle_cards']}|{stamps['rulings']}"


def _executable_lines(text: str) -> list[str]:
    """Strip comments and blank lines, collapse whitespace."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(" ".join(stripped.split()))
    return out


def test_the_private_workflow_contains_the_canonical_consumer_verbatim():
    """Token checks are not a contract.

    Looking for two `read` substrings in order let a real edit change the
    command inside the process substitution — adding `--kind oracle_cards`,
    say — while both reads survived: tests green, scheduled builds broken. So
    the whole executable snippet is compared, input command and guard
    included.

    Skipped where the sibling repo is absent, which is fine: the contract
    itself is enforced unconditionally by the test above. This only adds the
    cross-check when both halves are present.
    """
    workflow = (ROOT.parent / "casa-mtg-corpus" /
                ".github" / "workflows" / "build-corpus.yml")
    if not workflow.exists():
        pytest.skip("private build repository not checked out here")

    wanted = _executable_lines(CONSUMER.read_text())
    assert wanted, "the fixture has no executable content"
    text = workflow.read_text()
    present = _executable_lines(text)

    # The snippet uses $STAMPS but cannot carry its binding, since the path
    # differs by checkout. So the binding is checked separately: exactly one,
    # pointing at the real producer. Otherwise the workflow could satisfy the
    # comparison while running a different program entirely.
    bindings = [line for line in present if line.startswith("STAMPS=")]
    assert len(bindings) == 1, f"expected one STAMPS binding, got {bindings}"
    assert bindings[0].endswith("scripts/scryfall_stamp.py"), bindings[0]

    # The fixture's lines must appear in the workflow, consecutively and in
    # order — not merely somewhere, each.
    joined = "\n".join(present)
    assert "\n".join(wanted) in joined, (
        "the workflow no longer contains the canonical consumer verbatim.\n"
        "expected:\n  " + "\n  ".join(wanted))

    # RESIDUAL, stated rather than implied: this proves the lines are present,
    # consecutive, and bound to the real producer. It does not prove they are
    # REACHED — they could sit in an unexecuted branch while a different
    # consumer runs. Proving reachability means interpreting the shell, which
    # is a bigger machine than the thing it would guard. Every route to it
    # requires an owner edit to a private workflow, so the honest position is
    # to say so here rather than imply a guarantee that is not being made.


def test_the_skip_prediction_and_the_published_tag_are_one_function(tmp_path):
    """They were computed separately and disagreed, so every weekly run
    decided it had nothing, rebuilt, and then refused on an existing asset."""
    from corpus_release_id import release_id, tag_from_inputs

    oracle = "2026-08-07T09:02:54.151+00:00"
    rulings = "2026-08-07T09:00:36.125+00:00"
    path = _corpus(tmp_path / "c.sqlite", effective="August 7, 2026")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)", (oracle,))
    con.execute("INSERT INTO meta VALUES ('scryfall_rulings_updated_at', ?)",
                (rulings,))
    con.commit()
    con.close()

    predicted = tag_from_inputs(cr="20260807", oracle=oracle, rulings=rulings,
                                builder="abc1234def")
    assert release_id(path, builder="abc1234def")["tag"] == predicted


def test_a_parser_fix_can_be_published(tmp_path):
    """With unchanged upstream data a rebuilt corpus took the existing tag, so
    a published release refused it — the fix was unshippable."""
    from corpus_release_id import tag_from_inputs

    common = dict(cr="20260807", oracle="2026-08-07T09:02:54.151+00:00",
                  rulings="2026-08-07T09:00:36.125+00:00")
    assert (tag_from_inputs(**common, builder="oldsha1111")
            != tag_from_inputs(**common, builder="newsha2222"))


def test_an_alias_corpus_without_its_card_dump_stamp_refuses_a_name(tmp_path):
    """Two alias builds from different dumps would otherwise share a tag."""
    from corpus_release_id import release_id

    path = _corpus(tmp_path / "c.sqlite")
    con = sqlite3.connect(path)
    con.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
                ("2026-08-07T09:03:15.937+00:00",))
    con.execute("CREATE TABLE card_aliases(printed_lower, lang, oracle_id)")
    con.execute("INSERT INTO card_aliases VALUES ('x','it','1')")
    con.commit()
    con.close()
    with pytest.raises(ValueError, match="all_cards"):
        release_id(path)


# --- the workflows must be valid the way GITHUB reads them -----------------

def _load_strict(path):
    """Parse YAML rejecting duplicate mapping keys.

    PyYAML accepts them and keeps the last, so `yaml.safe_load` said a
    workflow with two `steps:` keys was fine while GitHub would refuse to run
    it at all. Validating with a tool that is more permissive than the
    consumer is not validating.
    """
    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def no_duplicates(loader, node, deep=False):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if key in seen:
                raise AssertionError(
                    f"duplicate key {key!r} at {key_node.start_mark}")
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_duplicates)
    return yaml.load(path.read_text(), Strict)


def test_this_repository_s_workflows_have_no_duplicate_keys():
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = _load_strict(path)
        assert doc, path


def test_the_private_build_workflow_has_no_duplicate_keys():
    """The duplicate `steps:` that escaped came from patching one file across
    many rounds. It is exactly the kind of damage a lenient parser hides."""
    workflow = (ROOT.parent / "casa-mtg-corpus" /
                ".github" / "workflows" / "build-corpus.yml")
    if not workflow.exists():
        pytest.skip("private build repository not checked out here")
    doc = _load_strict(workflow)
    steps = doc["jobs"]["build"]["steps"]
    assert steps, "the build job has no steps"
    # Every step must be reachable as a step, not stranded in a stale block.
    names = [s.get("name", s.get("uses", "?")) for s in steps]
    assert len(names) == len(set(names)), f"duplicated steps: {names}"


def test_a_404_classifies_as_absent_under_pipefail():
    """`gh api ... | grep -q "Not Found"` reads as obviously correct and is
    not: gh exits nonzero on a 404, so under `pipefail` the pipeline is
    nonzero even when grep matched. Every build would have stopped at "could
    not determine whether the tag exists" — a bug introduced BY adding a
    safety setting, which is why the shape is worth a test rather than a
    careful reading.
    """
    import subprocess

    def classify(script_body):
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + script_body],
            capture_output=True, text=True).stdout.strip()

    piped = '''
        fake() { echo "gh: Not Found (HTTP 404)" >&2; return 1; }
        if fake 2>&1 | grep -q "Not Found"; then echo absent; else echo failure; fi
    '''
    assert classify(piped) == "failure", (
        "if this ever passes, the pipefail interaction has changed and the "
        "comment explaining the fix is misleading")

    separated = '''
        fake() { echo "gh: Not Found (HTTP 404)" >&2; return 1; }
        if out=$(fake 2>&1); then echo present
        elif printf "%s" "$out" | grep -q "Not Found"; then echo absent
        else echo failure; fi
    '''
    assert classify(separated) == "absent"

    server_error = separated.replace("Not Found (HTTP 404)", "HTTP 500")
    assert classify(server_error) == "failure", (
        "a real failure must not be mistaken for an absence")


def test_the_private_workflow_does_not_pipe_gh_into_grep():
    """The shape that caused it, guarded directly."""
    workflow = (ROOT.parent / "casa-mtg-corpus" /
                ".github" / "workflows" / "build-corpus.yml")
    if not workflow.exists():
        pytest.skip("private build repository not checked out here")
    offenders = [line.strip() for line in workflow.read_text().splitlines()
                 if "gh api" in line and "| grep" in line]
    assert not offenders, offenders


def test_only_a_real_404_permits_deletion():
    """Two versions of this rule were wrong in the same direction.

    First, cleanup treated every failed lookup as "no release" and deleted the
    tag — so a 500 or a timeout removed the tag of a release that exists. Then
    the fix proved a 404 by grepping the phrase "Not Found" in diagnostic
    text, which any other failure can contain. The comments said "proven 404"
    while the code matched a phrase; a claim and a proof are not the same.

    It parses the status line now, and only 404 authorises a delete.
    """
    import subprocess

    def decide(status_line):
        body = f'''
            parse() {{ printf "%s\\n" "$1" | sed -nE "1s@^HTTP/[0-9.]+ ([0-9]{{3}}).*@\\1@p"; }}
            s=$(parse "{status_line}")
            case "$s" in 2*) echo keep-exists ;; 404) echo delete ;;
              *) echo "keep-status-${{s:-none}}" ;; esac
        '''
        return subprocess.run(["bash", "-c", "set -uo pipefail\n" + body],
                              capture_output=True, text=True).stdout.strip()

    assert decide("HTTP/2.0 404 Not Found") == "delete"
    assert decide("HTTP/2.0 200 OK") == "keep-exists"
    assert decide("HTTP/2.0 500 Internal Server Error") == "keep-status-500"
    # The precise case that defeated the phrase match.
    assert decide("HTTP/2.0 500 upstream said Not Found") == "keep-status-500"
    assert decide("curl: (7) connection refused") == "keep-status-none"


def test_the_private_workflow_never_deletes_on_an_unproven_absence():
    """The shape, guarded where it lives: any `gh api ... --silent 2>/dev/null`
    used as the sole condition before a DELETE collapses failure into
    absence."""
    workflow = (ROOT.parent / "casa-mtg-corpus" /
                ".github" / "workflows" / "build-corpus.yml")
    if not workflow.exists():
        pytest.skip("private build repository not checked out here")
    text = workflow.read_text()
    assert "method DELETE" in text, "expected the cleanup path to exist"
    # Deletion must be gated on a parsed STATUS, never on the presence of a
    # phrase in an error message.
    assert 'grep -q "Not Found"' not in text, (
        "authorising a delete by matching diagnostic text: any non-404 "
        "failure mentioning those words would qualify")

    # DOMINANCE, not a global count. Counting 404 checks anywhere in the file
    # would keep passing after an unguarded DELETE is added somewhere else, so
    # each one is checked against the lines that precede it in its own step.
    lines = text.splitlines()
    step_starts = [i for i, l in enumerate(lines) if l.startswith("      - name:")]
    for i, line in enumerate(lines):
        if "method DELETE" not in line:
            continue
        start = max([s for s in step_starts if s <= i], default=0)
        before = "\n".join(lines[start:i])
        # Two ways a deletion can be legitimate, and only two.
        #
        # The rule this test enforces is about deleting something whose state
        # was INFERRED — a tag believed abandoned because a release lookup
        # failed. There the 404 must be a parsed status, because a failed
        # lookup that is not a 404 means "we do not know", and not knowing
        # must never authorise a delete.
        #
        # Deleting an object this step just created is a different act. The
        # authority comes from having made it and still holding its id, not
        # from a lookup, so demanding a 404 there would be cargo-culting the
        # shape of the rule past the reason for it. It is still pinned: the
        # id must come from a creation in this same step.
        # The URL usually sits on a backslash continuation, so the target is
        # read from the statement, not from the line carrying the verb.
        statement = "\n".join(lines[i:i + 4])
        target_is_own_creation = (
            "releases/$created" in statement and "created=$(gh api" in before)
        if target_is_own_creation:
            continue
        assert "404" in before, (
            f"the DELETE at line {i + 1} is not preceded in its own step by a "
            "404 check; a failed lookup must never authorise a deletion")



def test_the_workflow_offers_no_force_input():
    """`force` promised to build when nothing had changed and could not: with
    an absent tag the workflow builds anyway, and with a published one force
    only turned a clean no-op into an error. An input that never enables
    anything is a lie in the UI."""
    workflow = (ROOT.parent / "casa-mtg-corpus" /
                ".github" / "workflows" / "build-corpus.yml")
    if not workflow.exists():
        pytest.skip("private build repository not checked out here")
    text = workflow.read_text()
    assert "inputs.force" not in text and "FORCE:" not in text, (
        "force is back; if it now means something, say what in the workflow")


def test_only_a_single_line_marker_claims_ownership_of_a_tag():
    """`grep -E "^...$"` matches per LINE, not per string.

    A tag message whose FIRST line is our reservation marker and whose second
    is anything at all satisfied the check — and that answer authorises a
    DELETE. So an operator could annotate a tag, or a marker could be forged
    with a trailing line, and the workflow would remove it. A multi-line
    message is not ours, full stop.
    """
    import subprocess

    marker = "casa-mtg-corpus reservation"
    body = r'''
        MARKER=$1
        check() {
          local msg=$1
          msg=${msg%$'\n'}
          case "$msg" in *$'\n'*) echo other; return ;; esac
          printf "%s" "$msg" | grep -qxE "$MARKER run [0-9]+" && echo ours || echo other
        }
        check "$2"
    '''

    def owner(message):
        return subprocess.run(
            ["bash", "-c", body, "_", marker, message],
            capture_output=True, text=True).stdout.strip()

    assert owner(f"{marker} run 481516") == "ours"
    assert owner(f"{marker} run 481516\n") == "ours", "a trailing newline is normal"

    # Both defeating shapes: the marker first with anything appended, and the
    # marker buried after an innocuous first line. Terra found the first, Sol
    # the second — a per-line anchor accepts either.
    assert owner(f"{marker} run 481516\noperator annotation") == "other"
    assert owner(f"operator note\n{marker} run 481516\ndo not delete") == "other"
    assert owner(f"{marker} run 481516\n\nanything") == "other"
    assert owner(f"{marker} by hand") == "other"
    assert owner("v1.2.3") == "other"

    # RESIDUAL, since this has caught me out three times now: the function
    # above is a COPY of the workflow's logic, not the workflow's logic. It
    # pins the rule, not the implementation. The verbatim-snippet check used
    # for the stamps consumer is the stronger pattern, and this ownership
    # test does not yet use it.


def test_a_404_assignment_does_not_kill_the_step_under_the_runner_shell():
    """The bug the first real run found, which review could not.

    GitHub invokes a step as `bash --noprofile --norc -e -o pipefail`. The
    script's own `set -uo pipefail` cannot remove that `-e`. So
    `status=$(gh api ...)` inherits the 404's nonzero exit and the step dies
    BEFORE the case that was written to interpret it — the cleanup never ran
    and left a reservation stranded on the very first execution.

    Fourteen review rounds read this shell and missed it, because reading it
    is not running it under the shell that runs it.
    """
    import subprocess

    def run(assignment):
        script = f'''
            set -uo pipefail
            fake() {{ echo "HTTP/2.0 404 Not Found"; return 1; }}
            {assignment}
            case "$status" in 404) echo reached-404 ;; *) echo "reached-$status" ;; esac
        '''
        # The RUNNER's shell, -e included.
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script],
            capture_output=True, text=True)

    naked = 'status=$(fake | sed -nE "1s@^HTTP/[0-9.]+ ([0-9]{3}).*@\\1@p")'
    assert run(naked).returncode != 0, (
        "if this passes, -e no longer propagates and the guard below is moot")

    guarded = naked + " || true"
    done = run(guarded)
    assert done.returncode == 0 and done.stdout.strip() == "reached-404"


def _logical_lines(text: str) -> list[str]:
    """Backslash continuations joined into one line each.

    This used to be inline in the test below, which accepted any line ending
    in a backslash — so a multi-line assignment with no guard anywhere in it
    passed. A reviewer removed the `|| true` from a wrapped assignment and
    the test stayed green.
    """
    logical: list[str] = []
    pending = ""
    for line in text.splitlines():
        s = line.strip()
        if s.endswith("\\"):
            pending += s[:-1].rstrip() + " "
            continue
        logical.append((pending + s).strip())
        pending = ""
    if pending:
        logical.append(pending.strip())
    return logical


# Any assignment from a command substitution, wherever it sits on the line.
# NOT a list of variable names: the previous version enumerated four
# (`status`, `now`, `still`, `final`), and a reviewer removed the guard from
# `rel=$(gh api -i ...)` — a fifth name — leaving the test reporting "five
# checked, zero offenders" while a runner-shell reproduction exited 1 on a
# 404. Whitespace defeated it too: `status=$( \` + newline joins to
# `status=$( gh api`, which does not start with `status=$(gh api`. And every
# new assignment added in ordinary maintenance was unchecked by construction.
_ASSIGNMENT = re.compile(r"(?<![\w$])(?:export\s+|local\s+)?[A-Za-z_]\w*=\$\(")
_GH_API = re.compile(r"gh\s+api\b")

# The escape hatch, deliberately explicit and deliberately noisy. Two calls
# in this workflow CREATE things (the tag object, the draft release) and must
# kill the step if they fail — guarding them would turn a failed reservation
# into a silent one. That is a real exemption, so it is written down at the
# call rather than inferred from the variable's name.
_EXEMPTION = "# unguarded-gh-api:"

# The number present when this test was last revised. Asserted as a FLOOR so
# that a matcher which stops matching fails loudly instead of silently
# checking nothing — the failure mode of every version of this test so far.
_KNOWN_GH_API_ASSIGNMENTS = 16


def _unguarded_gh_api_assignments(text: str) -> tuple[list[str], int]:
    """(offenders, total found). An offender is an assignment from a `gh api`
    command substitution that neither carries a `||` fallback nor sits under
    an explicit exemption comment.

    `bash -e` is why: GitHub runs every step under it, so `x=$(gh api ...)`
    kills the step outright when the API returns 404 — the expected outcome
    of several lookups here. That regression already happened once, stranding
    a tag reservation and killing the cleanup step that would have released
    it.
    """
    logical = _logical_lines(text)

    def _is_assignment(s: str) -> bool:
        m = _ASSIGNMENT.search(s)
        return bool(m and _GH_API.search(s[m.end():]))

    # Each exemption comment covers exactly ONE assignment: the next one,
    # with no blank line between them. One marker cannot silently cover a
    # second call added under it later, and a marker that covers nothing at
    # all is itself reported — a stale exemption left behind when the call it
    # excused was rewritten is how this kind of annotation rots.
    exempt: set[int] = set()
    orphaned: list[str] = []
    for j, s in enumerate(logical):
        if not s.startswith(_EXEMPTION):
            continue
        target = None
        for k in range(j + 1, len(logical)):
            if not logical[k]:
                break
            if _is_assignment(logical[k]) and k not in exempt:
                target = k
                break
        if target is None:
            orphaned.append(f"exemption covers no gh api assignment: {s}")
        else:
            exempt.add(target)

    offenders: list[str] = list(orphaned)
    found = 0
    for i, s in enumerate(logical):
        if not _is_assignment(s):
            continue
        found += 1
        # The fallback has to come after the call it is guarding; a `||`
        # earlier on the line is guarding something else.
        m = _ASSIGNMENT.search(s)
        gh_at = _GH_API.search(s[m.end():]).end() + m.end()
        if "||" in s[gh_at:] or i in exempt:
            continue
        offenders.append(s)
    return offenders, found


def test_the_workflow_guards_every_gh_api_assignment():
    workflow = (ROOT.parent / "casa-mtg-corpus" /
                ".github" / "workflows" / "build-corpus.yml")
    if not workflow.exists():
        pytest.skip("private build repository not checked out here")
    offenders, found = _unguarded_gh_api_assignments(workflow.read_text())
    assert not offenders, (
        "assignments from a gh call that can 404, with no fallback and no "
        f"{_EXEMPTION} exemption:\n" + "\n".join(offenders))
    assert found >= _KNOWN_GH_API_ASSIGNMENTS, (
        f"only {found} gh api assignments matched, down from "
        f"{_KNOWN_GH_API_ASSIGNMENTS}; the matcher has stopped matching and "
        "this test is now checking nothing")


@pytest.mark.parametrize("why,line", [
    ("a variable name the old prefix list did not enumerate",
     'rel=$(gh api -i "repos/$R/releases/tags/$t" 2>&1)'),
    ("whitespace between the substitution and the command",
     'status=$( gh api "repos/$R/releases/tags/$t")'),
    ("an assignment added in ordinary maintenance",
     'whatever=$(gh api "repos/$R/git/ref/tags/$t" --jq .object.sha)'),
    ("a guard that fires before the call rather than after it",
     'x=$(false || true; gh api "repos/$R/releases")'),
])
def test_the_guard_scanner_catches_the_ways_past_the_old_one(why, line):
    """Each of these was demonstrated against the previous version, which
    matched four literal variable-name prefixes and reported success over an
    unguarded call."""
    offenders, found = _unguarded_gh_api_assignments(f"        {line}\n")
    assert found == 1, why
    assert offenders == [line], why


def test_removing_any_single_guard_from_the_real_workflow_is_caught():
    """The mutation check, kept rather than re-run by hand: strip the
    fallback from each guarded assignment in turn and confirm the scanner
    names that one. Without this, the scanner could be silently narrowed
    back to something that matches everything and objects to nothing."""
    workflow = (ROOT.parent / "casa-mtg-corpus" /
                ".github" / "workflows" / "build-corpus.yml")
    if not workflow.exists():
        pytest.skip("private build repository not checked out here")
    logical = _logical_lines(workflow.read_text())
    base, found = _unguarded_gh_api_assignments(workflow.read_text())
    assert not base and found >= _KNOWN_GH_API_ASSIGNMENTS

    mutated = 0
    for i, s in enumerate(logical):
        m = _ASSIGNMENT.search(s)
        if not m or not _GH_API.search(s[m.end():]) or "||" not in s:
            continue
        stripped = re.sub(r"\s*\|\|.*$", "", s)
        offenders, _ = _unguarded_gh_api_assignments(
            "\n".join(logical[:i] + [stripped] + logical[i + 1:]))
        assert stripped in offenders, f"removing the guard went unnoticed: {s}"
        mutated += 1
    assert mutated >= 13, (
        f"only {mutated} guarded assignments were mutated; the workflow's "
        "guards are not where this test thinks they are")


def test_rulings_that_reference_no_card_are_refused():
    """A reviewer built a corpus with 60,000 well-formed ruling comments whose
    oracle_id was uniformly NULL and it passed clean. get_rulings joins on
    oracle_id, so every one of those rulings is unreachable — the corpus
    answers "no rulings" for every card while satisfying the row-count floor
    and the blank-comment tolerance."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = _corpus(Path(d) / "c.sqlite")
        con = sqlite3.connect(path)
        con.execute("UPDATE rulings SET oracle_id = NULL")
        con.commit()
        con.close()
        found = problems(path)
    assert any("carry no oracle_id" in p for p in found), found


def test_rulings_pointing_at_absent_cards_are_refused():
    """The two datasets have to belong to each other. Rulings paired with a
    card set they do not describe join to nothing, which looks identical to a
    healthy corpus from every count-based angle."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = _corpus(Path(d) / "c.sqlite")
        con = sqlite3.connect(path)
        con.execute("UPDATE rulings SET oracle_id = 'no-such-' || oracle_id")
        con.commit()
        con.close()
        found = problems(path)
    assert any("absent from cards" in p for p in found), found


def test_a_syntactically_perfect_feed_of_invented_cards_is_refused():
    """Every shape check passes on fabricated data: the counts are right, the
    columns are populated, the indexes are proportionate. What a substituted
    dataset cannot have is the cards that have been in print for thirty
    years."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = _corpus(Path(d) / "c.sqlite")
        con = sqlite3.connect(path)
        con.execute("UPDATE cards SET name = 'Invented ' || oracle_id")
        con.commit()
        con.close()
        found = problems(path)
    assert any("anchor cards" in p for p in found), found


def test_numbered_garbage_without_the_anchor_rules_is_refused():
    """Rules numbered plausibly and filled with text satisfy the count, the
    non-empty check and the subrule shape. CR 100.1 and 702.2 have been
    numbered the same for the life of the document."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = _corpus(Path(d) / "c.sqlite")
        con = sqlite3.connect(path)
        con.execute("DELETE FROM rules WHERE rule_id IN ('100.1', '702.2')")
        con.commit()
        con.close()
        found = problems(path)
    assert sum("does not look like the Comprehensive Rules" in p
               for p in found) == 2, found


def test_an_index_of_unrelated_text_is_refused():
    """A reviewer built a corpus whose rules_fts had exactly the right number
    of rows and indexed unrelated text. It passed every count and ratio check,
    and the server then answered "no rules match" for a phrase present in
    every single rule. Quantity is not correspondence."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = _corpus(Path(d) / "c.sqlite")
        con = sqlite3.connect(path)
        con.execute("DELETE FROM rules_fts")
        con.executemany(
            "INSERT INTO rules_fts VALUES (?,?)",
            [(str(i), "unrelated filler") for i in range(3002)])
        con.commit()
        con.close()
        found = problems(path)
    assert any("does not index rules" in p for p in found), found


def test_a_card_index_of_unrelated_text_is_refused():
    """Same hole on the card side: every name lookup fails while the index
    looks correctly sized."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = _corpus(Path(d) / "c.sqlite")
        con = sqlite3.connect(path)
        con.execute("DELETE FROM cards_fts")
        con.executemany(
            "INSERT INTO cards_fts VALUES (?,?)",
            [("zzzz", str(i)) for i in range(30000)])
        con.commit()
        con.close()
        found = problems(path)
    assert any("does not index cards" in p for p in found), found


def test_the_readme_test_count_matches_the_suite():
    """The README has claimed 202, 210, 214 and 220 tests at various points,
    each time because someone updated the suite and not the sentence. It is a
    small thing, but it is the sort of small thing a reader uses to judge
    whether the rest of the document was checked."""
    import re
    import subprocess

    readme = (ROOT / "README.md").read_text()
    m = re.search(r"^(\d+) tests, no network", readme, re.M)
    assert m, "README no longer states a test count in the expected shape"
    claimed = int(m.group(1))

    run = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "-q",
         "--collect-only"],
        capture_output=True, text=True, cwd=ROOT)
    # The return code matters: a collection error also prints a count, and
    # parsing stdout alone would compare the README against a partial suite.
    assert run.returncode == 0, (
        f"collection failed ({run.returncode}):\n{run.stdout[-600:]}")
    out = run.stdout
    n = re.search(r"^(\d+) tests collected", out, re.M)
    assert n, f"could not read a collected count from pytest:\n{out[-400:]}"
    assert claimed == int(n.group(1)), (
        f"README says {claimed} tests; the suite collects {n.group(1)}")


def test_two_snapshots_in_the_same_second_get_different_tags():
    """The readable stamp is truncated to whole seconds, so two genuinely
    different Scryfall snapshots produced one tag. The plan then reserves
    snapshot A's tag, the builder downloads snapshot B, and the publish-time
    comparison agrees because both sides computed the same wrong name."""
    from corpus_release_id import tag_from_inputs

    common = dict(cr="20260807", rulings="2026-08-07T09:00:38+00:00",
                  builder="bf025fc9aa")
    a = tag_from_inputs(oracle="2026-08-07T09:03:15.100+00:00", **common)
    b = tag_from_inputs(oracle="2026-08-07T09:03:15.900+00:00", **common)
    assert a != b, f"both snapshots named {a}"


def test_one_instant_expressed_two_ways_gets_one_tag():
    """The mirror of the above: an offset change alone must not rename a
    release, or the same snapshot would be published twice."""
    from corpus_release_id import tag_from_inputs

    common = dict(cr="20260807", builder="bf025fc9aa")
    utc = tag_from_inputs(oracle="2026-08-07T09:03:15.100+00:00",
                          rulings="2026-08-07T09:00:38Z", **common)
    plus2 = tag_from_inputs(oracle="2026-08-07T11:03:15.100+02:00",
                            rulings="2026-08-07T11:00:38+02:00", **common)
    assert utc == plus2, f"{utc} != {plus2}"


@pytest.mark.parametrize("a,b,same,why", [
    ("2026-08-07T09:03:15.100Z", "2026-08-07T09:03:15.900Z", False,
     "sub-second snapshots are different snapshots"),
    ("2026-08-07T09:03:15.1000001Z", "2026-08-07T09:03:15.1000009Z", False,
     "precision beyond what datetime models still distinguishes moments"),
    ("2026-08-07T11:03:15.1+02:00", "2026-08-07T09:03:15.100Z", True,
     "one instant written two ways is one snapshot"),
    ("2026-08-07", "2026-08-07T00:00:00Z", True,
     "the bare-date fallback means midnight, not a shape of its own"),
    ("2026-08-07T09:03:15.1Z", "2026-08-07T09:03:15.1000Z", True,
     "trailing zeros carry no information"),
    ("2026-08-07T09:03:15Z", "2026-08-07T09:03:15.5Z", False,
     "no fraction is not the same as some fraction"),
])
def test_release_tag_identity(a, b, same, why):
    """Two properties, and both have been broken: different snapshots must
    never share a tag (the plan reserves one snapshot's name while the builder
    downloads another, and the publish-time comparison agrees because both
    sides computed the same wrong string), and one snapshot must never produce
    two (the same corpus published twice under different names)."""
    from corpus_release_id import tag_from_inputs

    common = dict(cr="20260807", rulings="2026-08-07T09:00:38Z",
                  builder="bf025fc9aa1234")
    ta = tag_from_inputs(oracle=a, **common)
    tb = tag_from_inputs(oracle=b, **common)
    assert (ta == tb) is same, f"{why}: {ta} vs {tb}"


def test_the_release_tag_suffix_is_wide_enough_to_not_collide():
    """A reviewer collided the 32-bit suffix by deterministic search after
    126,930 candidates — two snapshots, one tag. The birthday bound on 64 bits
    puts that out of reach; this pins the width so it cannot be trimmed back
    for tidiness."""
    from corpus_release_id import tag_from_inputs

    tag = tag_from_inputs(cr="20260807", oracle="2026-08-07T09:03:15Z",
                          builder="bf025fc9aa1234")
    suffix = tag.rsplit("-s", 1)[1]
    assert len(suffix) == 16, f"suffix {suffix!r} is {len(suffix) * 4} bits"

    # And the search that found the old collision finds nothing here.
    seen = {}
    for micro in range(200000):
        stamp = f"2026-08-07T09:03:{micro // 1000000 % 60:02d}.{micro % 1000000:06d}Z"
        t = tag_from_inputs(cr="20260807", oracle=stamp, builder="b")
        assert t not in seen, f"collision: {stamp} and {seen[t]}"
        seen[t] = stamp


def _fts_attack_corpus(tmp_path, damage: str):
    """A healthy fixture corpus with its index damaged.

    Built from the FIXTURE, not from a real corpus. The first version of
    these tests required plugins/mtg/data/corpus.sqlite and skipped without
    it — so in the public repository, which contains no corpus by design and
    is the only place this code ships from, all four skipped and CI went
    green over the very protection they exist to hold in place. That is the
    third time a check in this repository has reported success while
    exercising nothing.

    The claim that justified it — that a synthetic fixture cannot express
    these defects — was simply wrong: the fixture creates genuine fts5
    virtual tables, so it has a genuine inverted index to damage.
    """
    path = _corpus(tmp_path / "damaged.sqlite")
    con = sqlite3.connect(path)
    con.executescript(damage)
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize("name,expected,damage", [
    ("virtual table swapped for an ordinary one", "cannot be searched", """
        CREATE TABLE rsrc AS SELECT rule_id, text FROM rules_fts;
        DROP TABLE rules_fts;
        CREATE TABLE rules_fts(rule_id TEXT, text TEXT);
        INSERT INTO rules_fts SELECT rule_id, text FROM rsrc;
        DROP TABLE rsrc;"""),
    ("shadow index blocks deleted", "cannot be searched",
     "DELETE FROM rules_fts_data WHERE id > 1;"),
    ("external content echoing an empty index", "not a working index", """
        CREATE TABLE rsrc(rule_id TEXT, text TEXT);
        INSERT INTO rsrc SELECT rule_id, text FROM rules;
        DROP TABLE rules_fts;
        CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text, content='rsrc');"""),
])
def test_an_index_that_cannot_be_searched_is_refused(tmp_path, name, expected,
                                                     damage):
    """Reading the columns an FTS table exposes as content is not reading the
    inverted index, and the two can part company. Every one of these passed a
    checker that compared stored strings: the content was right in each case
    and MATCH returned nothing, raised "no such column", or reported the
    database malformed — which the server renders to the user as
    "no rules match".

    Asserted on the PROBE'S OWN finding, not on the existence of any finding.
    `assert problems(...)` looked equivalent and was not: deleting the shadow
    blocks breaks the virtual table early enough that the row-count and
    content checks fail on it too, so that case kept passing when a reviewer
    deleted the probe it exists to pin. A test that survives the removal of
    the thing it tests is the same defect this file is full of tests for.
    """
    found = problems(_fts_attack_corpus(tmp_path, damage))
    assert any(expected in p for p in found), f"{name}: {found}"


def test_an_index_covering_only_the_rows_a_probe_would_reach_is_refused(tmp_path):
    """Both reviewers built this one. An external-content index that exposes
    every row but holds postings for only the first 300 defeats any probe
    with a fixed bound — which is what the previous version had, 300 rows,
    the same predictable-prefix mistake that had just been removed from the
    correspondence check. Roughly 90% of rules had no postings and the
    corpus was declared healthy."""
    path = _fts_attack_corpus(tmp_path, """
        CREATE TABLE rsrc(rule_id TEXT, text TEXT);
        INSERT INTO rsrc SELECT rule_id, text FROM rules;
        DROP TABLE rules_fts;
        CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text, content='rsrc');
        INSERT INTO rules_fts(rowid, rule_id, text)
            SELECT rowid, rule_id, text FROM rsrc WHERE rowid <= 300;""")
    found = problems(path)
    assert any("not a working index" in p for p in found), found


def test_an_unindexable_key_does_not_excuse_a_missing_posting(tmp_path):
    """A reviewer found this in the fix that introduced the exemption.

    The keyed probe puts the row's key in the query as well as its text, so a
    key that tokenises to nothing makes the row unfindable BY THE PROBE
    however complete the index is. The first version read that as "the value
    is unindexable" and excused the row — so a rule whose id tokenises to
    nothing, whose text tokenises perfectly well, and whose posting had been
    removed was reported as unindexable text and the corpus passed clean.
    The key is a property of the probe, not of the data.
    """
    path = _fts_attack_corpus(tmp_path, """
        UPDATE rules SET rule_id = '_' WHERE rule_id = '5';
        CREATE TABLE rsrc(rule_id TEXT, text TEXT);
        INSERT INTO rsrc SELECT rule_id, text FROM rules;
        DROP TABLE rules_fts;
        CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text, content='rsrc');
        INSERT INTO rules_fts(rowid, rule_id, text)
            SELECT rowid, rule_id, text FROM rsrc WHERE rule_id != '_';""")
    found = problems(path)
    assert any("not a working index" in p for p in found), found


def test_an_unindexable_key_whose_posting_is_present_is_still_accepted(tmp_path):
    """The other direction, and the one that makes the fix above safe to
    apply: re-probing without the key must FIND the row when the index really
    does hold it. Refusing here would be the failure this gate has had twice
    already — rejecting a corpus that is perfectly good."""
    path = _fts_attack_corpus(tmp_path, """
        UPDATE rules SET rule_id = '_' WHERE rule_id = '5';
        CREATE TABLE rsrc(rule_id TEXT, text TEXT);
        INSERT INTO rsrc SELECT rule_id, text FROM rules;
        DROP TABLE rules_fts;
        CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text, content='rsrc');
        INSERT INTO rules_fts(rowid, rule_id, text)
            SELECT rowid, rule_id, text FROM rsrc;""")
    assert problems(path) == []


def test_deleting_the_searchability_probe_makes_a_damaged_corpus_pass(tmp_path):
    """The mutation check, kept rather than re-run by hand.

    Every other test in this group asserts that a damaged corpus is refused.
    None of them can say whether the probe is what refuses it — a reviewer
    deleted the probe from the checker, re-ran them, and one kept passing on
    findings raised by unrelated checks. So run the mutation here: excise the
    probe, and require that the checker then ACCEPTS an index that holds no
    postings at all. If this test starts failing, either the markers moved or
    some other check has grown to cover this, and the answer is to find out
    which rather than to relax it.
    """
    source = (ROOT / "scripts" / "check_corpus_plausible.py").read_text()
    lines = source.splitlines(keepends=True)
    begin = [i for i, l in enumerate(lines) if "BEGIN searchability probe" in l]
    end = [i for i, l in enumerate(lines) if "END searchability probe" in l]
    assert len(begin) == 1 and len(end) == 1 and begin[0] < end[0], (
        "the searchability probe markers are gone; this test cannot excise "
        "what it cannot find")
    without_probe = "".join(lines[:begin[0]] + lines[end[0] + 1:])

    module: dict = {"__name__": "check_corpus_plausible_without_probe"}
    exec(compile(without_probe, "check_corpus_plausible.py<no probe>", "exec"),
         module)

    path = _fts_attack_corpus(tmp_path, """
        CREATE TABLE rsrc(rule_id TEXT, text TEXT);
        INSERT INTO rsrc SELECT rule_id, text FROM rules;
        DROP TABLE rules_fts;
        CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text, content='rsrc');""")
    assert module["problems"](path) == [], (
        "an index with no postings was still refused without the probe, so "
        "the FTS tests above are not pinning the probe they name")
    # And the unmutated checker does refuse it — otherwise the mutation
    # above proves nothing about the code that actually ships.
    assert any("not a working index" in p for p in problems(path))


def test_the_real_corpus_passes_when_one_is_present(tmp_path):
    """The check that matters most, and the one a previous version failed: a
    gate that rejects the only known-good input would have blocked every
    build. It rejected the real corpus twice — over a trailing space, then
    over tokenisation. This can only run where a corpus has been built, so
    it is a bonus on top of the fixture-based cases above rather than the
    protection itself."""
    real = ROOT / "plugins" / "mtg" / "data" / "corpus.sqlite"
    if not real.is_file():
        pytest.skip("no built corpus here; the fixture cases carry the coverage")
    assert problems(real) == []


def test_builder_revisions_sharing_a_prefix_get_different_tags():
    """The readable part carries a prefix of the builder revision, so two
    revisions agreeing on it produced one tag. A parser fix over unchanged
    upstream data yields a different corpus, and naming it identically is
    precisely what the builder component exists to prevent — the earlier test
    used revisions differing at the first character and never exercised the
    truncation."""
    from corpus_release_id import tag_from_inputs

    common = dict(cr="20260807", oracle="2026-08-07T09:03:15Z",
                  rulings="2026-08-07T09:00:38Z")
    a = tag_from_inputs(builder="aaaaaaaaaaaa" + "1" * 28, **common)
    b = tag_from_inputs(builder="aaaaaaaaaaaa" + "2" * 28, **common)
    assert a != b, f"both builder revisions named {a}"
