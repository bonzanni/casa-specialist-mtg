"""The corpus guard is a SIZE ceiling, and these tests are why.

Five content-matching versions of this guard were each bypassed the same way:
the scanner could not finish identifying something and returned clean. Those
bypasses are kept below as tests, because they are the evidence for the
design — every one is caught now not by being recognised, but by being large.

A corpus that escaped the old guard by being wrapped, padded, nested,
concatenated or prefixed is still ~14 MB compressed. There is no encoding of
46 MB of high-entropy SQLite that is small.
"""
import gzip
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from scan_no_corpus import (  # noqa: E402
    ALLOWLIST,
    LFS_POINTER,
    MAX_BLOB_BYTES,
    _verdict,
    describe,
    is_lfs_pointer,
)

ROOT = Path(__file__).resolve().parent.parent


def _corpus_like(path: Path, rows: int = 4000) -> bytes:
    """A SQLite database with the corpus schema, large enough to exceed the
    ceiling — which is now the only property that decides anything.

    The row text is RANDOM, not repeated filler. The size ceiling rests on the
    corpus being high-entropy: the real one is 46 MB that still weighs ~14 MB
    compressed. A fixture of `"x" * 200` gzips to almost nothing, so it would
    pass a compressed-form test for a reason no real corpus enjoys — and the
    test would be asserting a property the guard does not actually have.
    """
    import os

    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE rules(rule_id TEXT, parent_id TEXT, text TEXT, examples TEXT);"
        "CREATE TABLE glossary(term TEXT, definition TEXT);"
        "CREATE TABLE cards(oracle_id TEXT, name TEXT);"
        "CREATE TABLE rulings(oracle_id TEXT, comment TEXT);"
        "CREATE TABLE meta(key TEXT, value TEXT);")
    con.executemany(
        "INSERT INTO rules VALUES (?,?,?,?)",
        [(str(i), None, os.urandom(150).hex(), "") for i in range(rows)])
    con.commit()
    con.close()
    return path.read_bytes()


def _refused(blob: bytes, name: str = "some/file") -> bool:
    return _verdict(name, len(blob), blob) is not None


# --- the five historical bypasses, every one now caught by size ------------

def test_a_bare_corpus_is_refused(tmp_path):
    assert _refused(_corpus_like(tmp_path / "c.sqlite"))


def test_a_gzipped_corpus_is_refused(tmp_path):
    assert _refused(gzip.compress(_corpus_like(tmp_path / "c.sqlite")))


def test_a_corpus_behind_a_benign_prefix_is_refused(tmp_path):
    """Bypass five: twelve bytes ahead of the magic defeated every signature,
    because they all matched at offset zero. Size does not care about offset."""
    raw = _corpus_like(tmp_path / "c.sqlite")
    assert _refused(gzip.compress(b"release-note\n" + raw))


def test_a_corpus_behind_a_concatenated_member_is_refused(tmp_path):
    raw = _corpus_like(tmp_path / "c.sqlite")
    assert _refused(gzip.compress(b"innocent\n") + gzip.compress(raw))


def test_a_repeatedly_compressed_corpus_is_refused(tmp_path):
    """Bypass three: past the recursion limit the old guard gave up and passed.
    Recompressing already-compressed data does not shrink it."""
    blob = _corpus_like(tmp_path / "c.sqlite")
    for _ in range(4):
        blob = gzip.compress(blob)
    assert _refused(blob)


def test_a_corpus_renamed_to_anything_is_refused(tmp_path):
    """The original bypass: `--out corpus.db` walked past every name rule."""
    raw = _corpus_like(tmp_path / "c.sqlite")
    for name in ("corpus.db", "notes.txt", "data", "assets/blob.bin"):
        assert _refused(raw, name), name


# --- the guard must not obstruct ordinary work -----------------------------

def test_ordinary_files_pass():
    for rel in ("README.md", "scripts/scan_no_corpus.py",
                "plugins/mtg/server/mtg_server.py", "manifest.json"):
        blob = (ROOT / rel).read_bytes()
        assert _verdict(rel, len(blob), blob) is None, rel


