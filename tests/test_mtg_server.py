"""MCP server tool-function tests against a tiny fixture corpus."""
import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "corpus.sqlite"
    con = sqlite3.connect(p)
    con.executescript("""
      CREATE TABLE rules(rule_id TEXT PRIMARY KEY, parent_id TEXT, text TEXT, examples TEXT);
      CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text);
      CREATE TABLE glossary(term TEXT PRIMARY KEY, definition TEXT);
      CREATE TABLE cards(oracle_id TEXT PRIMARY KEY, name TEXT, name_lower TEXT,
                         type_line TEXT, oracle_text TEXT, keywords TEXT);
      CREATE VIRTUAL TABLE cards_fts USING fts5(name, oracle_id UNINDEXED);
      CREATE TABLE card_aliases(printed_lower TEXT, lang TEXT, oracle_id TEXT);
      CREATE TABLE rulings(oracle_id TEXT, published_at TEXT, source TEXT, comment TEXT);
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    con.execute("INSERT INTO rules VALUES ('702.2','',  'Deathtouch is a keyword.','')")
    con.execute("INSERT INTO rules VALUES ('702.2b','702.2','Destroyed as a state-based action. See rule 704.','Example: fight.')")
    con.execute("INSERT INTO rules VALUES ('704.1','','SBAs are checked continuously.','')")
    con.executemany("INSERT INTO rules_fts VALUES (?,?)",
                    [("702.2", "Deathtouch is a keyword."),
                     ("702.2b", "Destroyed as a state-based action."),
                     ("704.1", "SBAs are checked continuously.")])
    con.execute("INSERT INTO glossary VALUES ('Deathtouch','See rule 702.2.')")
    con.execute("INSERT INTO cards VALUES ('oid1','Grizzly Bears','grizzly bears','Creature — Bear','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Grizzly Bears','oid1')")
    con.execute("INSERT INTO card_aliases VALUES ('orso grizzly','it','oid1')")
    con.execute("INSERT INTO rulings VALUES ('oid1','2020-01-01','wotc','Bears rule.')")
    con.execute("INSERT INTO meta VALUES ('cr_effective_date','2026-06-01')")
    con.commit(); con.close()
    import plugins.mtg.server.mtg_server as srv
    monkeypatch.setattr(srv, "DB_PATH", p)
    srv._reset_db()
    return srv


def test_lookup_rule_includes_subrules_and_crossrefs(db):
    out = db.tool_lookup_rule({"rule_id": "702.2"})
    assert "702.2b" in out and "704.1" in out  # subrule + one-hop crossref
    assert "corpus_info" in out


def test_lookup_card_exact_and_alias(db):
    assert "Grizzly Bears" in db.tool_lookup_card({"name": "grizzly bears"})
    assert "Grizzly Bears" in db.tool_lookup_card(
        {"name": "Orso Grizzly", "lang": "it"})


def test_lookup_card_ambiguous_returns_multiple_candidates(db):
    # Seed two plausible fuzzy matches so "candidates" is non-vacuous.
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO cards VALUES ('oid2','Grizzly Bears Token','grizzly bears token','Token Creature — Bear','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Grizzly Bears Token','oid2')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "grizly bears"})  # typo, no exact row
    assert "candidates:" in out.lower()
    assert "Grizzly Bears" in out and "Grizzly Bears Token" in out


def test_lookup_card_true_miss_says_not_found_with_no_candidates(db):
    out = db.tool_lookup_card({"name": "zzqqxx"})
    assert "not found" in out.lower()


def test_rulings_labelled_by_source(db):
    out = db.tool_get_rulings({"oracle_id": "oid1"})
    assert "[wotc]" in out


def test_search_rules_bounded_counts_rows_not_substrings(db):
    con = sqlite3.connect(db.DB_PATH)
    for i in range(20):
        rid = f"999.{i}"
        con.execute("INSERT INTO rules VALUES (?, '', 'state-based widget', '')", (rid,))
        con.execute("INSERT INTO rules_fts VALUES (?, 'state-based widget')", (rid,))
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_search_rules({"terms": "state-based", "limit": 99})
    # count RULE rows (lines starting with a rule id), not the substring "70"
    import re
    rows = [ln for ln in out.splitlines() if re.match(r"^\d{3}\.\d+", ln)]
    assert len(rows) <= 8


# --- corpus_info carries the sidecar artifact sha256 -----------------------

def test_corpus_info_includes_artifact_sha256(db):
    sha_path = Path(str(db.DB_PATH) + ".sha256")
    sha_path.write_text(
        "deadbeefcafef00d1234567890abcdef1234567890abcdef1234567890abcdef  corpus.sqlite\n")
    out = db.tool_lookup_rule({"rule_id": "702.2"})
    assert "artifact_sha256=deadbeefcafef00d" in out


def test_corpus_info_artifact_sha256_unknown_when_sidecar_missing(db):
    # db.DB_PATH points at tmp_path/corpus.sqlite with no .sha256 sidecar.
    out = db.tool_lookup_rule({"rule_id": "702.2"})
    assert "artifact_sha256=unknown" in out


# --- DFC/split face-name lookup ----------------------------------------------

def test_lookup_card_finds_dfc_by_front_face_name(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute(
        "INSERT INTO cards VALUES ('oid-dfc','Delver of Secrets // Insectile Aberration',"
        "'delver of secrets // insectile aberration','Creature — Human Wizard','','[]')")
    # The builder indexes each face name into cards_fts, not just the canonical name.
    con.execute("INSERT INTO cards_fts VALUES ('Delver of Secrets','oid-dfc')")
    con.execute("INSERT INTO cards_fts VALUES ('Insectile Aberration','oid-dfc')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Delver of Secrets"})
    assert "Delver of Secrets // Insectile Aberration" in out
    assert "not found" not in out.lower()


# --- alias collisions return candidates, not a silent pick ------------------

def test_lookup_card_alias_collision_returns_candidates(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO cards VALUES ('oid-a','Card Alpha','card alpha','Creature','','[]')")
    con.execute("INSERT INTO cards VALUES ('oid-b','Card Beta','card beta','Creature','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Card Alpha','oid-a')")
    con.execute("INSERT INTO cards_fts VALUES ('Card Beta','oid-b')")
    # Same printed_lower/lang alias resolves to two different oracle_ids
    # (real corpus has this collision pattern, e.g. 'abbattere'/it).
    con.execute("INSERT INTO card_aliases VALUES ('carta collisa','it','oid-a')")
    con.execute("INSERT INTO card_aliases VALUES ('carta collisa','it','oid-b')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Carta Collisa", "lang": "it"})
    assert "candidates" in out.lower()
    assert "Card Alpha" in out and "Card Beta" in out


def test_lookup_card_alias_unambiguous_still_resolves(db):
    out = db.tool_lookup_card({"name": "Orso Grizzly", "lang": "it"})
    assert "Grizzly Bears" in out
    assert "candidates" not in out.lower()


# --- rulings source labeling -------------------------------------------------

def test_rulings_scryfall_source_labelled_as_note(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO rulings VALUES ('oid1','2021-05-05','scryfall','Unofficial commentary.')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_get_rulings({"oracle_id": "oid1"})
    assert "[scryfall_note]" in out
    assert "[wotc]" in out


def test_rulings_unknown_source_passes_through(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO rulings VALUES ('oid1','2022-01-01','judge','Judge commentary.')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_get_rulings({"oracle_id": "oid1"})
    assert "[judge]" in out


# --- search_rules hardening --------------------------------------------------

def test_search_rules_fts_injection_safe_never_raises(db):
    # A hyphen plus an unbalanced quote is FTS5 syntax poison if unquoted.
    out = db.tool_search_rules({"terms": 'state-based "702', "limit": 5})
    assert isinstance(out, str)
    assert "corpus_info" in out


def test_search_rules_rule_number_goes_direct_to_rules_table(db):
    out = db.tool_search_rules({"terms": "702.2", "limit": 5})
    assert "702.2b" in out  # subrule included, bypassing FTS
    assert "corpus_info" in out


# =============================================================================
# Regression tests for the server's adversarial-input contract
# =============================================================================

# --- lookup_card collects ALL matches, dedupes by oracle_id ----------------

def test_lookup_card_multi_oracle_exact_face_collision_returns_candidates(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO cards VALUES ('oid-e1','Elemental One','elemental one','Creature — Elemental','','[]')")
    con.execute("INSERT INTO cards VALUES ('oid-e2','Elemental Two','elemental two','Creature — Elemental','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Elemental','oid-e1')")
    con.execute("INSERT INTO cards_fts VALUES ('Elemental','oid-e2')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Elemental"})
    assert "candidates" in out.lower()
    assert "Elemental One" in out and "Elemental Two" in out
    assert "oid-e1" in out and "oid-e2" in out


def test_lookup_card_dfc_face_resolves_correct_card_among_multiple_dfcs(db):
    con = sqlite3.connect(db.DB_PATH)
    # Two distinct DFCs live in the corpus; a bare/unscoped fetchone() over
    # cards_fts (no exact WHERE filtering) could return either. The exact
    # face-name query must resolve precisely to the one that was asked for.
    con.execute(
        "INSERT INTO cards VALUES ('oid-delver','Delver of Secrets // Insectile Aberration',"
        "'delver of secrets // insectile aberration','Creature — Human Wizard','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Delver of Secrets','oid-delver')")
    con.execute("INSERT INTO cards_fts VALUES ('Insectile Aberration','oid-delver')")
    con.execute(
        "INSERT INTO cards VALUES ('oid-jace',\"Jace, Vryn's Prodigy // Jace, Telepath Unbound\","
        "\"jace, vryn's prodigy // jace, telepath unbound\",'Legendary Creature — Human Wizard','','[]')")
    con.execute("INSERT INTO cards_fts VALUES (\"Jace, Vryn's Prodigy\",'oid-jace')")
    con.execute("INSERT INTO cards_fts VALUES ('Jace, Telepath Unbound','oid-jace')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Delver of Secrets"})
    assert "Delver of Secrets // Insectile Aberration" in out
    assert "not found" not in out.lower()
    assert "Jace" not in out


def test_lookup_card_alias_vs_exact_disagreement_returns_candidates(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO cards VALUES ('oid-inferno-en','Inferno','inferno','Sorcery','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Inferno','oid-inferno-en')")
    con.execute("INSERT INTO cards VALUES ('oid-inferno-other','Inferno Elemental','inferno elemental','Creature','','[]')")
    con.execute("INSERT INTO card_aliases VALUES ('inferno','it','oid-inferno-other')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Inferno", "lang": "it"})
    assert "candidates" in out.lower()
    assert "Inferno" in out
    assert "Inferno Elemental" in out


# --- limit clamping -----------------------------------------------------------

def test_search_rules_negative_and_zero_limit_clamp_to_one(db):
    con = sqlite3.connect(db.DB_PATH)
    for i in range(20):
        rid = f"999.{i}"
        con.execute("INSERT INTO rules VALUES (?, '', 'state-based widget', '')", (rid,))
        con.execute("INSERT INTO rules_fts VALUES (?, 'state-based widget')", (rid,))
    con.commit(); con.close()
    db._reset_db()
    import re
    for lim in (-1, 0):
        out = db.tool_search_rules({"terms": "state-based", "limit": lim})
        rows = [ln for ln in out.splitlines() if re.match(r"^\d{3}\.\d+", ln)]
        assert len(rows) == 1, f"limit={lim} produced {len(rows)} rows"


def test_search_rules_schema_advertises_integer_bounded_limit(db):
    defs = {d["name"]: d for d in db._tool_defs()}
    limit_schema = defs["search_rules"]["inputSchema"]["properties"]["limit"]
    assert limit_schema["type"] == "integer"
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 8


# --- bounded output / truncated echo -----------------------------------------

def test_lookup_card_huge_name_produces_bounded_response(db):
    huge = "x" * 70_000
    out = db.tool_lookup_card({"name": huge})
    assert len(out) < 1000
    assert huge not in out


# --- tools/call error contract -----------------------------------------------

def test_handle_unknown_tool_is_result_iserror_not_top_level_error(db):
    resp = db._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "does_not_exist", "arguments": {}}})
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    assert "corpus_info" in resp["result"]["content"][0]["text"]


def test_handle_null_arguments_is_iserror_true(db):
    resp = db._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "lookup_rule", "arguments": None}})
    assert resp["result"]["isError"] is True
    assert "corpus_info" in resp["result"]["content"][0]["text"]


def test_handle_tool_exception_is_iserror_true_with_corpus_info(db, monkeypatch):
    def _boom(_args):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(db.TOOLS, "lookup_rule", (_boom, db.TOOLS["lookup_rule"][1]))
    resp = db._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "lookup_rule", "arguments": {"rule_id": "702.2"}}})
    assert resp["result"]["isError"] is True
    assert "corpus_info" in resp["result"]["content"][0]["text"]


def test_tool_error_falls_back_to_corpus_info_unavailable_when_meta_read_fails(db, monkeypatch):
    def _boom():
        raise sqlite3.OperationalError("no such table: meta")
    monkeypatch.setattr(db, "_corpus_info", _boom)
    resp = db._handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "nope", "arguments": {}}})
    assert resp["result"]["isError"] is True
    assert "corpus_info: unavailable" in resp["result"]["content"][0]["text"]


# --- JSON-RPC framing ---------------------------------------------------------

def test_handle_notification_tools_call_no_id_no_response(db):
    resp = db._handle({"jsonrpc": "2.0", "method": "tools/call",
                        "params": {"name": "lookup_term", "arguments": {"term": "Deathtouch"}}})
    assert resp is None


def test_handle_null_body_is_invalid_request(db):
    resp = db._handle(None)
    assert resp["error"]["code"] == -32600
    assert resp["id"] is None


def test_handle_non_object_json_bodies_are_invalid_request(db):
    for body in ([], "x", 5):
        resp = db._handle(body)
        assert resp["error"]["code"] == -32600
        assert resp["id"] is None


def test_handle_params_null_treated_as_empty_object(db):
    resp = db._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": None})
    assert resp["result"]["serverInfo"]["name"] == "mtg"


def test_handle_wrong_jsonrpc_version_with_id_is_invalid_request(db):
    resp = db._handle({"jsonrpc": "1.0", "id": 1, "method": "initialize"})
    assert resp["error"]["code"] == -32600


def test_handle_wrong_jsonrpc_version_notification_ignored_silently(db):
    resp = db._handle({"jsonrpc": "1.0", "method": "notifications/initialized"})
    assert resp is None


def test_main_invalid_json_line_returns_parse_error(db, monkeypatch, capsys):
    import io
    monkeypatch.setattr(db.sys, "stdin", io.StringIO("not json at all\n"))
    db.main()
    out = capsys.readouterr().out
    resp = json.loads(out.strip())
    assert resp["error"]["code"] == -32700
    assert resp["id"] is None


# --- keywords rendering -------------------------------------------------------

def test_card_text_renders_keywords_when_present(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute(
        "INSERT INTO cards VALUES ('oid-herald','Widget Herald','widget herald','Creature — Widget',"
        "'It flies, and it does not tire.','[\"Flying\", \"Vigilance\"]')")
    con.execute("INSERT INTO cards_fts VALUES ('Widget Herald','oid-herald')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "widget herald"})
    assert "keywords: Flying, Vigilance" in out


# --- a rule-shaped miss never falls through to FTS ---------------------------

def test_search_rules_rule_shaped_miss_returns_not_found_without_fts_fallback(db):
    con = sqlite3.connect(db.DB_PATH)
    # A decoy FTS-matchable row that would surface if the miss incorrectly
    # fell through to full-text search.
    con.execute("INSERT INTO rules VALUES ('999.998','','See rule 999.999 for details.','')")
    con.execute("INSERT INTO rules_fts VALUES ('999.998', 'See rule 999.999 for details.')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_search_rules({"terms": "999.999"})
    assert "not found" in out.lower()
    assert "999.998" not in out


# --- sidecar robustness -------------------------------------------------------

def test_corpus_info_artifact_sha256_unknown_when_sidecar_corrupt_binary(db):
    sha_path = Path(str(db.DB_PATH) + ".sha256")
    sha_path.write_bytes(b"\xff\xfe\x00\x01not-hex-\x80\x81")
    out = db.tool_lookup_rule({"rule_id": "702.2"})
    assert "artifact_sha256=unknown" in out


def test_corpus_info_artifact_sha256_unknown_when_sidecar_not_hex(db):
    sha_path = Path(str(db.DB_PATH) + ".sha256")
    sha_path.write_text("not-a-real-hash\n")
    out = db.tool_lookup_rule({"rule_id": "702.2"})
    assert "artifact_sha256=unknown" in out


# =============================================================================
# Further adversarial-input regressions
# =============================================================================

# --- a canonical exact match takes precedence over face-name hits -----------

def test_lookup_card_canonical_exact_beats_other_cards_face_name(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute(
        "INSERT INTO cards VALUES ('oid-lash-canon','Voltaic Lash','voltaic lash',"
        "'Instant','Voltaic Lash sparks a chosen widget for 3.','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Voltaic Lash','oid-lash-canon')")
    # A different card whose FACE (not canonical name) is named 'Voltaic Lash'.
    con.execute(
        "INSERT INTO cards VALUES ('oid-other-dfc','Emeritus of Conflict // Voltaic Lash',"
        "'emeritus of conflict // voltaic lash','Creature // Instant','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Emeritus of Conflict','oid-other-dfc')")
    con.execute("INSERT INTO cards_fts VALUES ('Voltaic Lash','oid-other-dfc')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Voltaic Lash"})
    assert "sparks a chosen widget for 3" in out
    assert "candidates" not in out.lower()
    assert "Emeritus" not in out


# --- the unknown-method error truncates the echoed method -------------------

def test_handle_unknown_method_huge_name_produces_bounded_response(db):
    huge = "z" * 70_000
    resp = db._handle({"jsonrpc": "2.0", "id": 1, "method": huge})
    assert len(json.dumps(resp)) < 1000
    assert huge not in resp["error"]["message"]


# --- a malformed method shape is -32600, not a silent notification ----------

def test_handle_missing_method_is_invalid_request_with_null_id(db):
    resp = db._handle({"jsonrpc": "2.0"})
    assert resp is not None
    assert resp["error"]["code"] == -32600
    assert resp["id"] is None


def test_handle_non_string_method_with_id_is_invalid_request_not_unknown_method(db):
    resp = db._handle({"jsonrpc": "2.0", "id": 7, "method": 5})
    assert resp["error"]["code"] == -32600
    assert resp["id"] == 7


# --- non-standard JSON constants (NaN/Infinity) are parse errors ------------

def test_main_nan_constant_returns_parse_error(db, monkeypatch, capsys):
    import io
    monkeypatch.setattr(
        db.sys, "stdin",
        io.StringIO('{"jsonrpc":"2.0","id":1,"method":"tools/list","params":NaN}\n'))
    db.main()
    out = capsys.readouterr().out
    resp = json.loads(out.strip())
    assert resp["error"]["code"] == -32700


# --- trailing-period rule ids strip before the rule-shape regex -------------

def test_search_rules_trailing_period_rule_id_direct_lookup_not_found(db):
    con = sqlite3.connect(db.DB_PATH)
    # Decoy FTS-matchable row that would surface if this incorrectly fell
    # through to full-text search instead of the direct-lookup miss path.
    con.execute("INSERT INTO rules VALUES ('999.998','','See rule 999.999. for details.','')")
    con.execute("INSERT INTO rules_fts VALUES ('999.998', 'See rule 999.999 for details.')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_search_rules({"terms": "999.999."})
    assert "not found" in out.lower()
    assert "999.998" not in out


# --- sidecar decode is strict; ignore-decode must never launder -------------
# corrupt bytes into a spuriously-valid 64-hex-char sha256.

def test_corpus_info_artifact_sha256_unknown_when_valid_hex_padded_with_invalid_utf8(db):
    sha_path = Path(str(db.DB_PATH) + ".sha256")
    valid_hex = b"deadbeefcafef00d1234567890abcdef1234567890abcdef1234567890abcdef"
    assert len(valid_hex) == 64
    # Lone continuation bytes are invalid UTF-8; errors="ignore" would drop
    # them and leave exactly the 64 valid hex chars behind.
    sha_path.write_bytes(valid_hex + b"\x80\x81" + b"  corpus.sqlite\n")
    out = db.tool_lookup_rule({"rule_id": "702.2"})
    assert "artifact_sha256=unknown" in out


# --- where the corpus is read from -------------------------------------------
# Casa gives the plugin a writable directory of its own; the installed plugin
# tree is checksummed and must stay exactly as it was shipped.

def test_data_dir_prefers_the_writable_plugin_data_directory(db, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path / "plugin-data"))
    assert db._resolve_data_dir() == tmp_path / "plugin-data"


def test_data_dir_falls_back_to_the_bundled_directory(db, monkeypatch):
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    assert db._resolve_data_dir() == db.BUNDLED_DATA_DIR


def test_blank_plugin_data_is_treated_as_unset(db, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "   ")
    assert db._resolve_data_dir() == db.BUNDLED_DATA_DIR


# --- what the corpus COVERS, not only what it is -----------------------------

def test_corpus_info_reports_alias_languages(db):
    out = db.tool_lookup_card({"name": "grizzly bears"})
    assert "alias_languages=it:1" in out


def test_corpus_info_says_none_when_the_corpus_has_no_aliases(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute("DELETE FROM card_aliases")
    con.commit(); con.close()
    db._reset_db()
    assert "alias_languages=none" in db.tool_lookup_card({"name": "grizzly bears"})


def test_alias_coverage_is_invalidated_by_reset_db(db):
    """setup_corpus can replace the corpus inside a live process. A cached
    count that outlives the connection would describe the old corpus while
    every other field describes the new one."""
    assert "alias_languages=it:1" in db.tool_lookup_card({"name": "grizzly bears"})
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO card_aliases VALUES ('altro nome','it','oid1')")
    con.commit(); con.close()
    db._reset_db()
    assert "alias_languages=it:2" in db.tool_lookup_card({"name": "grizzly bears"})


def test_corpus_info_says_unknown_when_the_alias_table_cannot_be_read(db):
    """'unknown' is a third state on purpose: it is neither 'no languages'
    nor a number, and reporting an unreadable table as none would be a claim
    about the corpus drawn from a failure to read it.

    Asserted through lookup_rule, not lookup_card: card_aliases is in
    setup_corpus._REQUIRED_SCHEMA, so a corpus missing it cannot install, and
    after the cut lookup_card consults it on every call and raises here — as
    that module's own comment says it should. corpus_info is reached by every
    tool, and the ones that do not touch aliases must still print a line."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("DROP TABLE card_aliases")
    con.commit(); con.close()
    db._reset_db()
    assert "alias_languages=unknown" in db.tool_lookup_rule({"rule_id": "702.2"})


# --- the cut: `lang` no longer selects which table is consulted --------------

def test_lang_changes_nothing(db):
    """`lang` was a guess by the model — the one component this plugin
    exists to distrust — and it selected which table was consulted. It is
    now inert. A future edit that reintroduces a lang-selected branch fails
    here rather than in the field."""
    a = db.tool_lookup_card({"name": "Orso Grizzly"})
    b = db.tool_lookup_card({"name": "Orso Grizzly", "lang": "en"})
    c = db.tool_lookup_card({"name": "Orso Grizzly", "lang": "it"})
    assert a == b == c
    assert "Grizzly Bears" in a


def test_a_name_meaning_different_cards_in_two_languages_is_ambiguous(db):
    """The real corpus collision: Italian 'vendetta' is the printed name of
    the English card Vengeance, and a DIFFERENT English card is named
    Vendetta. Resolving that to either one is a coin flip presented as a
    ruling."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO cards VALUES ('oid-ven','Vendetta','vendetta','Instant','','[]')")
    con.execute("INSERT INTO cards VALUES ('oid-vng','Vengeance','vengeance','Sorcery','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Vendetta','oid-ven')")
    con.execute("INSERT INTO cards_fts VALUES ('Vengeance','oid-vng')")
    con.execute("INSERT INTO card_aliases VALUES ('vendetta','it','oid-vng')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Vendetta"})    # no lang at all
    assert "ambiguous" in out.lower()
    assert "Vendetta" in out and "Vengeance" in out
    assert "it printed name" in out


def test_canonical_precedence_survives_the_cut(db):
    """An exact canonical match still suppresses other cards' face names.
    Both reviewers reached for this case: in the real corpus 'Lightning
    Bolt' is also a FACE of 'Emeritus of Conflict // Lightning Bolt', so
    consulting faces unconditionally makes 38 ordinary lookups ambiguous."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("INSERT INTO cards VALUES ('oid-lb','Lightning Bolt','lightning bolt','Instant','','[]')")
    con.execute("INSERT INTO cards VALUES ('oid-dfc','Emeritus of Conflict // Lightning Bolt','emeritus of conflict // lightning bolt','Creature','','[]')")
    con.execute("INSERT INTO cards_fts VALUES ('Lightning Bolt','oid-lb')")
    con.execute("INSERT INTO cards_fts VALUES ('Lightning Bolt','oid-dfc')")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Lightning Bolt"})
    assert "ambiguous" not in out.lower()
    assert "oid-lb" in out and "oid-dfc" not in out


def test_a_resolved_card_names_the_source_that_resolved_it(db):
    out = db.tool_lookup_card({"name": "Orso Grizzly"})
    assert 'it printed name "orso grizzly"' in out


def test_a_card_resolved_by_its_english_name_says_so_too(db):
    """The other half of attribution: an English hit is labelled as one, so
    'matched by' is never a line that only appears for localized names."""
    assert "matched by: en" in db.tool_lookup_card({"name": "grizzly bears"})


def test_candidate_list_is_bounded_and_reports_the_total(db):
    """`elemental` has 35 identities in the real corpus. A silently
    truncated list claims a completeness it does not have."""
    con = sqlite3.connect(db.DB_PATH)
    for i in range(8):
        con.execute("INSERT INTO cards VALUES (?,?,?,'Creature','','[]')",
                    (f"oid-e{i}", "Elemental", "elemental"))
        con.execute("INSERT INTO cards_fts VALUES ('Elemental',?)", (f"oid-e{i}",))
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Elemental"})
    assert "showing 5 of 8" in out


# --- a miss says what the corpus covers, not only that it missed -------------

def test_a_miss_states_the_corpus_language_coverage(db):
    out = db.tool_lookup_card({"name": "zzqqxx"})
    assert "not found" in out.lower()
    assert "alias_languages=it:1" in out
    assert "carries" in out.lower()


def test_a_miss_on_an_english_only_corpus_says_english_only(db):
    con = sqlite3.connect(db.DB_PATH)
    con.execute("DELETE FROM card_aliases")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Fulmine"})
    assert "English card names only" in out


def test_fuzzy_candidates_are_labelled_by_language(db):
    """A near-miss on an Italian name must come back as an Italian name, not
    as a plausible-looking English suggestion — that substitution is what
    produced the original defect."""
    out = db.tool_lookup_card({"name": "Orso Grizzli"})   # typo on the alias
    assert "orso grizzly" in out.lower()
    assert "it printed name" in out


def test_english_typo_recovery_survives_on_an_english_only_corpus(db):
    """An earlier draft skipped fuzzy when coverage was zero. The server
    cannot know an input was localized, and skipping fuzzy would break
    ordinary typo recovery."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("DELETE FROM card_aliases")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "grizly bears"})
    assert "Grizzly Bears" in out


# --- a kept mutation test, not a mutation a reviewer promises to re-run ------

def test_excising_the_coverage_sentence_changes_the_answer(db):
    """A kept mutation test, because six times in this repo a check has
    reported success while exercising nothing, and a reviewer re-running a
    mutation by hand is what kept failing.

    It asserts BOTH directions. Asserting only that the mutated module says
    a bare "not found" would pass just as well if the coverage sentence had
    never been written -- that IS the pre-change behaviour. Terra caught
    exactly this in design round 1.
    """
    import re
    source = Path(db.__file__).read_text(encoding="utf-8")
    mutated, n = re.subn(
        r"# --- coverage-sentence: begin ---.*?# --- coverage-sentence: end ---",
        "def _coverage_sentence() -> str:\n    return ''",
        source, flags=re.DOTALL)
    assert n == 1, "source markers moved; this test is no longer excising anything"

    # Direction 1: unmutated, the sentence is there.
    intact = db.tool_lookup_card({"name": "zzqqxx"})
    assert "This corpus carries" in intact

    # Direction 2: excised, it is gone -- and nothing else broke.
    # __file__ because the module resolves its bundled data dir at import;
    # __name__ so the mutated copy does not decide it is __main__ and serve.
    ns: dict = {"__file__": db.__file__, "__name__": "mtg_server_mutated"}
    exec(compile(mutated, "mtg_server_mutated", "exec"), ns)
    ns["DB_PATH"] = db.DB_PATH
    ns["_reset_db"]()
    damaged = ns["tool_lookup_card"]({"name": "zzqqxx"})
    assert "This corpus carries" not in damaged
    assert "not found" in damaged.lower()


# --- cost is three-valued ----------------------------------------------------

def test_mana_cost_distinguishes_none_from_not_carried(db):
    """Three states, three sentences. 'The card has no cost' and 'this
    corpus does not carry cost' are different claims; collapsing them
    recreates the original defect in a new field."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("ALTER TABLE cards ADD COLUMN mana_cost TEXT NOT NULL DEFAULT ''")
    con.execute("UPDATE cards SET mana_cost='{1}{G}' WHERE oracle_id='oid1'")
    con.execute("INSERT INTO cards VALUES ('oid-land','Island','island','Land','','[]','')")
    con.execute("INSERT INTO cards_fts VALUES ('Island','oid-land')")
    con.commit(); con.close()
    db._reset_db()
    assert "mana_cost: {1}{G}" in db.tool_lookup_card({"name": "grizzly bears"})
    assert "mana_cost: none" in db.tool_lookup_card({"name": "Island"})


def test_a_corpus_without_the_column_says_so(db):
    """The fixture corpus has no mana_cost column -- exactly like every
    corpus published before this change."""
    out = db.tool_lookup_card({"name": "grizzly bears"})
    assert "mana_cost: not carried by this corpus" in out


# --- an alias that names no language cannot attribute anything ---------------

def test_an_alias_with_no_language_does_not_resolve(db):
    """Round 24, found by both reviewers independently. A card_aliases row
    with a NULL or blank lang used to resolve a card and label it
    'matched by: None printed name "..."' -- an attribution naming no
    language, pointing at a row alias_languages had already excluded from
    the coverage count. The result block looked exactly as grounded as a
    real one, which is the whole failure this change exists to remove.

    Ignoring the row is what makes the two lines agree: the corpus is then
    reported as carrying no such language, and the name does not resolve."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("UPDATE card_aliases SET lang=NULL WHERE printed_lower='orso grizzly'")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Orso Grizzly"})
    assert "None printed name" not in out
    assert "Grizzly Bears —" not in out       # no card block: it must not resolve
    assert "not found" in out.lower()
    assert "alias_languages=none" in out


def test_a_blank_language_alias_does_not_resolve_either(db):
    """NULL is not the only way to have no language; '' and '   ' reach the
    same place, and _alias_coverage already excludes all three."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("UPDATE card_aliases SET lang='   ' WHERE printed_lower='orso grizzly'")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Orso Grizzly"})
    assert "matched by" not in out          # nothing resolved it
    assert "Grizzly Bears —" not in out     # and no card block came back
    assert "not found" in out.lower()
    assert "alias_languages=none" in out


