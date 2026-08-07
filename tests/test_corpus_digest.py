"""The corpus digest must stay bit-compatible with casa's content_checksum.

casa refuses to install a component whose corpus dependency does not hash to
the digest pinned in manifest.json. If this reimplementation drifts — a
changed frame format, a dropped entry type, a sort-order difference — every
release would publish a digest casa rejects, and the failure would surface
at install time in someone else's deployment rather than here.

GOLDEN was produced by running casa's own content_checksum implementation
over the fixture below. It covers a regular file, a nested file, the
executable bit, and a symlink — the four entry shapes the format
distinguishes.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from corpus_digest import content_checksum  # noqa: E402

GOLDEN = "9ca092736bab86b711670872ac7c0355a3b6c8bc56fab75321d5f3da98a44c2e"


@pytest.fixture()
def tree(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "sub" / "b.bin").write_bytes(b"beta")
    run = tmp_path / "run.sh"
    run.write_bytes(b"exe")
    run.chmod(0o755)
    (tmp_path / "link.txt").symlink_to("a.txt")
    return tmp_path


def test_matches_casa_golden_vector(tree):
    assert content_checksum(tree) == GOLDEN


def test_exec_bit_changes_the_digest(tree):
    before = content_checksum(tree)
    (tree / "a.txt").chmod(0o755)
    assert content_checksum(tree) != before


def test_symlink_target_changes_the_digest(tree):
    before = content_checksum(tree)
    link = tree / "link.txt"
    link.unlink()
    link.symlink_to("sub/b.bin")
    assert content_checksum(tree) != before


def test_metadata_file_is_excluded(tree):
    before = content_checksum(tree)
    (tree / ".casa-artifact.json").write_text("{}")
    assert content_checksum(tree) == before


def test_content_change_changes_the_digest(tree):
    before = content_checksum(tree)
    (tree / "a.txt").write_bytes(b"alphb")
    assert content_checksum(tree) != before


def test_unicode_normalizations_of_a_name_are_distinct_entries(tmp_path):
    """The frame length counts UTF-8 BYTES, not str characters, so two names
    that render as the same glyph but differ in code points are distinct
    entries.

    Written with explicit escapes on purpose: the difference between these
    two strings is invisible in a source file, and a test whose outcome
    depends on an invisible character is a test nobody can review.
    """
    precomposed = "caf\u00e9"      # e-acute as one code point  -> 5 UTF-8 bytes
    decomposed = "cafe\u0301"      # e + combining acute        -> 6 UTF-8 bytes
    assert precomposed != decomposed

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / precomposed).write_bytes(b"")
    (b / decomposed).write_bytes(b"")
    assert content_checksum(a) != content_checksum(b)