def test_an_unrelated_small_sqlite_database_passes(tmp_path):
    """Refusing every SQLite file failed CI on any ordinary database, and
    refusing any database sharing ONE corpus table name was the same mistake
    half-fixed. Size is what distinguishes a corpus, not the format."""
    db = tmp_path / "cache.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE cards(id INTEGER)")  # a corpus table name, even
    con.commit()
    con.close()
    assert not _refused(db.read_bytes(), "cache.sqlite")


def test_the_guard_does_not_flag_its_own_sources():
    for rel in ("scripts/scan_no_corpus.py", "tests/test_scan_no_corpus.py"):
        blob = (ROOT / rel).read_bytes()
        assert _verdict(rel, len(blob), blob) is None, rel


def test_every_tracked_file_is_under_the_ceiling_or_allowlisted():
    """The ceiling has to be livable. If this fails, either something large was
    committed or the ceiling needs a deliberate, reasoned change."""
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                         check=True, capture_output=True).stdout
    oversized = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        name = raw.decode()
        if name in ALLOWLIST or "corpus.sqlite" in name:
            continue  # the private repo still tracks the corpus, by design
        blob = subprocess.run(["git", "-C", str(ROOT), "show", f":{name}"],
                              check=True, capture_output=True).stdout
        if len(blob) > MAX_BLOB_BYTES:
            oversized.append((name, len(blob)))
    assert not oversized, f"oversized tracked files: {oversized}"


# --- allowlist and LFS -----------------------------------------------------

def test_the_allowlist_exempts_by_exact_path(tmp_path, monkeypatch):
    raw = _corpus_like(tmp_path / "c.sqlite")
    assert _refused(raw, "big/thing.bin")
    monkeypatch.setitem(ALLOWLIST, "big/thing.bin", "reviewed, not a corpus")
    assert not _refused(raw, "big/thing.bin")
    assert _refused(raw, "big/other.bin"), "the exemption must not generalise"


def test_an_lfs_pointer_is_refused_despite_being_tiny():
    """A pointer is a few hundred bytes and passes any size rule, while still
    publishing whatever it references."""
    pointer = LFS_POINTER + b"\noid sha256:abc\nsize 46596096\n"
    assert is_lfs_pointer(pointer)
    assert _refused(pointer, "plugins/mtg/data/corpus.sqlite")


# --- diagnostics explain a refusal; they never grant one -------------------

@pytest.mark.parametrize("blob,expected", [
    (b"SQLite" + b" format 3\x00" + b"x" * 100, "SQLite"),
    (gzip.compress(b"x" * 100), "compressed"),
    (b"nothing in particular", "unrecognised"),
])
def test_describe_is_advisory(blob, expected):
    assert expected in describe(blob)


