#!/usr/bin/env python3
"""Build plugins/mtg/data/corpus.sqlite from the CR + Scryfall bulk data.

Usage:
  python3 scripts/build_corpus.py --cr-url <MagicCompRules .txt URL> \
      [--out plugins/mtg/data/corpus.sqlite] [--skip-download]

Find the current --cr-url at https://magic.wizards.com/en/rules (the .txt
link; its filename carries the effective date and changes every release).

Stdlib only, builder included: Scryfall's move to JSONL means a line is a
document, so the multi-gigabyte all_cards file streams without ijson.
Downloads land in plugins/mtg/data/ and are
gitignored, as is the built corpus: neither the raw rules text nor the
compiled corpus is redistributed from this repository, and no build of it is
published anywhere from here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "plugins" / "mtg" / "data"
UA = {"User-Agent": "casa-mtg-corpus-builder/1.0", "Accept": "*/*"}

_RULE_RE = re.compile(r"^(\d{3}\.\d+[a-z]?)\.?\s+(.*)$")
_EXAMPLE_RE = re.compile(r"^Example:\s*(.*)$")


def _fetch(url: str, dest: Path) -> Path:
    """Stream a URL to disk, decompressing gzip on the way if it is gzipped.

    Scryfall serves bulk data as `.jsonl.gz`. Streaming through GzipFile keeps
    the memory profile the same as before while writing plain JSONL, so
    everything downstream reads one object per line and nothing has to know
    the transport was compressed.
    """
    import gzip
    import shutil

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=300) as resp:
        source = gzip.GzipFile(fileobj=resp) if url.endswith(".gz") else resp
        with open(dest, "wb") as fh:
            shutil.copyfileobj(source, fh, length=1 << 20)
    time.sleep(0.2)  # Scryfall API courtesy
    return dest


def parse_cr(text: str) -> list[dict]:
    """Parse numbered rules + subrules; attach Example lines to the rule above."""
    rules: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _RULE_RE.match(line)
        if m:
            rid, body = m.group(1), m.group(2)
            parent = rid[:-1] if rid[-1].isalpha() else None
            rules.append({"rule_id": rid, "parent_id": parent,
                          "text": body, "examples": ""})
            continue
        ex = _EXAMPLE_RE.match(line)
        if ex and rules:
            prev = rules[-1]
            prev["examples"] = (prev["examples"] + "\n" + ex.group(1)).strip()
        elif line and rules and not line[0].isdigit():
            # continuation line of the previous rule paragraph
            rules[-1]["text"] += " " + line
    return rules


def parse_glossary(text: str) -> list[tuple[str, str]]:
    """Glossary section: term line followed by definition paragraph(s)."""
    entries, term, buf = [], None, []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            if term and buf:
                entries.append((term, " ".join(buf)))
            term, buf = None, []
        elif term is None:
            term = line.strip()
        else:
            buf.append(line.strip())
    if term and buf:
        entries.append((term, " ".join(buf)))
    return entries


def split_cr_sections(full: str) -> tuple[str, str, str]:
    """Return (rules_text, glossary_text, effective_date).

    The CR txt has 'Glossary' and 'Credits' section markers; effective date
    appears near the top as 'These rules are effective as of <date>.'"""
    m_date = re.search(r"effective as of ([A-Za-z]+ \d{1,2}, \d{4})", full)
    date = m_date.group(1) if m_date else "unknown"
    # Split on the LAST 'Glossary' occurrence line (ToC lists it once early).
    idx = full.rfind("\nGlossary\n")
    rules_text = full[:idx] if idx != -1 else full
    tail = full[idx:] if idx != -1 else ""
    cidx = tail.rfind("\nCredits\n")
    glossary_text = tail[:cidx] if cidx != -1 else tail
    return rules_text, glossary_text, date


def _fts_name_rows(c: dict) -> list[tuple[str, str]]:
    """Distinct (name, oracle_id) pairs to index in cards_fts for one oracle
    card: the canonical display name plus each card_faces[] name, deduped
    per-card via exact string match. Without the dedupe, DFC/split cards
    whose faces repeat the canonical name (e.g. "Lightning Bolt // Lightning
    Bolt") produce duplicate (name, oracle_id) rows."""
    oid = c.get("oracle_id")
    seen_names: set[str] = set()
    rows: list[tuple[str, str]] = []
    name = c.get("name")
    if name and name not in seen_names:
        seen_names.add(name)
        rows.append((name, oid))
    for f in (c.get("card_faces") or []):
        fname = f.get("name")
        if fname and fname not in seen_names:
            seen_names.add(fname)
            rows.append((fname, oid))
    return rows


def _it_alias_rows(card: dict, seen: set[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(printed_lower, lang, oracle_id) alias rows for one all_cards object
    with lang=="it": the top-level printed_name AND every card_faces[]
    printed_name — Scryfall stores localized DFC/split names under
    card_faces[].printed_name, not just the top-level printed_name.
    Dedups through the caller-owned `seen` set of (printed_lower, oracle_id)."""
    oid = card.get("oracle_id")
    if card.get("lang") != "it" or not oid:
        return []
    names = []
    if card.get("printed_name"):
        names.append(card["printed_name"])
    for f in (card.get("card_faces") or []):
        if f.get("printed_name"):
            names.append(f["printed_name"])
    rows: list[tuple[str, str, str]] = []
    for name in names:
        key = (name.lower(), oid)
        if key in seen:
            continue
        seen.add(key)
        rows.append((name.lower(), "it", oid))
    return rows


def _scryfall_updated_at(bulk_meta_path: Path | None, kind: str = "oracle_cards") -> str:
    """The real Scryfall bulk-data timestamp for oracle_cards when
    plugins/mtg/data/bulk_meta.json (written by main()) is available; falls
    back to the build date, with a printed warning, when the file is absent
    (e.g. a --skip-download run over downloads from an older build)."""
    if bulk_meta_path is not None and bulk_meta_path.exists():
        try:
            meta = json.loads(bulk_meta_path.read_text(encoding="utf-8"))
            for entry in meta.get("data", []):
                if entry.get("type") == kind and entry.get("updated_at"):
                    return entry["updated_at"]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"warning: could not read {bulk_meta_path}: {exc}; "
                  f"scryfall_updated_at falls back to build date", file=sys.stderr)
            return time.strftime("%Y-%m-%d")
    print(f"warning: {bulk_meta_path} not found; "
          f"scryfall_updated_at falls back to build date", file=sys.stderr)
    return time.strftime("%Y-%m-%d")


def build(cr_path: Path, oracle_path: Path, rulings_path: Path,
          all_cards_path: Path | None, out: Path,
          bulk_meta_path: Path | None = None) -> None:
    if bulk_meta_path is None:
        bulk_meta_path = DATA / "bulk_meta.json"
    full = cr_path.read_text(encoding="utf-8", errors="replace")
    rules_text, glossary_text, cr_date = split_cr_sections(full)
    rules = parse_cr(rules_text)
    glossary = parse_glossary(glossary_text)
    oracle = list(_iter_jsonl(oracle_path))
    rulings = list(_iter_jsonl(rulings_path))

    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out)
    db.executescript("""
      CREATE TABLE rules(rule_id TEXT PRIMARY KEY, parent_id TEXT,
                         text TEXT, examples TEXT);
      CREATE VIRTUAL TABLE rules_fts USING fts5(rule_id, text);
      CREATE TABLE glossary(term TEXT PRIMARY KEY, definition TEXT);
      CREATE TABLE cards(oracle_id TEXT PRIMARY KEY, name TEXT,
                         name_lower TEXT, type_line TEXT, oracle_text TEXT,
                         keywords TEXT);
      CREATE VIRTUAL TABLE cards_fts USING fts5(name, oracle_id UNINDEXED);
      CREATE TABLE card_aliases(printed_lower TEXT, lang TEXT, oracle_id TEXT);
      CREATE INDEX alias_idx ON card_aliases(printed_lower);
      CREATE TABLE rulings(oracle_id TEXT, published_at TEXT,
                           source TEXT, comment TEXT);
      CREATE INDEX rulings_idx ON rulings(oracle_id);
      CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
    """)
    db.executemany(
        "INSERT OR REPLACE INTO rules VALUES (?,?,?,?)",
        [(r["rule_id"], r["parent_id"], r["text"], r["examples"])
         for r in rules])
    db.executemany(
        "INSERT INTO rules_fts VALUES (?,?)",
        [(r["rule_id"], r["text"] + " " + r["examples"]) for r in rules])
    db.executemany("INSERT OR REPLACE INTO glossary VALUES (?,?)", glossary)

    def _oracle_text(c: dict) -> str:
        # DFC/split/adventure cards carry their text under card_faces
        # rather than a top-level oracle_text.
        if c.get("oracle_text"):
            return c["oracle_text"]
        faces = c.get("card_faces") or []
        return "\n//\n".join(
            f"{f.get('name','')}: {f.get('oracle_text','')}".strip()
            for f in faces if f.get("oracle_text"))

    card_rows = 0
    for c in oracle:
        oid = c.get("oracle_id")
        if not oid:
            continue
        if c.get("layout") == "art_series":
            # Non-playable ghost objects: empty oracle text, names that
            # collide with real cards (e.g. "Delver of Secrets // Delver
            # of Secrets"). Exclude from cards/cards_fts entirely.
            continue
        db.execute("INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?)", (
            oid, c.get("name"), (c.get("name") or "").lower(),
            c.get("type_line", ""), _oracle_text(c),
            json.dumps(c.get("keywords", []))))
        for name, name_oid in _fts_name_rows(c):
            db.execute("INSERT INTO cards_fts VALUES (?,?)", (name, name_oid))
        card_rows += 1
    for r in rulings:
        db.execute("INSERT INTO rulings VALUES (?,?,?,?)", (
            r.get("oracle_id"), r.get("published_at"),
            r.get("source", ""), r.get("comment", "")))
    if all_cards_path is not None and all_cards_path.exists():
        seen: set[tuple[str, str]] = set()
        for c in _iter_jsonl(all_cards_path):   # streamed, not loaded whole
            if c.get("layout") == "art_series":
                continue
            for row in _it_alias_rows(c, seen):
                db.execute("INSERT INTO card_aliases VALUES (?,?,?)", row)
    db.execute("INSERT INTO meta VALUES ('cr_effective_date', ?)", (cr_date,))
    db.execute("INSERT INTO meta VALUES ('scryfall_updated_at', ?)",
               (_scryfall_updated_at(bulk_meta_path),))
    # Rulings update independently of Oracle text. Recording the snapshot the
    # corpus was actually built from is what lets a release be named for its
    # real inputs rather than for a subset of them.
    db.execute("INSERT INTO meta VALUES ('scryfall_rulings_updated_at', ?)",
               (_scryfall_updated_at(bulk_meta_path, "rulings"),))
    if all_cards_path is not None and all_cards_path.exists():
        # The Italian aliases come from all_cards, which updates on its own
        # schedule. A build that used it depends on it, so its snapshot
        # belongs in the corpus's provenance and therefore in its identity.
        db.execute("INSERT INTO meta VALUES ('scryfall_all_cards_updated_at', ?)",
                   (_scryfall_updated_at(bulk_meta_path, "all_cards"),))
    db.execute("INSERT INTO meta VALUES ('built_at', ?)",
               (time.strftime("%Y-%m-%dT%H:%M:%S"),))
    db.execute("INSERT INTO meta VALUES ('plugin_version', ?)",
               (_plugin_version(),))
    db.commit()
    db.close()
    # Sidecar hash — hashing the FINAL file avoids the self-reference bug
    # (a hash stored inside the DB would change the DB's own hash). The MCP
    # server reads corpus.sqlite.sha256 for its corpus_info line, and the
    # release workflow asserts it matches the published artifact.
    sha = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.parent / (out.name + ".sha256")).write_text(sha + "\n")
    print(f"corpus built: {out} rules={len(rules)} cards={card_rows} sha={sha[:16]}")


def _plugin_version() -> str:
    # Stamped into meta.plugin_version so a built corpus records which plugin
    # version produced it. Let a missing/malformed manifest raise: failing the
    # build beats silently stamping a wrong version into a shipped corpus.
    import json as _j
    mani = (Path(__file__).resolve().parent.parent
            / "plugins" / "mtg" / ".claude-plugin" / "plugin.json")
    return _j.loads(mani.read_text())["version"]


def _iter_jsonl(path: Path):
    """Stream objects from a JSONL file, one per line, in constant memory.

    Scryfall moved bulk data from a single JSON array to JSONL, which is why
    this no longer needs ijson: a line is a document, so the stdlib is enough
    even for the multi-gigabyte all_cards file. Blank lines are skipped; a
    malformed line is fatal, because silently dropping cards would produce a
    corpus that looks fine and answers "not found" for whatever fell out.
    """
    with open(path, "r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: malformed JSONL: {exc}") from None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cr-url", required=True)
    ap.add_argument("--out", default=str(DATA / "corpus.sqlite"))
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--with-it-aliases", action="store_true",
                    help="also download all_cards (~2GB) for IT printed names")
    args = ap.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    cr = DATA / "cr.txt"
    oracle = DATA / "oracle_cards.json"
    rl = DATA / "rulings.json"
    allc = DATA / "all_cards.json"
    if not args.skip_download:
        _fetch(args.cr_url, cr)
        bulk = json.loads(urllib.request.urlopen(
            urllib.request.Request("https://api.scryfall.com/bulk-data",
                                   headers=UA), timeout=60).read())
        (DATA / "bulk_meta.json").write_text(json.dumps(bulk))
        # Scryfall replaced `download_uri` with `jsonl_download_uri` when it
        # moved bulk data to JSONL. Fail loudly on the old key rather than
        # KeyError deep in a comprehension, so the next schema change reads as
        # a schema change.
        uris = {}
        for b in bulk["data"]:
            if "jsonl_download_uri" not in b:
                raise SystemExit(
                    f"bulk entry {b.get('type')!r} has no jsonl_download_uri; "
                    "Scryfall's bulk-data schema has changed again")
            uris[b["type"]] = b["jsonl_download_uri"]
        _fetch(uris["oracle_cards"], oracle)
        _fetch(uris["rulings"], rl)
        if args.with_it_aliases:
            _fetch(uris["all_cards"], allc)
    # Gate on the FLAG, not on the file's existence. A leftover all_cards.json
    # from an earlier --with-it-aliases run would otherwise be silently picked
    # up by a later default build, mixing stale Italian aliases into freshly
    # downloaded Oracle data — a corpus nobody asked for and nothing reports.
    aliases = allc if (args.with_it_aliases and allc.exists()) else None
    if args.with_it_aliases and aliases is None:
        print(f"error: --with-it-aliases given but {allc} is missing", file=sys.stderr)
        return 1
    build(cr, oracle, rl, aliases, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
