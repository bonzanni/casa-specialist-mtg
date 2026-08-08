"""manifest.json's pinned plugin digest must match the tree git actually ships.

Casa refuses to install a component whose bundled plugin tree does not hash to
the pinned digest. That digest has now gone stale three times in this
repository's short life — every edit to anything under plugins/mtg/ changes it,
including a comment — and each time it was noticed by a reviewer rather than by
the repository itself. Noticing it here costs a second.

The digest is computed over the TRACKED files only, with the corpus removed.
Hashing the working directory would produce a value that is right on a machine
where a corpus has been built and wrong everywhere else — and in the private
repository, which still tracks the corpus by design, it would be wrong even
there.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from corpus_digest import content_checksum  # noqa: E402

PLUGIN_DIR = "plugins/mtg"


def _tracked_plugin_tree(dest: Path) -> Path:
    """Materialise plugins/mtg as git would ship it."""
    # `-s` carries the mode, which matters: casa's checksum includes the
    # executable bit and distinguishes symlinks, so materialising everything
    # as a plain 0644 file would compute the right digest today and quietly
    # the wrong one the first time a script becomes executable.
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-sz", PLUGIN_DIR],
        check=True, capture_output=True).stdout
    entries = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        meta, _, name = raw.decode().partition("\t")
        mode = meta.split()[0]
        entries.append((mode, name))
    if not entries:
        pytest.skip("plugins/mtg is not tracked here")
    for mode, name in entries:
        blob = subprocess.run(
            ["git", "-C", str(ROOT), "show", f":{name}"],
            check=True, capture_output=True).stdout
        target = dest / Path(name).relative_to(PLUGIN_DIR)
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "120000":
            target.symlink_to(blob.decode())
            continue
        target.write_bytes(blob)
        if mode == "100755":
            target.chmod(0o755)
    return dest


def test_pinned_plugin_digest_matches_the_shipped_tree(tmp_path):
    manifest = json.loads((ROOT / "manifest.json").read_text())
    pinned = next(d["digest"] for d in manifest["dependencies"]
                  if d["kind"] == "plugin/implementation")

    tree = _tracked_plugin_tree(tmp_path / "mtg")
    # The corpus never ships, so it is not part of what casa hashes.
    for stale in tree.rglob("corpus.sqlite*"):
        stale.unlink()
    data = tree / "data"
    if data.is_dir() and not any(data.iterdir()):
        data.rmdir()  # git preserves no empty directories

    actual = "sha256:" + content_checksum(tree)
    assert actual == pinned, (
        f"manifest.json pins {pinned} but the tracked plugin tree hashes to "
        f"{actual}. Casa would reject this component. Recompute with:\n"
        f"  python3 scripts/corpus_digest.py <tracked plugins/mtg>")


def test_no_corpus_dependency_is_declared():
    """The corpus arrives after install, via setup_corpus. If it reappears as a
    dependency, casa will demand a corpus/ directory this repository does not
    and must not contain, and every install fails as dependency_unavailable."""
    manifest = json.loads((ROOT / "manifest.json").read_text())
    kinds = [d["kind"] for d in manifest["dependencies"]]
    assert "corpus/data" not in kinds


def test_the_component_checksum_matches_the_role_and_config_files():
    """manifest.checksum covers exactly role/role.yaml, role/doctrine.md and
    config-schema.json. Casa refuses to load a component whose checksum does
    not match, so editing the role — max_turns, tool grants, the doctrine —
    silently invalidates the manifest unless this is recomputed. The plugin
    digest already had a drift test; this one did not, and role.yaml is the
    file most likely to be tuned in a hurry.

    Computed HERE rather than by importing Casa. This test used to insert an
    absolute path into a private checkout and skip on ImportError, which
    meant that in the public repository — the only place this file actually
    ships from — it never ran at all, and reported a pass while doing so.
    The canonicalisation is RFC 8785 over a document of ASCII keys and
    strings with no numbers, for which sorted, separator-tight, non-escaping
    json.dumps is exactly JCS. Casa's own implementation is still consulted
    when it happens to be present, below, so the local shortcut cannot drift
    away from the thing it stands in for.
    """
    def _sum(data: bytes) -> str:
        return "sha256:" + hashlib.sha256(data).hexdigest()

    files = {n: (ROOT / n).read_bytes() for n in
             ("role/role.yaml", "role/doctrine.md", "config-schema.json")}
    rows = [{"path": n, "checksum": _sum(files[n])} for n in sorted(files)]
    document = {"api_version": "casa.specialist-component.manifest/v1",
                "files": rows}
    computed = _sum(json.dumps(
        document, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8"))

    pinned = json.loads((ROOT / "manifest.json").read_text())["checksum"]
    assert computed == pinned, (
        f"manifest.json pins {pinned} but role/config hash to {computed}. "
        "Casa would refuse to load this component.")

    # Cross-check against Casa itself where the source is on hand. A
    # disagreement here means the shortcut above stopped being equivalent,
    # which is the only way this test could pass while the component still
    # failed to load.
    casa = Path("/home/nicola/Projects/ha-casa-app/casa/rootfs/opt/casa")
    if not casa.is_dir():
        return
    import sys as _sys
    _sys.path.insert(0, str(casa))
    try:
        from canonical_bytes import canonical_json_bytes, checksum_bytes
    except ImportError:
        return  # rfc8785 not installed; the local computation still ran
    theirs = checksum_bytes(canonical_json_bytes(
        {"api_version": "casa.specialist-component.manifest/v1",
         "files": [{"path": n, "checksum": checksum_bytes(files[n])}
                   for n in sorted(files)]}))
    assert theirs == computed, (
        f"casa computes {theirs}, this test computes {computed} — the local "
        "canonicalisation is no longer equivalent to casa's")
