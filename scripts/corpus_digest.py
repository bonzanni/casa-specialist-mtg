#!/usr/bin/env python3
"""Print the `sha256:` digest casa pins for a corpus directory.

This mirrors casa's content-checksum format: a sha256 over
length-framed entry lines, one per path in the tree, sorted by path. It is
NOT the hash of the release tarball — that only proves the download arrived
intact. It is the digest casa computes for a directory tree, kept here because the
build tooling still needs to report one for a corpus directory. Note that the
component no longer declares a corpus/data dependency: the corpus is installed
after the fact by the setup tool, which pins the ARCHIVE's sha256 instead.

Usage:
  python3 scripts/corpus_digest.py plugins/mtg/data
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
from pathlib import Path

# casa excludes its own artifact metadata file from the hash.
METADATA_FILENAME = ".casa-artifact.json"


def _entry_line(rel: str, etype: str, exec_bit: int, payload: str) -> bytes:
    # Length-framed over UTF-8 BYTES (not str chars — multibyte paths would
    # otherwise produce ambiguous frames).
    body = f"{rel}\x00{etype}\x00{exec_bit}\x00{payload}".encode("utf-8")
    return str(len(body)).encode("ascii") + b":" + body


def content_checksum(root: Path) -> str:
    root = Path(root)
    lines: list[bytes] = []
    entries = sorted(
        p for p in root.rglob("*")
        if p.relative_to(root).as_posix() != METADATA_FILENAME
    )
    for p in entries:
        rel = p.relative_to(root).as_posix()
        st = p.lstat()
        exec_bit = 1 if (st.st_mode & stat.S_IXUSR) else 0
        if stat.S_ISLNK(st.st_mode):
            lines.append(_entry_line(rel, "l", 0, os.readlink(p)))
        elif stat.S_ISDIR(st.st_mode):
            lines.append(_entry_line(rel, "d", 0, ""))
        elif stat.S_ISREG(st.st_mode):
            h = hashlib.sha256(p.read_bytes()).hexdigest()
            lines.append(_entry_line(rel, "f", exec_bit, h))
        else:
            raise ValueError(f"special file in artifact: {rel}")
    return hashlib.sha256(b"".join(lines)).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 1
    print("sha256:" + content_checksum(root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
