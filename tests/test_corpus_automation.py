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
    con.executemany("INSERT INTO rules_fts VALUES (?,?)",
                    [(str(i), "rule text") for i in range(rules)])
    con.executemany("INSERT INTO cards_fts VALUES (?,?)",
                    [("n", str(i)) for i in range(cards)])
    con.executemany(
        "INSERT INTO rules VALUES (?,?,?,?)",
        [(str(i), str(i) if i < subrules else None,
          "" if i < empty_text else "rule text", "") for i in range(rules)])
    con.executemany("INSERT INTO glossary VALUES (?,?)",
                    [(str(i), "d") for i in range(glossary)])
    con.executemany("INSERT INTO cards VALUES (?,?,?)",
                    [(str(i), f"Card {i}", "rules text") for i in range(cards)])
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
    assert release_id(a)["tag"] == "cr-20260619-cards-20260807T090315"


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
    assert info["tag"] == "cr-20260807-cards-20260807T090315"


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
