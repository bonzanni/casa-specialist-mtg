"""Casa's own code judges the component, as an install would.

Every other test here checks a copy of a Casa rule. This one runs Casa's
loaders on the tree Casa would fetch: component and role schema, the
untrusted-bytes scan, the dependency closure (persona and plugin digests),
and the persona pack. It also asks Casa's prompt compiler what the judge is
actually served on the text surface, the only one a delegated or own-chat
specialist gets, so a rule that drifts out of the served doctrine fails here.
"""
import subprocess

import pytest

from casa_checkout import ROOT, casa_modules, casa_root


def _tracked_tree(dest):
    """The git INDEX as files: what a commit of this tree would ship."""
    tree = subprocess.run(["git", "-C", str(ROOT), "write-tree"], check=True,
                          capture_output=True, text=True).stdout.strip()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", tree],
                             check=True, capture_output=True).stdout
    dest.mkdir()
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive, check=True)
    return dest


def test_a_set_but_wrong_casa_root_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("CASA_ROOT", str(tmp_path))
    with pytest.raises(AssertionError):
        casa_root()


def test_casa_accepts_the_component(tmp_path):
    m = casa_modules()
    tree = _tracked_tree(tmp_path / "mtg")
    component = m["specialist_component"].load_specialist_component(
        tree, tree / "manifest.json")
    m["specialist_install"]._validate_untrusted_bytes(component)
    deps = m["specialist_install"].resolve_dependency_closure(component, tree)
    assert all(d.available for d in deps), [
        (d.identifier, d.detail) for d in deps if not d.available]
    m["persona_pack"].load_persona_pack(
        tree / "persona/pack", tree / "persona/manifest.json")


def test_the_served_text_doctrine_carries_both_carriers():
    m = casa_modules()
    doctrine = (ROOT / "role/doctrine.md").read_text(encoding="utf-8")
    served = m["prompt_compiler"].doctrine_for_surface(doctrine, "text")
    for anchor in ("StructuredOutput", "spoken_summary", "written ruling",
                   "Write nothing but the answer"):
        assert anchor in " ".join(served.split()), anchor


def test_the_served_guest_doctrine_is_core_plus_guest_projection_only():
    """Casa 0.372.0 compiles a fourth surface, `guest`, for a friend's summons:
    Core doctrine and `## Guest projection`, the Text projection cut out. The
    own-chat and Ellen rules must therefore NOT reach a guest, and the guest
    rules must."""
    m = casa_modules()
    doctrine = (ROOT / "role/doctrine.md").read_text(encoding="utf-8")
    served = " ".join(m["prompt_compiler"].doctrine_for_surface(doctrine, "guest").split())
    for anchor in ("Invoke the mtg-judge procedure", "not in the corpus",
                   "name the two readings"):
        assert anchor in served, anchor
    for excluded in ("Ellen", "your own chat", "answer_shown", "Gary"):
        assert excluded not in served, excluded