def test_an_unrecognised_oversized_blob_is_still_refused():
    """The old guard read 'I do not recognise this' as 'this is fine', which
    is precisely how five different corpora would have been published."""
    blob = bytes(range(256)) * (MAX_BLOB_BYTES // 256 + 10)
    assert "unrecognised" in describe(blob)
    assert _refused(blob)


def test_small_files_carrying_real_upstream_text_are_refused():
    """The ceiling stops bulk but cannot see a 60 KB captured API response or
    a pasted rules excerpt. Signatures ADD this refusal; they never grant one."""
    from scan_no_corpus import CR_MARKER, SCRYFALL_KEYS, looks_like_corpus_text

    scryfall = b'[{"object":"card",' + SCRYFALL_KEYS[0] + b':"x",' + \
        SCRYFALL_KEYS[1] + b':"y"}]'
    assert looks_like_corpus_text(scryfall)
    assert _refused(scryfall, "tests/fixtures/cards.json")

    cr = CR_MARKER + b"\nEffective June 19, 2026.\n100.1. These rules apply...\n"
    assert looks_like_corpus_text(cr)
    assert _refused(cr, "tests/fixtures/rules.txt")


def test_a_signature_miss_never_clears_an_oversized_file():
    """The inversion that defeated five earlier versions: not recognising
    something is not evidence that it is fine."""
    from scan_no_corpus import looks_like_corpus_text

    blob = bytes(range(256)) * (MAX_BLOB_BYTES // 256 + 10)
    assert looks_like_corpus_text(blob) is None
    assert _refused(blob)


def test_ordinary_small_files_are_not_caught_by_the_signatures():
    for rel in ("README.md", "manifest.json", "plugins/mtg/skills/mtg-judge/SKILL.md"):
        blob = (ROOT / rel).read_bytes()
        assert _verdict(rel, len(blob), blob) is None, rel


def test_a_sharded_corpus_is_caught_by_the_aggregate_ceiling(tmp_path):
    """Splitting a large file to dodge a size limit is an ordinary thing to do
    by accident, and it defeats any per-file rule: 56 pieces of a gzipped
    corpus each pass, and the recipient just concatenates them."""
    from scan_no_corpus import MAX_TOTAL_BYTES, _aggregate_verdict

    corpus = gzip.compress(_corpus_like(tmp_path / "c.sqlite", rows=20000))
    shards = [corpus[i:i + MAX_BLOB_BYTES]
              for i in range(0, len(corpus), MAX_BLOB_BYTES)]
    assert len(shards) > 1, "fixture must actually need splitting"

    # The leading shard carries the gzip magic, and this repository contains
    # no compressed files — so reassembly is broken at part 000.
    assert _refused(shards[0], "data/part000.bin")

    # The trailing shards are headerless and genuinely unrecognisable; that is
    # the point of the aggregate ceiling, which sees the total they add up to.
    assert not _refused(shards[-1], f"data/part{len(shards) - 1:03d}.bin")
    assert _aggregate_verdict(sum(len(s) for s in shards), "tracked files")


def test_the_aggregate_ceiling_leaves_room_for_the_real_tree():
    """A guard that trips on ordinary growth gets disabled, so this pins that
    the real repository sits well inside the limit."""
    from scan_no_corpus import MAX_TOTAL_BYTES, _aggregate_verdict

    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                         check=True, capture_output=True).stdout
    total = 0
    for raw in out.split(b"\0"):
        if not raw:
            continue
        name = raw.decode()
        if "corpus.sqlite" in name:
            continue  # the private repo tracks the corpus by design
        total += len(subprocess.run(
            ["git", "-C", str(ROOT), "show", f":{name}"],
            check=True, capture_output=True).stdout)
    assert _aggregate_verdict(total, "tracked files") is None
    assert total < MAX_TOTAL_BYTES // 4, (
        f"tree is {total} bytes, uncomfortably close to {MAX_TOTAL_BYTES}")


def test_a_compressed_rules_dump_is_refused():
    """Sol's finding: all 3,153 rules gzip to ~178 KB, which sat under the old
    256 KB ceiling and carries no file header to recognise. The ceiling came
    down, and the signature now peels one layer and counts rule-numbered
    lines — a shape ordinary source and prose do not have."""
    from scan_no_corpus import looks_like_corpus_text

    dump = "\n".join(f"{100 + i // 9}.{i % 9 + 1}{'abc'[i % 3]} Some rule text."
                     for i in range(300)).encode()
    assert looks_like_corpus_text(dump)
    assert _refused(gzip.compress(dump), "fixtures/rules.txt.gz")


def test_ordinary_source_is_not_mistaken_for_rules_text():
    """The rule-line pattern must not fire on version numbers, IP addresses,
    timestamps or decimals in ordinary files."""
    from scan_no_corpus import looks_like_corpus_text

    for rel in ("README.md", "scripts/build_corpus.py",
                "plugins/mtg/server/mtg_server.py",
                "plugins/mtg/skills/mtg-judge/SKILL.md", "manifest.json"):
        assert looks_like_corpus_text((ROOT / rel).read_bytes()) is None, rel
    assert looks_like_corpus_text(
        b"\n".join(b"192.168.1.%d  v1.2.3  10.5.0" % i for i in range(100))) is None


def test_peeling_never_clears_anything(tmp_path):
    """_peel exists only to find MORE. A blob it cannot decompress is judged
    exactly as before — the old design's fatal move was the opposite."""
    from scan_no_corpus import _peel

    blob = b"\x1f\x8b" + b"not actually gzip"
    assert _peel(blob) == blob
    big = _corpus_like(tmp_path / "c.sqlite")
    assert _refused(big)


