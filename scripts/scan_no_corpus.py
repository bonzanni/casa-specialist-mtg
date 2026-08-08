#!/usr/bin/env python3
"""Refuse corpus data in this repository.

This repository does not redistribute Comprehensive Rules text, Oracle card
text, or rulings; see README.md, "Rules data and attribution". `.gitignore`
covers the paths, and publication uses an explicit file allowlist — this is
the backstop that catches an accident neither of those did.

THE GUARD IS A SIZE CEILING, and that choice was expensive to learn.

The first five versions of this file tried to recognise corpus data by its
content: SQLite magic bytes, Scryfall keys, the CR header. Every version was
bypassed, always the same way — the scanner could not finish identifying
something and returned "clean":

  - a plain `tar czf corpus.tar.gz` (no decompression at all)
  - a 513 MB padding member ahead of the corpus (budget exhausted -> clean)
  - the corpus gzipped four times (depth limit -> clean)
  - a second concatenated gzip member (only the first was read)
  - twelve bytes of prefix inside one member (magic no longer at offset zero)

Content matching cannot establish ABSENCE. It has to enumerate every
container format, every nesting, and every offset, and anything it fails to
recognise falls through as ordinary data. Each fix closed one hole and left
an identically-shaped one beside it.

Size cannot be argued with. The corpus is ~46 MB of high-entropy SQLite; it
compresses to ~14 MB and stays large however it is wrapped, prefixed, nested
or re-encoded. Every legitimate file here is a few KB — the largest is about
26 KB, and the whole tree is ~190 KB. So there are two ceilings: no single
blob over MAX_BLOB_BYTES, and no more than MAX_TOTAL_BYTES in aggregate. The
second exists because the first cannot see a corpus split into 56 innocent
shards — and splitting a file to dodge a size limit is an ordinary accident,
not an exotic attack. Exceeding either needs a human to add a path to
ALLOWLIST with a reason.

Content matching survives in exactly two roles, neither of which can clear
anything:

  - `describe()` puts a human word to an oversized blob in the failure message
  - `looks_like_corpus_text()` ADDS a refusal for a SMALL file carrying real
    rules or card text — a captured Scryfall response or a pasted CR excerpt
    used as a fixture is a realistic accident the ceiling cannot see

The distinction that matters: a signature miss never means "clean". That
inversion is what defeated all five earlier versions.

Three things smaller than the ceiling are also refused outright: a Git LFS
pointer, whose real bytes live elsewhere and cannot be inspected; a submodule
gitlink, which can name a private repository; and any compressed stream, since
this repository legitimately contains none.

THE RESIDUAL, stated rather than chased. These rules stop the corpus, the
rules dataset, and the obvious ways either arrives in pieces. They do not stop
someone who sets out to defeat them — split the rules text into headerless
fragments below every threshold and no size or signature rule will object. The
guard exists to catch an ACCIDENT: a build output committed by mistake, a
fixture pasted from upstream, a large file split to get past a size limit.
Publication itself is governed by an explicit file allowlist, not by this
scanner, and that is the control that decides what actually ships.

Usage:
  python3 scripts/scan_no_corpus.py --tracked      # the index
  python3 scripts/scan_no_corpus.py --history      # every reachable blob
  python3 scripts/scan_no_corpus.py FILE [FILE...] # explicit paths
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

# The largest legitimate file is ~26 KB. 64 KB leaves room to grow while
# staying under a threshold that matters: the complete Comprehensive Rules,
# every rule and nothing else, gzip to ~200 KB. A 256 KB ceiling let that
# entire dataset through as one innocuous-looking file.
MAX_BLOB_BYTES = 64 * 1024

# A per-blob ceiling alone does not stop a corpus SPLIT INTO PARTS. Gzip the
# corpus to ~14 MB, cut it into 56 pieces of 256 KB, commit them all: every
# piece passes, and a recipient concatenates them back. That is not an exotic
# attack — splitting a file to get around a large-file limit is an ordinary
# thing to do, which is exactly why it needs catching.
#
# So the whole tree is bounded too. The real one is ~168 KB; 2 MB leaves room
# to grow by an order of magnitude while remaining far below any useful
# fraction of a 14 MB compressed corpus.
MAX_TOTAL_BYTES = 2 * 1024 * 1024

# Paths permitted to exceed the PER-BLOB ceiling in the working tree. Every
# entry is a deliberate decision with a reason attached, which is the point:
# growing this list requires someone to look at the file and say why.
#
# It does NOT authorise exceeding the aggregate ceiling — a tree that large
# needs a human decision about the ceiling itself, not an exemption for one
# path. For history, exempt by blob id via ALLOWLIST_BLOBS instead.
ALLOWLIST: dict[str, str] = {
    # 65 KB of pytest, and it was already within a kilobyte of the ceiling
    # before round 24 added the mana_cost floor tests. Looked at: it is
    # assertions and invented fixtures — no rules text, no card text, no
    # captured upstream response. The exemption is for SIZE only; the
    # signature checks above run before this list is consulted, so a real CR
    # excerpt pasted into it would still be refused.
    "tests/test_corpus_automation.py": (
        "test source, reviewed 2026-08-08: assertions and invented fixtures"),
}

# Exemptions that survive history, keyed by the blob's object id. A path can
# be reused, renamed, or reported ambiguously by rev-list; an object id names
# exactly one sequence of bytes that a human looked at. Everything historical
# is judged on this, which is also what makes an exemption usable at all: a
# path-keyed one could never be honoured in history.
ALLOWLIST_BLOBS: dict[str, str] = {
    # tests/test_corpus_automation.py at 067 bytes over the ceiling, as it
    # entered history in 068ad0f. The path-keyed entry above cannot be
    # honoured here by design, and this blob is published: rewriting history
    # to remove it would be a far larger act than the 1,489 bytes that
    # crossed the line. Inspected: pytest assertions and invented fixtures,
    # no rules or card text.
    "eb15a6ab6e4dd595add8c151ca114d44bfa9cf7b": (
        "test source, reviewed 2026-08-08: assertions and invented fixtures"),
}

# A sentinel path that can never be in ALLOWLIST, used to route history's
# verdicts away from path-based exemption.
_HISTORY = "\0<history>"

# --- Diagnostics only. These NEVER clear a file; they only name what an
# --- oversized blob appears to be. Assembled from fragments so that this
# --- file and its tests do not match their own patterns.
SQLITE_MAGIC = b"SQLite" + b" format 3\x00"
SCRYFALL_KEYS = (b'"oracle' + b'_id"', b'"scryfall' + b'_uri"')
CR_MARKER = b"Magic: The Gathering" + b" Comprehensive Rules"
LFS_POINTER = b"version https://git-lfs." + b"github.com/spec/v1"
SNIFF_BYTES = 65536

# A small file's decompressed form is inspected for signatures. Bounded so a
# bomb cannot turn the guard into a denial of service against its own CI.
MAX_PEELED_BYTES = 32 * 1024 * 1024


def describe(blob: bytes) -> str:
    """A human-readable guess at what an oversized blob is. Never a verdict."""
    head = blob[:SNIFF_BYTES]
    if blob.startswith(SQLITE_MAGIC):
        return "looks like a SQLite database"
    if all(k in head for k in SCRYFALL_KEYS):
        return "looks like Scryfall bulk card data"
    if CR_MARKER in head:
        return "looks like Comprehensive Rules text"
    if blob[:2] == b"\x1f\x8b" or blob[:3] == b"BZh" or blob[:6] == b"\xfd7zXZ\x00":
        return "looks like a compressed archive"
    return "unrecognised content"


def is_lfs_pointer(blob: bytes) -> bool:
    """LFS stores the real bytes elsewhere, so a pointer is small and would
    sail under the ceiling while still publishing whatever it references."""
    return blob.startswith(LFS_POINTER)


def _git(*args: str) -> bytes:
    # --no-replace-objects: `git replace` rewires traversal, so a history
    # rehearsal that grafts a clean commit over a bad one makes every scan
    # read the clean graph — while `git push` does not carry refs/replace/*,
    # so the original blob goes to the remote after CI reported clean. The
    # guard must see the object graph that will actually be pushed.
    return subprocess.run(
        ("git", "--no-replace-objects", *args),
        check=True, capture_output=True).stdout


# A CR rule line: "702.19b", "100.1.", "601.2a" at the start of a line. Real
# rules text is thousands of these; ordinary prose and source code are not.
# This catches a rules dump that carries no file header to recognise it by.
# Comprehensive Rules lines come in exactly two shapes: a numbered rule with a
# terminating period ("100.1. Text") or a subrule with a letter ("702.19b
# Text"). Requiring one of those is what separates them from ordinary decimal
# data — "100.25 measurement" is not a rule, and neither is an IP address
# octet, both of which matched earlier versions of this pattern.
_RULE_LINE = re.compile(rb"(?m)^\s*\d{3}\.\d+(?:[a-z][.\s]|\.[ \t])")
_RULE_LINE_THRESHOLD = 20


def _peel(blob: bytes) -> bytes:
    """Best-effort single decompression, for signature purposes only.

    Safe here in a way it never was before: this can only ADD a refusal. If
    the bytes are not compressed, or are compressed in some way not handled,
    nothing is concluded from that — size still governs. The old design's
    fatal move was treating "could not decompress" as "clean".
    """
    import bz2
    import lzma
    import zlib

    # Incremental, with the cap applied DURING decompression. The whole-buffer
    # helpers (gzip.decompress and friends) allocate everything first and check
    # afterwards, so a 64 KB bomb could take CI's memory with it before any
    # limit was consulted — the advertised bound did not exist.
    factories = (
        lambda: zlib.decompressobj(16 + zlib.MAX_WBITS),  # gzip
        lambda: zlib.decompressobj(),                     # raw zlib
        bz2.BZ2Decompressor,
        # memlimit matters: an LZMA header declares its dictionary size and
        # the decompressor allocates that up front, before a single byte of
        # output exists for the cap to measure. A 24-byte stream can ask for a
        # gigabyte. Exceeding the limit raises, which this loop treats as
        # "not this format" — concluding nothing, as always.
        lambda: lzma.LZMADecompressor(memlimit=MAX_PEELED_BYTES),
    )
    for make in factories:
        obj = make()
        out = bytearray()
        try:
            for i in range(0, len(blob), 1 << 16):
                out += obj.decompress(blob[i:i + (1 << 16)],
                                      MAX_PEELED_BYTES - len(out) + 1)
                if len(out) > MAX_PEELED_BYTES:
                    # Too big to inspect. Conclude NOTHING — size still
                    # governs, and this function may only ever add findings.
                    out = bytearray()
                    break
                if len(out) >= SNIFF_BYTES and getattr(obj, "eof", False):
                    break
        except Exception:  # noqa: BLE001 — not this format; conclude nothing
            continue
        if out:
            return bytes(out)
    return blob


def looks_like_corpus_text(blob: bytes) -> str | None:
    """Recognise upstream text in a SMALL file. Additive only.

    The size ceiling stops bulk. It cannot see a 60 KB captured Scryfall
    response or a pasted CR excerpt used as a test fixture — a realistic
    accident on this repository's own development surface, and squarely
    against the stated policy.

    This is the one safe use of content matching: it can only ADD a refusal.
    Five earlier versions of this guard failed because a signature miss was
    read as proof of innocence; nothing here is ever cleared by these checks.
    """
    for candidate in (blob, _peel(blob)):
        head = candidate[:SNIFF_BYTES]
        if all(k in head for k in SCRYFALL_KEYS):
            return "contains Scryfall card data"
        if CR_MARKER in head:
            return "contains Comprehensive Rules text"
        hits = len(_RULE_LINE.findall(head))
        if hits >= _RULE_LINE_THRESHOLD:
            return (f"contains {hits}+ Comprehensive Rules-numbered lines "
                    "in its first 64 KB")
    return None


# gzip, bzip2, xz, zstd, zip. This repository ships source, markdown, JSON and
# YAML — nothing legitimately compressed. Saying so outright closes the
# shard-a-compressed-file route cheaply: the FIRST shard of any split
# compressed stream still carries its magic, and without that shard the rest
# cannot be reconstructed.
_COMPRESSED_MAGIC = (
    (b"\x1f\x8b", "gzip"), (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
    (b"\x28\xb5\x2f\xfd", "zstd"), (b"PK\x03\x04", "zip"),
)


def compressed_kind(blob: bytes) -> str | None:
    for magic, name in _COMPRESSED_MAGIC:
        if blob.startswith(magic):
            return name
    return None


def _verdict(path: str, size: int, blob: bytes | None) -> str | None:
    """The decision point. Size decides; signatures may only add a refusal."""
    if blob is not None and is_lfs_pointer(blob):
        return ("a Git LFS pointer — the bytes it references are not in this "
                "repository and cannot be inspected, so it is refused outright")
    # The signature checks are NOT subject to the allowlist. An entry there
    # says "this file is bigger than the ceiling and that is fine" — it is a
    # judgement about size, made by someone looking at a size. Letting it also
    # wave through recognised rules text would turn a size exemption into a
    # content exemption nobody intended to grant.
    if blob is not None:
        kind = compressed_kind(blob)
        if kind:
            return (f"a {kind} stream. This repository contains no compressed "
                    "files, so one is either upstream data or the leading "
                    "shard of a split archive — and without that shard the "
                    "rest cannot be reassembled.")
        found = looks_like_corpus_text(blob)
        if found:
            return (f"{found}. This repository ships no rules or card text.")
    if size <= MAX_BLOB_BYTES:
        return None
    if path in ALLOWLIST:
        return None
    what = describe(blob) if blob is not None else "not inspected"
    return (f"{size} bytes exceeds the {MAX_BLOB_BYTES}-byte ceiling ({what}). "
            f"If this file is legitimate, add it to ALLOWLIST in "
            f"scripts/scan_no_corpus.py with a reason.")


def _aggregate_verdict(total: int, what: str) -> str | None:
    """The whole tree, not just each file. See MAX_TOTAL_BYTES."""
    if total <= MAX_TOTAL_BYTES:
        return None
    return (f"{what} total {total} bytes, over the {MAX_TOTAL_BYTES}-byte "
            f"aggregate ceiling. A corpus split into under-ceiling pieces "
            f"passes every per-file check; the total is what catches it.")


def scan_tracked() -> list[tuple[str, str]]:
    hits = []
    total = 0
    for raw in _git("ls-files", "-sz").split(b"\0"):
        if not raw:
            continue
        # Parse the index record as BYTES and fetch by the object id it
        # carries. Decoding the path first and then asking for `:<path>` fails
        # for any name that is not valid UTF-8 — and a failed lookup used to
        # be skipped silently, so an oversized blob at such a path was counted
        # in neither the per-file nor the aggregate check. A name we cannot
        # print is still a file we must weigh.
        meta_b, _, name_b = raw.partition(b"\t")
        fields = meta_b.split()
        if len(fields) < 2:
            continue
        mode = fields[0].decode("ascii", "replace")
        oid = fields[1].decode("ascii", "replace")
        name = name_b.decode("utf-8", "replace")
        if mode == "160000":
            # A gitlink names another repository's commit. It is tiny, so no
            # size rule sees it, and it can point at the private repo.
            hits.append((name, "a submodule gitlink — this repository has no "
                               "submodules, and one could reference a private "
                               "repository"))
            continue
        try:
            blob = _git("cat-file", "-p", oid)
        except subprocess.CalledProcessError:
            # Unreadable, therefore unweighable. Refuse rather than skip: a
            # silent skip is exactly how the undecodable-name case slipped
            # past both ceilings.
            hits.append((name, f"object {oid[:12]} could not be read, so it "
                               "cannot be checked; refused rather than skipped"))
            continue
        total += len(blob)
        reason = _verdict(name, len(blob), blob)
        if reason:
            hits.append((name, reason))
    over = _aggregate_verdict(total, "tracked files")
    if over:
        hits.append(("<all tracked files>", over))
    return hits


def scan_history() -> list[tuple[str, str]]:
    """Every blob reachable from any ref.

    `git rev-list --objects` with `--filter` reports each object's size
    directly, so nothing has to be loaded to be judged — which is both far
    faster than reading every blob and immune to a decompression bomb, since
    an oversized object is refused on its recorded size without being touched.
    """
    hits = []
    total = 0
    out = _git("rev-list", "--objects", "--all",
               "--filter=blob:none", "--filter-print-omitted").decode(
                   "utf-8", "replace")
    omitted = {line[1:].split()[0] for line in out.splitlines()
               if line.startswith("~")}

    listing = _git("rev-list", "--objects", "--all").decode("utf-8", "replace")
    paths: dict[str, set[str]] = {}
    for line in listing.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            paths.setdefault(parts[0], set()).add(parts[1])

    for sha in sorted(omitted):
        # `rev-list --objects` prints ONE path per blob, whichever it
        # discovered first — so a blob committed at an unapproved path and
        # later at an allowlisted one may be reported only under the approved
        # name. There is no cheap way to recover every historical path, so
        # history does not honour ALLOWLIST at all: `_HISTORY` below forces
        # every oversized historical blob to be refused regardless of the name
        # it happens to be reported under. An exemption is a statement about a
        # file someone looked at today, not a licence for whatever shared
        # those bytes in the past.
        names = sorted(paths.get(sha, ()))
        name = names[0] if names else f"<unnamed blob {sha[:12]}>"
        try:
            size = int(_git("cat-file", "-s", sha).strip())
        except (subprocess.CalledProcessError, ValueError):
            continue
        approved = sha in ALLOWLIST_BLOBS
        # Oversized blobs are normally not read at all — the recorded size is
        # enough to refuse them. An APPROVED one must still be read, or the
        # size waiver silently becomes a content waiver: with blob=None the
        # compressed, LFS and rules-text signatures never run, so allowlisting
        # an oversized gzip made it disappear from the scan entirely.
        if size <= MAX_BLOB_BYTES or approved:
            blob = _git("cat-file", "-p", sha)
        else:
            blob = None
        effective_size = 0 if approved else size
        reason = _verdict(_HISTORY, effective_size, blob)
        if reason:
            hits.append((f"{name} (blob {sha[:12]})", reason))
    hits += _history_trees()
    return hits


def _reachable_treeish() -> list[str]:
    """Every commit, PLUS any ref that names a tree directly.

    `rev-list --all` walks commits. A ref can point straight at a tree —
    `git update-ref refs/tags/x <tree>` — and such a ref emits no commit, so a
    commit-only walk never weighs that tree at all. Its blobs are still found
    individually by the object scan, but the AGGREGATE ceiling was skipped,
    which is precisely the sharded-corpus shape. `push --mirror` carries the
    ref, so the tree is publishable.

    Annotated tags are peeled with ^{tree}, which also resolves a commit or a
    tag-of-a-tree to the tree it ultimately names.
    """
    seen: list[str] = []
    out = _git("rev-list", "--all").decode().split()
    seen.extend(out)
    refs = _git("for-each-ref", "--format=%(objecttype) %(objectname)").decode()
    for line in refs.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        kind, oid = parts
        if kind == "commit":
            continue  # already covered by rev-list
        try:
            tree = _git("rev-parse", f"{oid}^{{tree}}").decode().strip()
        except subprocess.CalledProcessError:
            continue
        if tree and tree not in seen:
            seen.append(tree)
    return seen


def _history_trees() -> list[tuple[str, str]]:
    """Aggregate-check each reachable TREE, and find gitlinks.

    "Reachable" means every commit's tree plus any tree a ref names directly;
    see _reachable_treeish for why the second is not a hypothetical.

    Not the lifetime sum of every blob version. Summing across history
    conflates "this repository once held too much at one moment" with "this
    repository has been edited a lot": seventy ordinary revisions of a 30 KB
    file exceed 2 MB while no single commit ever held more than 50 KB, and CI
    would start failing on nothing but normal maintenance. The question the
    ceiling is asking is about a tree, so it is asked of each tree.
    """
    hits = []
    for treeish in _reachable_treeish():
        try:
            listing = _git("ls-tree", "-r", "-l", treeish).decode("utf-8", "replace")
        except subprocess.CalledProcessError:
            continue
        # Named generically: a treeish here may be a commit from rev-list or a
        # tree a ref points at directly.
        commit = treeish
        total = 0
        for line in listing.splitlines():
            meta, _, path = line.partition("\t")
            fields = meta.split()
            if len(fields) < 4:
                continue
            mode, _kind, oid, size = fields[0], fields[1], fields[2], fields[3]
            if mode == "160000":
                hits.append((f"{path} (commit {commit[:12]})",
                             "a submodule gitlink in history — it can name a "
                             "private repository, and deleting it at the tip "
                             "does not remove it from the pushed history"))
                continue
            if not size.isdigit():
                continue
            # Exempt blobs still COUNT toward the tree total. An exemption
            # says "this file is not corpus data", not "this file is free" —
            # a tree that large needs a decision about the ceiling itself.
            total += int(size)
        over = _aggregate_verdict(total, f"the tree at {commit[:12]}")
        if over:
            hits.append((f"<tree of {commit[:12]}>", over))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracked", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("paths", nargs="*")
    args = ap.parse_args()

    hits: list[tuple[str, str]] = []
    if args.tracked:
        hits += scan_tracked()
    if args.history:
        hits += scan_history()
    for p in args.paths:
        with open(p, "rb") as fh:
            blob = fh.read()
        reason = _verdict(p, len(blob), blob)
        if reason:
            hits.append((p, reason))

    if not hits:
        return 0
    print("Refused:", file=sys.stderr)
    for name, reason in hits:
        print(f"  {name}\n    ^ {reason}", file=sys.stderr)
    print(
        "\nThis repository does not redistribute rules or card text, and no\n"
        "legitimate file in it is large. Build the corpus locally with\n"
        "scripts/build_corpus.py instead of committing it. If this is history\n"
        "rather than the working tree, removing the file from the tip is not\n"
        "enough — the history has to be rewritten, or the repository replaced.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
