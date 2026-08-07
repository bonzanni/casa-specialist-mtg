#!/usr/bin/env python3
"""casa-mtg MCP server. Stdlib-only stdio JSON-RPC.

The query path is read-only by construction: sqlite opened mode=ro; constant
DB path; no shell; no network; every tool returns bounded text ending in a
corpus_info line.

The one exception is `setup_corpus`, which provisions the corpus after
install. Its implementation lives in a separate module that is imported only
when that tool is called, so answering a rules question can never reach code
that opens a socket or writes a file.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

BUNDLED_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _resolve_data_dir() -> Path:
    """Where the corpus lives.

    Casa hands the plugin a writable directory of its own, outside the
    installed plugin tree — and outside is the point: that tree is
    content-checksummed, so a corpus written into it would make the next
    registry reload read the plugin as corrupt. A standalone Claude Code
    install has no such directory, and there the plugin's own data/ is both
    the natural place and the one the builder writes to by default.
    """
    external = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    return Path(external) if external else BUNDLED_DATA_DIR


DB_PATH = _resolve_data_dir() / "corpus.sqlite"
_DB: sqlite3.Connection | None = None
MAX_LIMIT = 8
ECHO_LIMIT = 120  # user-echoed input is bounded in every response
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def _reset_db() -> None:
    global _DB
    _DB = None


def _db() -> sqlite3.Connection:
    global _DB
    if _DB is None:
        _DB = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        _DB.row_factory = sqlite3.Row
    return _DB


def _truncate_echo(value) -> str:
    """Bound any user-supplied text before it is echoed back."""
    s = str(value)
    return s if len(s) <= ECHO_LIMIT else s[:ECHO_LIMIT] + "…"


def _parse_limit(args: dict, default: int = 5) -> int:
    """Safe parse + clamp to [1, MAX_LIMIT]. Non-int/absent -> default."""
    raw = args.get("limit", default)
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, MAX_LIMIT))


def _format_candidates(pairs: list[tuple[str, str]], limit: int = 5) -> str:
    """Bounded 'Name (oracle_id)' candidate list, sorted for determinism."""
    bounded = sorted(pairs, key=lambda p: p[0])[:limit]
    return ", ".join(f"{name} ({oid})" for name, oid in bounded)


def _corpus_info() -> str:
    rows = _db().execute("SELECT key, value FROM meta").fetchall()
    kv = ", ".join(f"{r['key']}={r['value']}" for r in rows)
    # The sidecar hash must never crash a response -- read bytes with
    # STRICT UTF-8 decoding (never errors="ignore", which can launder
    # corrupt bytes into a spuriously-valid 64-hex-char string) and only
    # accept a strict 64-hex-char sha256; anything else (missing, corrupt,
    # non-hex, non-UTF-8) degrades to "unknown".
    digest = "unknown"
    sha_path = Path(str(DB_PATH) + ".sha256")
    try:
        raw = sha_path.read_bytes()
    except OSError:
        raw = b""
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        text = ""
    candidate = text.split()[0] if text else ""
    if _SHA256_RE.match(candidate):
        digest = candidate[:16].lower()
    return f"corpus_info: {kv}, artifact_sha256={digest}"


def _corpus_info_safe() -> str:
    """Error paths must still end with a corpus_info line, even if
    reading corpus_info itself fails (e.g. a broken/missing DB)."""
    try:
        return _corpus_info()
    except Exception:  # noqa: BLE001 -- never let this crash an error response
        return "corpus_info: unavailable"


def _rule_block(row) -> str:
    ex = f"\n  Example: {row['examples']}" if row["examples"] else ""
    return f"{row['rule_id']} {row['text']}{ex}"


def tool_lookup_rule(args: dict) -> str:
    rid = str(args.get("rule_id", "")).strip().rstrip(".")
    row = _db().execute("SELECT * FROM rules WHERE rule_id=?", (rid,)).fetchone()
    if row is None:
        return f"rule {_truncate_echo(rid)!r} not found\n{_corpus_info()}"
    parts = [_rule_block(row)]
    subs = _db().execute(
        "SELECT * FROM rules WHERE parent_id=? ORDER BY rule_id", (rid,),
    ).fetchall()
    parts += [_rule_block(s) for s in subs]
    # one hop of cross-references ("See rule NNN[.N[x]]")
    refs = set(re.findall(r"rule (\d{3}(?:\.\d+[a-z]?)?)",
                          " ".join(p for p in parts)))
    refs.discard(rid)
    for ref in sorted(refs)[:4]:
        r = _db().execute(
            "SELECT * FROM rules WHERE rule_id=? OR rule_id LIKE ? "
            "ORDER BY rule_id LIMIT 1", (ref, ref + ".%")).fetchone()
        if r:
            parts.append("(ref) " + _rule_block(r))
    return "\n".join(parts) + "\n" + _corpus_info()


_RULE_ID_RE = None


def _rule_id_pattern():
    global _RULE_ID_RE
    if _RULE_ID_RE is None:
        _RULE_ID_RE = re.compile(r"^\d{3}(\.\d+[a-z]?)?$")
    return _RULE_ID_RE


def _quote_fts_terms(terms: str) -> str:
    """Quote each whitespace-separated term for safe FTS5 MATCH.

    Embedded double quotes are stripped before wrapping so a term can
    never break out of its quoted literal and inject FTS5 query syntax
    (column filters, NEAR, boolean operators, dangling quotes, etc.).
    """
    parts = []
    for tok in terms.split():
        cleaned = tok.replace('"', "")
        if not cleaned:
            continue
        parts.append(f'"{cleaned}"')
    return " ".join(parts)


def tool_search_rules(args: dict) -> str:
    terms = str(args.get("terms", "")).strip()
    limit = _parse_limit(args)
    if not terms:
        return "empty search\n" + _corpus_info()

    # Rule-number-aware: a bare rule id goes straight to the rules table
    # (and its subrules). If it's rule-number-shaped but absent, that is an
    # authoritative miss: never fall through to FTS. Strip a
    # trailing period BEFORE the rule-shape regex test, so "999.999." (a
    # common trailing-punctuation form) is still recognized as rule-shaped
    # instead of leaking through to FTS.
    rid = terms.rstrip(".")
    if _rule_id_pattern().match(rid):
        row = _db().execute("SELECT * FROM rules WHERE rule_id=?", (rid,)).fetchone()
        if row is not None:
            out = [_rule_block(row)]
            subs = _db().execute(
                "SELECT * FROM rules WHERE parent_id=? ORDER BY rule_id",
                (rid,)).fetchall()
            out += [_rule_block(s) for s in subs[: max(limit - 1, 0)]]
            return "\n".join(out) + "\n" + _corpus_info()
        return f"rule {_truncate_echo(terms)!r} not found\n{_corpus_info()}"

    fts_query = _quote_fts_terms(terms)
    if not fts_query:
        return f"no rules match {_truncate_echo(terms)!r}\n{_corpus_info()}"
    try:
        rows = _db().execute(
            "SELECT rule_id FROM rules_fts WHERE rules_fts MATCH ? "
            "ORDER BY rank LIMIT ?", (fts_query, limit)).fetchall()
    except sqlite3.OperationalError:
        return f"no rules match {_truncate_echo(terms)!r}\n{_corpus_info()}"
    if not rows:
        return f"no rules match {_truncate_echo(terms)!r}\n{_corpus_info()}"
    out = []
    for r in rows:
        row = _db().execute("SELECT * FROM rules WHERE rule_id=?",
                            (r["rule_id"],)).fetchone()
        if row:
            out.append(_rule_block(row))
    return "\n".join(out) + "\n" + _corpus_info()


def tool_lookup_term(args: dict) -> str:
    term = str(args.get("term", "")).strip()
    row = _db().execute(
        "SELECT * FROM glossary WHERE term=? COLLATE NOCASE", (term,),
    ).fetchone()
    if row is None:
        rows = _db().execute(
            "SELECT term FROM glossary WHERE term LIKE ? LIMIT 5",
            (f"%{term}%",)).fetchall()
        cand = ", ".join(r["term"] for r in rows) or "none"
        return (f"term {_truncate_echo(term)!r} not found; candidates: "
                f"{cand}\n{_corpus_info()}")
    return f"{row['term']}: {row['definition']}\n{_corpus_info()}"


def _card_text(row) -> str:
    lines = [f"{row['name']} — {row['type_line']}", row["oracle_text"]]
    try:
        raw_kw = row["keywords"]
    except (IndexError, KeyError):
        raw_kw = None
    keywords = []
    if raw_kw:
        try:
            keywords = json.loads(raw_kw)
        except (TypeError, ValueError):
            keywords = []
    if keywords:
        lines.append("keywords: " + ", ".join(keywords))
    lines.append(f"oracle_id: {row['oracle_id']}")
    return "\n".join(lines)


def tool_lookup_card(args: dict) -> str:
    raw_name = args.get("name", "")
    name = str(raw_name).strip().lower()
    lang = str(args.get("lang", "en")).strip().lower()
    display_name = _truncate_echo(raw_name)

    # An EXACT canonical name_lower match takes precedence over
    # face-name-only matches for OTHER cards. If exactly one
    # canonical exact match exists, the face-name (cards_fts) query is
    # skipped entirely so a face-name hit on a different card can never turn
    # a clean canonical resolution into a false ambiguity. Alias matches are
    # NOT face-name matches and are always still consulted below -- an
    # it-alias-vs-English disagreement must still surface as candidates.
    canonical_ids: list[str] = []
    for r in _db().execute(
        "SELECT oracle_id FROM cards WHERE name_lower=?", (name,)
    ).fetchall():
        if r["oracle_id"] not in canonical_ids:
            canonical_ids.append(r["oracle_id"])

    # Collect ALL matches from every source, then dedupe by oracle_id.
    # Exactly one distinct oracle_id resolves; more than one is a genuine
    # ambiguity (bounded candidates), never an arbitrary fetchone() pick.
    matched: dict[str, None] = {}
    for oid in canonical_ids:
        matched.setdefault(oid, None)

    if len(canonical_ids) != 1:
        for r in _db().execute(
            "SELECT oracle_id FROM cards_fts WHERE name = ? COLLATE NOCASE",
            (raw_name,),
        ).fetchall():
            matched.setdefault(r["oracle_id"], None)

    if lang != "en":
        for r in _db().execute(
            "SELECT oracle_id FROM card_aliases WHERE printed_lower=? AND lang=?",
            (name, lang),
        ).fetchall():
            matched.setdefault(r["oracle_id"], None)

    oracle_ids = list(matched.keys())
    if len(oracle_ids) == 1:
        row = _db().execute("SELECT * FROM cards WHERE oracle_id=?",
                            (oracle_ids[0],)).fetchone()
        if row is not None:
            return _card_text(row) + "\n" + _corpus_info()
    elif len(oracle_ids) > 1:
        pairs = []
        for oid in oracle_ids:
            r = _db().execute("SELECT name FROM cards WHERE oracle_id=?",
                              (oid,)).fetchone()
            if r is not None:
                pairs.append((r["name"], oid))
        if pairs:
            return (f"card {display_name!r} is ambiguous; candidates: "
                    f"{_format_candidates(pairs)}\n{_corpus_info()}")

    # Fuzzy candidates for typos / garbled STT (difflib over the name list;
    # FTS MATCH alone won't catch "grizly bears"). Bounded and scored.
    import difflib
    names = [r["name"] for r in _db().execute("SELECT name FROM cards").fetchall()]
    cands = difflib.get_close_matches(raw_name, names, n=5, cutoff=0.6)
    if cands:
        return (f"card {display_name!r} not found exactly; candidates: "
                f"{', '.join(cands[:5])}\n{_corpus_info()}")
    return f"card {display_name!r} not found\n{_corpus_info()}"


_SOURCE_LABELS = {
    "scryfall": "scryfall_note",
    "wotc": "wotc",
}


def tool_get_rulings(args: dict) -> str:
    oid = str(args.get("oracle_id", "")).strip()
    rows = _db().execute(
        "SELECT * FROM rulings WHERE oracle_id=? ORDER BY published_at",
        (oid,)).fetchall()
    if not rows:
        return f"no rulings for {_truncate_echo(oid)}\n{_corpus_info()}"
    out = []
    for r in rows:
        # Raw Scryfall `source` values get relabeled for the reader;
        # 'scryfall' is unofficial commentary, 'wotc' is official, anything
        # else (future source values) passes through unchanged.
        label = _SOURCE_LABELS.get(r["source"], r["source"])
        out.append(f"[{label}] {r['published_at']}: {r['comment']}")
    return "\n".join(out[:12]) + "\n" + _corpus_info()


def _setup_corpus_module():
    """Import the corpus installer, script-launched or package-imported.

    The plugin runs this file as a script (no package context) but the tests
    import it as `plugins.mtg.server.mtg_server`; both have to resolve the
    sibling module, and neither may resolve it at import time.
    """
    try:
        from . import setup_corpus
    except ImportError:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import setup_corpus
    return setup_corpus


def tool_setup_corpus(args: dict) -> str:
    setup = _setup_corpus_module()
    # The installer is told where to write rather than resolving it again:
    # the corpus must land exactly where this server reads from, and one
    # resolution cannot disagree with itself.
    summary = setup.run(args, data_dir=DB_PATH.parent)
    # The file the open connection refers to has just been replaced; drop it
    # so the next question is answered from the corpus that is now on disk.
    _reset_db()
    return summary + "\n" + _corpus_info_safe()


TOOLS = {
    "lookup_rule": (tool_lookup_rule, {"rule_id": "CR rule number, e.g. 702.19b"}),
    "search_rules": (tool_search_rules, {
        "terms": "FTS terms",
        "limit": {"type": "integer", "description": "max results",
                  "minimum": 1, "maximum": MAX_LIMIT},
    }),
    "lookup_term": (tool_lookup_term, {"term": "glossary term, e.g. deathtouch"}),
    "lookup_card": (tool_lookup_card, {"name": "card name", "lang": "en|it (default en)"}),
    "get_rulings": (tool_get_rulings, {"oracle_id": "from lookup_card"}),
    "setup_corpus": (tool_setup_corpus, {
        "url": "https:// URL of the corpus tarball (default: configured)",
        "sha256": "expected sha256 of that tarball (default: configured)",
        "token": "bearer token, if the URL needs one; prefer the "
                 "configured value — an argument goes in the transcript",
        "force": {"type": "boolean",
                  "description": "replace an existing corpus"},
    }),
}

# Casa runs a setup tool with no arguments, so nothing in its schema may be
# required — the force flag is for an operator asking for it by hand.
ARGUMENT_FREE_TOOLS = frozenset(("setup_corpus",))

_TOOL_DESCRIPTIONS = {
    "setup_corpus": ("MTG corpus (setup): download and install the corpus "
                     "from the location configured by the operator"),
}


def _tool_defs() -> list[dict]:
    return [{
        "name": name,
        "description": _TOOL_DESCRIPTIONS.get(
            name, f"MTG corpus (read-only): {name}"),
        "inputSchema": {
            "type": "object",
            "properties": {
                k: (v if isinstance(v, dict) else {"type": "string", "description": v})
                for k, v in params.items()
            },
            "required": ([] if name in ARGUMENT_FREE_TOOLS or not params
                         else [next(iter(params))]),
        },
    } for name, (fn, params) in TOOLS.items()]


def _tool_call_result(params: dict) -> tuple[str, bool]:
    """Unknown tool, non-dict/None arguments, and tool exceptions ALL
    surface as (text, isError=True) — never a top-level JSON-RPC error."""
    name = str(params.get("name", ""))
    fn_entry = TOOLS.get(name)
    if fn_entry is None:
        return f"unknown tool {_truncate_echo(name)!r}\n{_corpus_info_safe()}", True

    has_args_key = "arguments" in params
    arguments = params.get("arguments", {})
    if has_args_key and not isinstance(arguments, dict):
        return (f"invalid arguments for {_truncate_echo(name)!r}\n"
                f"{_corpus_info_safe()}", True)

    try:
        text = fn_entry[0](arguments if isinstance(arguments, dict) else {})
        return text, False
    except Exception as exc:  # noqa: BLE001 -- real failure => isError, never crash
        return f"tool error: {exc}\n{_corpus_info_safe()}", True


def _handle(req) -> dict | None:
    # Valid JSON that isn't an object (null, [], "x", 5, ...) is an
    # invalid request; id is unknowable, so it is reported as null.
    if not isinstance(req, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "invalid request"}}

    has_id = "id" in req
    rid = req.get("id")

    # Validate the request shape BEFORE the notification check. A
    # request with a missing or non-string "method" is malformed — not a
    # notification, even if it also happens to lack an "id" — so it always
    # gets a response (id=null when the id is unknowable/absent).
    method = req.get("method")
    if not isinstance(method, str):
        return {"jsonrpc": "2.0", "id": rid if has_id else None,
                "error": {"code": -32600, "message": "invalid request"}}

    # A JSON-RPC notification is a well-formed request object without an
    # "id" member — it must never receive a response, no matter the method.
    version = req.get("jsonrpc")
    if version != "2.0":
        if not has_id:
            return None  # notification with a bad version: ignore silently
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32600, "message": "invalid jsonrpc version"}}

    params = req.get("params")
    if not isinstance(params, dict):  # params:null (or any non-object) -> {}
        params = {}

    if method == "initialize":
        if not has_id:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "mtg", "version": "1.0.0"}}}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        if not has_id:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": _tool_defs()}}
    if method == "tools/call":
        text, is_error = _tool_call_result(params)
        if not has_id:
            return None
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "content": [{"type": "text", "text": text}], "isError": is_error}}
    if has_id:
        return {"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32601,
            "message": f"unknown method {_truncate_echo(method)}"}}
    return None


def _reject_json_constant(name: str):
    """json.loads accepts the non-standard JSON extension constants
    NaN/Infinity/-Infinity by default; reject them as a parse error instead
    of silently admitting non-standard JSON."""
    raise ValueError(f"non-standard JSON constant: {name}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": str(exc)}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue
        try:
            resp = _handle(parsed)
        except Exception as exc:  # noqa: BLE001 -- never crash the stdio loop
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32603, "message": str(exc)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