def test_no_compressed_file_may_be_tracked(tmp_path):
    """This repository ships source, markdown, JSON and YAML. A compressed
    blob is either upstream data or the head of a split archive."""
    import bz2
    import lzma

    from scan_no_corpus import compressed_kind

    payload = b"harmless enough on its own\n" * 50
    for name, blob in (("gzip", gzip.compress(payload)),
                       ("bzip2", bz2.compress(payload)),
                       ("xz", lzma.compress(payload)),
                       ("zip", b"PK\x03\x04" + payload)):
        assert compressed_kind(blob) == name
        assert _refused(blob, f"assets/thing.{name}"), name


def test_the_compressed_rule_does_not_fire_on_ordinary_files():
    from scan_no_corpus import compressed_kind

    for rel in ("README.md", "manifest.json", "scripts/scan_no_corpus.py"):
        assert compressed_kind((ROOT / rel).read_bytes()) is None, rel


def test_peeling_a_bomb_neither_hangs_nor_exhausts_memory():
    """_peel used whole-buffer decompressors and checked the size afterwards,
    so a 64 KB input could allocate gigabytes before any limit applied."""
    from scan_no_corpus import _peel

    from scan_no_corpus import MAX_PEELED_BYTES

    # Sized so the compressed form is a small, ordinary-looking tracked file
    # while the expansion blows the peel cap — the case where a whole-buffer
    # decompressor allocates first and checks second.
    bomb = gzip.compress(b"\0" * (MAX_PEELED_BYTES * 2))
    assert len(bomb) < MAX_BLOB_BYTES, "the bomb must look like a small file"
    assert _peel(bomb) == bomb, "an oversized expansion must conclude nothing"


def test_ordinary_decimal_data_is_not_mistaken_for_rules():
    """The pattern has now false-positived twice: on IP octets, then on plain
    decimals like "100.25 measurement". Real rules always carry a terminating
    period or a subrule letter, and that is what distinguishes them."""
    from scan_no_corpus import looks_like_corpus_text

    for sample in (
        b"\n".join(b"100.%d measurement reading" % (20 + i) for i in range(40)),
        b"\n".join(b"192.168.1.%d gateway" % i for i in range(40)),
        b"\n".join(b"404.%d error rate" % i for i in range(40)),
    ):
        assert looks_like_corpus_text(sample) is None


def test_both_real_rule_shapes_are_recognised():
    from scan_no_corpus import looks_like_corpus_text

    numbered = b"\n".join(b"%d.1. A numbered rule." % (100 + i) for i in range(30))
    subrules = b"\n".join(b"702.%db A subrule with a letter." % i for i in range(30))
    assert looks_like_corpus_text(numbered)
    assert looks_like_corpus_text(subrules)


def test_history_ignores_the_allowlist(monkeypatch, tmp_path):
    """rev-list prints one path per blob, so a blob committed at an unapproved
    path and later at an approved one could be judged only under the approved
    name. History therefore exempts nothing."""
    from scan_no_corpus import _HISTORY, _verdict

    raw = _corpus_like(tmp_path / "c.sqlite")
    monkeypatch.setitem(ALLOWLIST, "approved/path.bin", "reviewed")
    assert _verdict("approved/path.bin", len(raw), raw) is None  # working tree
    assert _verdict(_HISTORY, len(raw), raw) is not None         # history
    assert _HISTORY not in ALLOWLIST


def test_a_file_whose_name_is_not_utf8_is_still_weighed(tmp_path, monkeypatch):
    """The path was decoded with replacement and then looked up by that
    mangled name, so `git show` failed and the file was skipped silently —
    counted in neither the per-file nor the aggregate check."""
    import os

    import scan_no_corpus as sc

    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "x").write_bytes(b"ok\n")
    os.rename(repo / "x", repo / os.fsdecode(b"bad\xff\xfename.bin"))
    (repo / os.fsdecode(b"bad\xff\xfename.bin")).write_bytes(
        os.urandom(MAX_BLOB_BYTES + 1024))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)

    monkeypatch.chdir(repo)
    hits = sc.scan_tracked()
    assert hits, "an oversized blob must be refused whatever its name decodes to"


def _repo(tmp_path, name="r"):
    import subprocess as sp
    r = tmp_path / name
    r.mkdir()
    sp.run(["git", "init", "-q", str(r)], check=True)
    return r


