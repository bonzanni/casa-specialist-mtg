#!/usr/bin/env python3
"""Refuse to ship a corpus that parsed badly.

The real hazard of building unattended is not a crash — a crash fails the
workflow and someone looks. It is a parser that half-works: the build
succeeds, the sidecar matches, setup_corpus verifies the schema, and the
result answers "no rules match" to questions it should answer, or cites a
rule that is missing half its text. Every check downstream is about integrity
and identity; none of them asks whether the thing is any good.

So this asks. The floors are deliberately far below the real numbers (~3,150
rules, ~735 glossary entries, ~36,000 cards, ~76,800 rulings): they exist to
catch a parse that collapsed, not to track upstream growth, and a threshold
that needs adjusting every set would soon be raised until it meant nothing.

Usage:
  python3 scripts/check_corpus_plausible.py plugins/mtg/data/corpus.sqlite
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

FLOORS = {
    "rules": 2500,
    "glossary": 500,
    "cards": 25000,
    "rulings": 50000,
}


def problems(path: Path, notes: list[str] | None = None) -> list[str]:
    """Every reason this corpus should not be published. Empty means fine.

    `notes` collects observations that are not themselves refusals but that a
    human should see — chiefly the count of values that tokenise to nothing,
    which is the one thing the searchability probe below is allowed to excuse.
    A sudden jump in it means the data changed shape, and absorbing it
    silently is how a tolerance turns into a budget.
    """
    found: list[str] = []
    notes = notes if notes is not None else []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        for table, floor in FLOORS.items():
            try:
                n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                found.append(f"{table}: unreadable ({exc})")
                continue
            if n < floor:
                found.append(f"{table}: {n} rows, expected at least {floor}")

        meta = dict(con.execute("SELECT key, value FROM meta"))
        if meta.get("cr_effective_date", "unknown") == "unknown":
            # The date is parsed out of the rules text. Losing it means the
            # header changed shape, which usually means the rest did too —
            # and every ruling would cite provenance it does not have.
            found.append("cr_effective_date did not parse")

        # A rules table full of empty text passes a row count.
        empty = con.execute(
            "SELECT count(*) FROM rules WHERE text IS NULL OR trim(text) = ''"
        ).fetchone()[0]
        if empty:
            found.append(f"{empty} rules have no text")

        # Row counts say nothing about whether the rows are usable. A bulk
        # response of objects carrying oracle_id but no name yields plenty of
        # `cards` rows, an empty cards_fts, and no card that can be looked up
        # — and setup_corpus only checks that cards_fts EXISTS, not that
        # anything is in it.
        for table, column in (("cards", "name"), ("cards", "oracle_text"),
                              ("rulings", "comment")):
            try:
                blank = con.execute(
                    f"SELECT count(*) FROM {table} "
                    f"WHERE {column} IS NULL OR trim({column}) = ''"
                ).fetchone()[0]
                total = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error as exc:
                found.append(f"{table}.{column}: unreadable ({exc})")
                continue
            # Tolerances chosen per column rather than a blanket half. Half
            # the cards having no name passed the earlier check while the
            # corpus was unusable; meanwhile plenty of real cards legitimately
            # have no rules text, so that one has to stay loose.
            # Set against MEASURED reality (0% nameless, 2% textless, 0%
            # empty rulings), with room for a set release, not at some round
            # number that happens to admit thousands of unusable rows. 60%
            # textless "passed" while 21,000 cards had no rules text.
            limit = {"name": 0.001, "oracle_text": 0.10, "comment": 0.01}[column]
            if total and blank >= total * limit:
                found.append(
                    f"{table}.{column}: {blank} of {total} rows are empty "
                    f"(over the {limit:.0%} tolerance)")

        # Most cards have a cost; lands legitimately do not. The floor is far
        # below the real number (~30,000 of 36,000) for the same reason as
        # FLOORS above: it catches a column that filled with nothing, not
        # upstream drift. A corpus built before mana_cost existed has no
        # column at all and is not this failure — say so and move on, because
        # refusing it would retroactively condemn a corpus that was fine when
        # it was built and is still pinned in the field.
        try:
            priced = con.execute(
                "SELECT count(*) FROM cards WHERE trim(mana_cost) <> ''"
            ).fetchone()[0]
        except sqlite3.Error:
            notes.append("cards has no mana_cost column (pre-0.6 corpus)")
        else:
            # The builder declares the column NOT NULL, so a NULL means this
            # corpus was not built by it. It matters beyond tidiness: the
            # count above steps straight over NULLs, so 20,000 real costs can
            # sit alongside any number of rows the server would then have to
            # decline to describe.
            nulls = con.execute(
                "SELECT count(*) FROM cards WHERE mana_cost IS NULL"
            ).fetchone()[0]
            if nulls:
                found.append(
                    f"cards: {nulls} rows have a NULL mana_cost; the column is "
                    "declared NOT NULL, so this corpus was not built by "
                    "scripts/build_corpus.py")
            if priced < 20000:
                found.append(
                    f"cards: only {priced} rows carry a mana_cost, "
                    f"expected at least 20000")

        # PROPORTIONATE, not merely non-empty. One row in cards_fts passed a
        # "not empty" test while almost every card lookup failed, which is the
        # same shape of hole as accepting a corpus because it has rows.
        for fts, base in (("cards_fts", "cards"), ("rules_fts", "rules")):
            try:
                n = con.execute(f"SELECT count(*) FROM {fts}").fetchone()[0]
                b = con.execute(f"SELECT count(*) FROM {base}").fetchone()[0]
            except sqlite3.Error as exc:
                found.append(f"{fts}: unreadable ({exc}) — search would fail")
                continue
            # Near-complete, not merely half. Half an index means half of all
            # searches fail — a corpus that answers most questions with a
            # confident "no rules match", which is the worst outcome this
            # component has.
            if b and n < b * 0.9:
                found.append(
                    f"{fts} holds {n} rows for {b} in {base}; searches would "
                    "miss a large share of the corpus")

        # CORRESPONDENCE, not merely proportion. Reviewers built indexes with
        # exactly the right number of rows over unrelated text — one of them
        # by taking the real corpus and overwriting every indexed string with
        # "zzzxxyy" — and the server then answered "no rules match" for a
        # phrase present in every single rule. An index is only an index OF
        # something.
        #
        # Checked EXHAUSTIVELY, and by reading the stored strings rather than
        # by searching for them. Two earlier attempts failed for reasons worth
        # recording, because both are tempting:
        #
        #   Sampling the first 200 rows is predictable, so an index can be
        #   made correct exactly where it is inspected: 2,953 of 3,153 rule
        #   rows unrelated, `Island` returning nothing, and a clean bill.
        #   Any fixed sample has this property; a random one trades it for
        #   irreproducibility and still only bounds the damage statistically.
        #
        #   Searching for a token taken from the row means guessing how FTS5
        #   tokenised it. Stripping punctuation from "two-player" produced
        #   "twoplayer", which is indexed as two terms and matches nothing —
        #   that version REJECTED the real corpus, 26 misses in 200. A check
        #   that fails on the only known-good input is worse than no check.
        #
        # Comparing stored strings needs neither a sample nor a tokeniser.
        # These tables are small enough to hold in memory (3k rules, 37k
        # cards) and the comparison is a set difference.
        for fts, base, key, column in (
                ("rules_fts", "rules", "rule_id", "text"),
                ("cards_fts", "cards", "oracle_id", "name")):
            try:
                # Whitespace-normalised, and CONTAINMENT rather than
                # equality: rules_fts deliberately indexes `text + " " +
                # examples`, so the indexed string is a superset of the row's
                # own text. Demanding equality would re-encode the builder's
                # formatting here and break the moment it changed — it
                # already rejected the real corpus for a trailing space.
                # Containment is weaker but it is aimed at the actual defect,
                # which is the row's text not being in the index at all.
                indexed: dict[str, list[str]] = {}
                for k, v in con.execute(f"SELECT {key}, {column} FROM {fts}"):
                    indexed.setdefault(str(k), []).append(
                        " ".join(str(v).split()))
                stored = [
                    (str(k), " ".join(str(v).split()))
                    for k, v in con.execute(
                        f"SELECT {key}, {column} FROM {base} "
                        f"WHERE {column} IS NOT NULL AND trim({column}) != ''")]
            except sqlite3.Error as exc:
                found.append(f"{fts}/{base}: unreadable ({exc})")
                continue
            # Every base row must be findable in the index under its own key.
            # Not the converse: cards_fts legitimately carries extra rows for
            # individual card faces, which have no row of their own in
            # `cards`.
            missing = sum(
                1 for k, text in stored
                if not any(text in got for got in indexed.get(k, ())))
            if stored and missing:
                found.append(
                    f"{fts} does not index {base}: {missing} of {len(stored)} "
                    f"rows are absent from the index under their own {column}")

        # THE INDEX ITSELF, not the strings beside it.
        #
        # Everything above reads the columns an FTS table exposes as content.
        # That is not the inverted index, and the two can part company:
        # reviewers passed this checker with an external-content table
        # echoing all 3,153 rows over an empty index, with the shadow
        # `..._data` blocks deleted (MATCH then raises "database disk image
        # is malformed", which the server renders as "no rules match"), and
        # with the virtual tables replaced by ordinary tables of identical
        # rows. Correspondence proves the content is right; only running a
        # query proves the thing is searchable.
        #
        # Probed with the row's WHOLE value as an FTS5 phrase, and with the
        # key expressed as a column filter rather than a SQL predicate.
        #
        # Both details are load-bearing. A phrase is tokenised by the same
        # tokeniser that indexed the content, so there is nothing to guess —
        # an earlier version picked a word and stripped its punctuation,
        # turned "two-player" into "twoplayer", and REJECTED the real
        # corpus. And `MATCH 'word' AND key = ?` filters after the match, so
        # a common token makes it quadratic: on a corpus whose card names
        # share a token it ran for minutes. `key:"..." AND col:"..."` is one
        # index lookup.
        #
        # EVERY row. The first version stopped at 300 — the same
        # predictable-prefix mistake that had just been removed from the
        # correspondence check above — and both reviewers walked through it
        # with an external-content index covering exactly those 300 rows and
        # nothing else: 2,853 of 3,153 rules with no postings, a clean bill
        # of health. Exhaustive costs 1.4 seconds on the real corpus.
        #
        # And ZERO unexplained misses, not a small allowance. The allowance
        # was `max(10, 0.5%)`, which reads as tight and is not: on 36,018
        # cards it is 180 rows, and a reviewer built an external-content
        # index missing exactly 178 postings — 180 misses against a limit of
        # 180.09 — that this checker declared healthy. A tolerance is a
        # budget, and a half-failed build spends it as readily as anyone.
        #
        # What the allowance was actually for is real, though: two of the
        # 36,018 card names ("_____", "______") tokenise to nothing at all,
        # so no query of any kind can return them and a zero-miss rule would
        # reject the only known-good input. That is a property of the VALUE,
        # decidable without consulting the index — so decide it, with the
        # same tokeniser, and excuse only those.
        #
        # --- BEGIN searchability probe (tests/test_corpus_automation.py
        # ---   excises everything between these markers to prove the
        # ---   surrounding checks do not already cover it)
        def _phrase(value: str) -> str:
            return '"' + str(value).replace('"', '""') + '"'

        def _tokeniser(fts: str) -> str | None:
            """The tokenize= option `fts` was declared with, '' for default.

            Mirrored rather than assumed: asking a default scratch index
            whether a value tokenises to nothing, while the real index used
            some other tokeniser, is the "validate with a tool more
            permissive than the consumer" mistake this file has made before.
            None means the declaration is not something safe to mirror.
            """
            row = con.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (fts,)).fetchone()
            m = re.search(r"tokenize\s*=\s*(['\"])(.*?)\1", (row and row[0]) or "")
            if m is None:
                return ""
            # An allowlist, because this string is interpolated into a CREATE
            # and the corpus is the thing under suspicion.
            return m.group(2) if re.fullmatch(r"[a-z0-9_ ]+", m.group(2)) else None

        con.execute("ATTACH ':memory:' AS scratch")

        for fts, base, key, column, keyed in (
                ("rules_fts", "rules", "rule_id", "text", True),
                # cards_fts declares oracle_id UNINDEXED, so it cannot appear
                # in a column filter; match on the name and confirm the key
                # among the rows that come back.
                ("cards_fts", "cards", "oracle_id", "name", False)):
            try:
                rows = con.execute(
                    f"SELECT {key}, {column} FROM {base} "
                    f"WHERE {column} IS NOT NULL AND trim({column}) != ''"
                ).fetchall()
            except sqlite3.Error as exc:
                found.append(f"{base}: unreadable while probing ({exc})")
                continue
            if not rows:
                continue
            misses: list[tuple[str, str]] = []
            broken = False
            for key_value, value in rows:
                try:
                    if keyed:
                        hit = con.execute(
                            f"SELECT 1 FROM {fts} WHERE {fts} MATCH ? LIMIT 1",
                            (f"{key}:{_phrase(key_value)} AND "
                             f"{column}:{_phrase(value)}",)).fetchone()
                    else:
                        hit = (str(key_value),) in {
                            (str(r[0]),) for r in con.execute(
                                f"SELECT {key} FROM {fts} WHERE {fts} MATCH ?",
                                (f"{column}:{_phrase(value)}",))} or None
                except sqlite3.Error as exc:
                    # A raise here is the finding: an ordinary table has no
                    # MATCH, and a damaged index reports corruption.
                    found.append(
                        f"{fts} cannot be searched ({exc}); every lookup "
                        "against this corpus would fail")
                    broken = True
                    break
                if hit is None:
                    misses.append((str(key_value), str(value)))
            if broken:
                continue

            # Split the misses: a value with no tokens cannot be found by any
            # query, which is not a fact about the index; anything else is a
            # posting that should be there and is not.
            tok = _tokeniser(fts)
            if tok is None:
                found.append(
                    f"{fts} declares a tokeniser this checker cannot mirror; "
                    "it cannot tell an unindexable value from a missing one")
                continue
            option = f", tokenize='{tok}'" if tok else ""
            con.execute("DROP TABLE IF EXISTS scratch.tok")
            con.execute("DROP TABLE IF EXISTS scratch.tok_terms")
            con.execute(f"CREATE VIRTUAL TABLE scratch.tok USING fts5(v{option})")
            con.execute(
                "CREATE VIRTUAL TABLE scratch.tok_terms "
                "USING fts5vocab(tok, 'row')")

            def _tokens(value: str) -> int:
                con.execute("DELETE FROM scratch.tok")
                con.execute("INSERT INTO scratch.tok(v) VALUES (?)", (value,))
                return con.execute(
                    "SELECT count(*) FROM scratch.tok_terms").fetchone()[0]

            unindexable = 0
            unexplained: list[str] = []
            for key_value, value in misses:
                # The keyed probe puts the key in the query too, so a key that
                # tokenises to nothing makes the row unfindable BY THIS PROBE
                # however good the index is. Excusing the row on that basis
                # was wrong and this is the finding that came back: a rule
                # whose id tokenises to nothing but whose text tokenises
                # normally had its posting removed, and the checker reported
                # it as unindexable text and passed the corpus. The key is a
                # property of the probe, so re-probe without it rather than
                # letting it excuse anything.
                if keyed and _tokens(key_value) == 0:
                    try:
                        again = con.execute(
                            f"SELECT {key} FROM {fts} WHERE {fts} MATCH ?",
                            (f"{column}:{_phrase(value)}",)).fetchall()
                    except sqlite3.Error as exc:
                        found.append(
                            f"{fts} cannot be searched ({exc}); every lookup "
                            "against this corpus would fail")
                        break
                    if any(str(r[0]) == key_value for r in again):
                        continue
                # Classified on the VALUE alone. A value with no tokens
                # cannot be returned by any query; anything else is a posting
                # that should be there and is not.
                if _tokens(value) == 0:
                    unindexable += 1
                else:
                    unexplained.append(value)

            notes.append(
                f"{fts}: {unindexable} of {len(rows)} {base} rows have a "
                f"{column} that tokenises to nothing and can never be found")
            if unexplained:
                sample = ", ".join(repr(v[:40]) for v in unexplained[:3])
                found.append(
                    f"{fts} is not a working index of {base}: "
                    f"{len(unexplained)} of {len(rows)} rows cannot be found "
                    f"by searching for their own {column}, and their "
                    f"{column} does tokenise ({sample})")
            # The excuse itself is bounded. Measured reality is 2 of 36,018
            # and 0 of 3,153; the way to hide a hole behind this exemption is
            # to replace real values with punctuation, and that is worth
            # refusing too rather than discovering later.
            if unindexable > max(10, len(rows) * 0.001):
                found.append(
                    f"{fts}: {unindexable} of {len(rows)} {base} rows have a "
                    f"{column} that tokenises to nothing; no search can ever "
                    "return them")
        # --- END searchability probe

        # Rulings that reference no card are rulings nobody can reach.
        # get_rulings joins on oracle_id, so a rulings table whose oracle_id
        # is uniformly NULL answers every card's rulings query with nothing
        # while passing the row-count floor and the blank-comment tolerance
        # above — 60,000 well-formed comments, none of them reachable.
        try:
            unlinked = con.execute(
                "SELECT count(*) FROM rulings WHERE oracle_id IS NULL "
                "OR trim(oracle_id) = ''").fetchone()[0]
            total_r = con.execute("SELECT count(*) FROM rulings").fetchone()[0]
            # Set difference in Python rather than a correlated subquery:
            # cards.oracle_id carries no index, so NOT EXISTS scans the whole
            # cards table once per ruling — half a minute on a real corpus.
            # Both id sets together are a few MB.
            known = {
                r[0] for r in con.execute(
                    "SELECT DISTINCT oracle_id FROM cards "
                    "WHERE oracle_id IS NOT NULL")
            }
            orphan = sum(
                1 for (oid,) in con.execute(
                    "SELECT oracle_id FROM rulings WHERE oracle_id IS NOT NULL "
                    "AND trim(oracle_id) != ''")
                if oid not in known)
        except sqlite3.Error as exc:
            found.append(f"rulings.oracle_id: unreadable ({exc})")
        else:
            if total_r and unlinked >= total_r * 0.01:
                found.append(
                    f"rulings.oracle_id: {unlinked} of {total_r} rows carry no "
                    "oracle_id; those rulings can never be looked up")
            # Rulings legitimately outlive the oracle_cards snapshot they are
            # paired with, so this is a drift tolerance, not equality.
            if total_r and orphan >= total_r * 0.10:
                found.append(
                    f"rulings.oracle_id: {orphan} of {total_r} rows reference "
                    "an oracle_id absent from cards; the two datasets do not "
                    "belong to each other")

        # Anchors. Everything above measures shape — a syntactically perfect
        # feed of invented cards and numbered nonsense satisfies all of it.
        # These are things any real Magic corpus contains and no substituted
        # dataset would: the basic lands, a few cards continuously in print
        # for decades, and two rules whose numbering has been stable for the
        # life of the CR. Deliberately tiny and deliberately boring — an
        # anchor list that tracked recent sets would need editing forever.
        for rule_id in ("100.1", "702.2"):
            try:
                row = con.execute(
                    "SELECT text FROM rules WHERE rule_id = ?", (rule_id,)
                ).fetchone()
            except sqlite3.Error as exc:
                found.append(f"rules[{rule_id}]: unreadable ({exc})")
                continue
            if row is None or not (row[0] or "").strip():
                found.append(
                    f"CR {rule_id} is missing or empty; this does not look "
                    "like the Comprehensive Rules")
        anchors = ("Island", "Forest", "Mountain", "Plains", "Swamp",
                   "Lightning Bolt", "Counterspell", "Llanowar Elves")
        try:
            present = {
                r[0] for r in con.execute(
                    "SELECT name FROM cards WHERE name IN "
                    f"({','.join('?' * len(anchors))})", anchors)
            }
        except sqlite3.Error as exc:
            found.append(f"cards anchor lookup unreadable ({exc})")
        else:
            missing = [a for a in anchors if a not in present]
            if missing:
                found.append(
                    f"cards is missing {len(missing)} of {len(anchors)} anchor "
                    f"cards ({', '.join(missing)}); this does not look like "
                    "Scryfall's card data")

        # Subrules are the shape that breaks first when the parser drifts.
        subrules = con.execute(
            "SELECT count(*) FROM rules WHERE parent_id IS NOT NULL AND parent_id != ''"
        ).fetchone()[0]
        if subrules < 1000:
            found.append(f"only {subrules} subrules; the parser likely lost them")
    finally:
        con.close()
    return found


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        print(f"error: no corpus at {path}", file=sys.stderr)
        return 1
    notes: list[str] = []
    found = problems(path, notes)
    # Printed either way. These are the counts the checker chose not to refuse
    # over; a build log that shows them is the only way a drift in them gets
    # noticed before it matters.
    for line in notes:
        print(f"note: {line}", file=sys.stderr)
    if found:
        print("implausible corpus, refusing to publish:", file=sys.stderr)
        for line in found:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"{path} looks like a real corpus")
    return 0


if __name__ == "__main__":
    sys.exit(main())