def test_an_unlabelled_alias_is_not_offered_as_a_fuzzy_candidate(db):
    """The same row reaches the reader by a second route. A suggestion
    labelled '(None printed name)' is an unattributable name presented as a
    candidate, and the fuzzy tail builds its pool from the same table."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("UPDATE card_aliases SET lang=NULL WHERE printed_lower='orso grizzly'")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "Orso Grizzli"})    # typo on the alias
    assert "None printed name" not in out
    assert "orso grizzly" not in out.lower()


def test_a_null_cost_is_not_reported_as_none(db):
    """Round 27, Sol. A NULL cost supports neither 'this card has no cost'
    nor a cost, but truthiness collapsed it into 'none' -- an unsupported
    claim in the field this change exists to make honest.

    The builder writes the column NOT NULL, so this needs a hand-built
    corpus to reach; that is exactly the corpus a self-hosting operator
    installs, and the server must not state a fact it cannot support
    whatever it is pointed at."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("ALTER TABLE cards ADD COLUMN mana_cost TEXT")   # nullable
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "grizzly bears"})
    assert "mana_cost: none" not in out
    assert "mana_cost: not carried by this corpus" in out


def test_a_non_string_cost_is_not_reported_as_a_cost(db):
    """The same seam by a different route: an integer or a blob is not a
    mana cost, and printing it back would launder it into one."""
    con = sqlite3.connect(db.DB_PATH)
    con.execute("ALTER TABLE cards ADD COLUMN mana_cost")         # no type
    con.execute("UPDATE cards SET mana_cost=3 WHERE oracle_id='oid1'")
    con.commit(); con.close()
    db._reset_db()
    out = db.tool_lookup_card({"name": "grizzly bears"})
    assert "mana_cost: 3" not in out
    assert "mana_cost: not carried by this corpus" in out