def _commit(repo, msg="c"):
    import subprocess as sp
    sp.run(["git", "-C", str(repo), "add", "-A"], check=True)
    sp.run(["git", "-C", str(repo), "-c", "user.name=x", "-c",
            "user.email=x@y", "commit", "-qm", msg], check=True)


def test_ordinary_churn_does_not_eventually_brick_ci(tmp_path, monkeypatch):
    """The aggregate was summed across every blob version ever committed, so a
    repository merely EDITED enough would trip it while no commit ever held a
    large tree. The ceiling asks a question about a tree; ask it of each tree."""
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    for i in range(40):
        (repo / "snap.txt").write_bytes(os.urandom(15000).hex().encode()[:30000])
        _commit(repo, f"r{i}")
    monkeypatch.chdir(repo)
    assert not sc.scan_history(), "normal maintenance must not fail the guard"


def test_a_single_oversized_tree_is_still_caught(tmp_path, monkeypatch):
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    # Enough under-ceiling files that the TREE exceeds the aggregate limit —
    # which is exactly the sharded-corpus shape, arriving as many innocent
    # files rather than one guilty one.
    per_file = sc.MAX_BLOB_BYTES - 1024
    count = sc.MAX_TOTAL_BYTES // per_file + 2
    for i in range(count):
        (repo / f"part{i}.bin").write_bytes(os.urandom(per_file))
    _commit(repo, "many medium files, one large tree")
    monkeypatch.chdir(repo)
    hits = sc.scan_history()
    assert any("tree of" in name for name, _ in hits), hits


def test_a_historical_exemption_binds_to_blob_identity(tmp_path, monkeypatch):
    """Paths are ambiguous in history — rev-list prints only one per blob — so
    an exemption keyed by path could never be honoured there. An object id
    names exactly the bytes a human approved."""
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    payload = os.urandom(sc.MAX_BLOB_BYTES + 4096)
    (repo / "big.bin").write_bytes(payload)
    _commit(repo, "one big file")
    monkeypatch.chdir(repo)

    assert sc.scan_history(), "unexempted, it must be refused"

    oid = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD:big.bin"],
                         check=True, capture_output=True).stdout.decode().strip()
    monkeypatch.setitem(sc.ALLOWLIST_BLOBS, oid, "reviewed: not corpus data")
    assert not sc.scan_history(), "an approved blob id must be honoured"


def test_an_lzma_header_cannot_demand_a_huge_allocation():
    """An LZMA header declares its dictionary size and the decompressor
    allocates that up front — before any output exists for the cap to measure.
    A 31-byte file can ask for a gigabyte."""
    import lzma

    from scan_no_corpus import MAX_PEELED_BYTES, _peel

    comp = lzma.LZMACompressor(
        format=lzma.FORMAT_ALONE,
        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 30}])
    blob = comp.compress(b"x" * 1000) + comp.flush()
    assert len(blob) < 1024, "must look like a trivially small file"
    assert _peel(blob) == blob, "an over-limit dictionary must conclude nothing"


def test_history_covers_stash_notes_and_custom_ref_namespaces(tmp_path, monkeypatch):
    """`--all` is all of refs/ plus HEAD, and `push --mirror` pushes refs/*.
    A blob reachable only from refs/notes or a custom namespace is therefore
    publishable, and must be seen."""
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    (repo / "a").write_bytes(b"base\n")
    _commit(repo, "base")

    (repo / "custom.bin").write_bytes(os.urandom(sc.MAX_BLOB_BYTES + 2048))
    subprocess.run(["git", "-C", str(repo), "add", "custom.bin"], check=True)
    tree = subprocess.run(["git", "-C", str(repo), "write-tree"],
                          check=True, capture_output=True).stdout.decode().strip()
    commit = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=x", "-c", "user.email=x@y",
         "commit-tree", tree, "-m", "hidden"],
        check=True, capture_output=True).stdout.decode().strip()
    subprocess.run(["git", "-C", str(repo), "update-ref",
                    "refs/custom/thing", commit], check=True)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "--cached",
                    "custom.bin"], check=True)
    (repo / "custom.bin").unlink()

    monkeypatch.chdir(repo)
    hits = sc.scan_history()
    assert any("custom.bin" in name for name, _ in hits), hits


