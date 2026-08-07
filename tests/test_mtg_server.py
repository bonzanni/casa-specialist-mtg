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