def test_a_size_exemption_does_not_become_a_content_exemption(monkeypatch):
    """An ALLOWLIST entry is a judgement about size, made by someone looking
    at a size. It must not also wave through recognised rules text."""
    from scan_no_corpus import CR_MARKER

    rules = CR_MARKER + b"\n" + b"\n".join(
        b"%d.1. A rule." % (100 + i) for i in range(40))
    assert _refused(rules, "docs/excerpt.txt")
    monkeypatch.setitem(ALLOWLIST, "docs/excerpt.txt", "big but fine")
    assert _refused(rules, "docs/excerpt.txt"), \
        "the exemption covers size, not content"


def test_a_ref_pointing_straight_at_a_tree_is_weighed(tmp_path, monkeypatch):
    """`rev-list --all` walks commits, and a ref can name a tree directly —
    `git update-ref refs/tags/x <tree>`. Such a ref emits no commit, so a
    commit-only walk skipped the aggregate entirely while `push --mirror`
    still carried the tree."""
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    (repo / "a").write_bytes(b"base\n")
    _commit(repo, "base")

    per_file = sc.MAX_BLOB_BYTES - 4096
    for i in range(sc.MAX_TOTAL_BYTES // per_file + 2):
        (repo / f"p{i}.bin").write_bytes(os.urandom(per_file))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    tree = subprocess.run(["git", "-C", str(repo), "write-tree"],
                          check=True, capture_output=True).stdout.decode().strip()
    subprocess.run(["git", "-C", str(repo), "update-ref",
                    "refs/tags/tree-only", tree], check=True)
    subprocess.run(["git", "-C", str(repo), "rm", "-q", "--cached"] +
                   [f"p{i}.bin" for i in range(sc.MAX_TOTAL_BYTES // per_file + 2)],
                   check=True)
    for i in range(sc.MAX_TOTAL_BYTES // per_file + 2):
        (repo / f"p{i}.bin").unlink()

    monkeypatch.chdir(repo)
    hits = sc.scan_history()
    assert any("tree at" in name or "tree of" in name for name, _ in hits), hits


def test_history_sees_a_blob_reachable_only_from_the_stash(tmp_path, monkeypatch):
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    (repo / "a").write_bytes(b"base\n")
    _commit(repo, "base")
    (repo / "secret.bin").write_bytes(os.urandom(sc.MAX_BLOB_BYTES + 2048))
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c",
                    "user.email=x@y", "stash", "-q", "-u"], check=True)

    monkeypatch.chdir(repo)
    assert any("secret.bin" in n for n, _ in sc.scan_history())


def test_history_sees_a_blob_reachable_only_from_notes(tmp_path, monkeypatch):
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    (repo / "a").write_bytes(b"base\n")
    _commit(repo, "base")
    note = tmp_path / "note.txt"
    note.write_bytes(os.urandom(sc.MAX_BLOB_BYTES).hex().encode())
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=x", "-c",
                    "user.email=x@y", "notes", "add", "-F", str(note), "HEAD"],
                   check=True)

    monkeypatch.chdir(repo)
    assert sc.scan_history(), "a note blob over the ceiling must be refused"


def test_an_approved_oversized_blob_is_still_checked_for_content(tmp_path, monkeypatch):
    """Oversized blobs are normally judged on recorded size without being
    read. An APPROVED one must still be read, or the size waiver becomes a
    content waiver and an allowlisted oversized gzip vanishes from the scan."""
    import os

    import scan_no_corpus as sc

    repo = _repo(tmp_path)
    payload = gzip.compress(os.urandom(sc.MAX_BLOB_BYTES * 2))
    assert len(payload) > sc.MAX_BLOB_BYTES
    (repo / "blob.gz").write_bytes(payload)
    _commit(repo, "a compressed file")
    monkeypatch.chdir(repo)

    assert sc.scan_history(), "unexempted, it must be refused"

    oid = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD:blob.gz"],
                         check=True, capture_output=True).stdout.decode().strip()
    monkeypatch.setitem(sc.ALLOWLIST_BLOBS, oid, "size approved")
    hits = sc.scan_history()
    assert any("stream" in reason for _, reason in hits), (
        "a size exemption must not silence the compressed-stream signature")
