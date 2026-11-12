"""The judge's persona: within Casa's limits, and pinned where it was chosen.

Casa refuses a persona pack whose `# Core` body is outside 300-500
characters, measured on everything under `# Core` up to the next level-1
header, so the nested `## Negative space` counts (`persona_pack.py`). A
component whose persona is refused does not install. The ceiling here is
450, not 500, so that a small edit fails this test before it fails an install.

The pack checksum is computed here the way `test_manifest_digest` computes
the component checksum (RFC 8785 over ASCII keys and strings, which sorted,
separator-tight json.dumps is). `tests/test_casa_loaders.py` runs Casa's own
loader on the pack and is the authority if the two ever disagree.
"""
import hashlib
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "persona/pack"
PERSONA_MD = PACK / "persona.md"
PERSONA_YAML = PACK / "persona.yaml"
PERSONA_MANIFEST = ROOT / "persona/manifest.json"

CORE_MIN, CORE_HEADROOM_MAX = 300, 450


def _sum(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_core_length_as_casa_measures_it():
    text = PERSONA_MD.read_text(encoding="utf-8")
    core = text.split("# Core", 1)[1].split("\n# ", 1)[0].strip()
    assert CORE_MIN <= len(core) <= CORE_HEADROOM_MAX, (
        f"Core body is {len(core)} chars; Casa admits 300-500, this repo "
        f"keeps it at most {CORE_HEADROOM_MAX}")


def test_negative_space_present():
    assert "\n## Negative space" in PERSONA_MD.read_text(encoding="utf-8")


def test_identity():
    y = yaml.safe_load(PERSONA_YAML.read_text(encoding="utf-8"))
    assert y["version"] == "0.2.0"
    assert y["identity"]["pronouns"]["subject"] == "he"
    assert y["traits"] == {"warmth": 3, "formality": 3, "candor": 5,
                           "attunement": 3, "curiosity": 4, "levity": 2,
                           "social_energy": 1, "optimism": 3}


def test_manifest_rows_and_checksum_match_files():
    m = json.loads(PERSONA_MANIFEST.read_text())
    for row in m["files"]:
        assert row["checksum"] == _sum((PACK / row["path"]).read_bytes()), row["path"]
    assert sorted(r["path"] for r in m["files"]) == sorted(
        p.name for p in PACK.iterdir() if p.is_file())
    computed = _sum(json.dumps(
        {"api_version": m["api_version"], "files": m["files"]},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
    assert m["checksum"] == computed

    component = json.loads((ROOT / "manifest.json").read_text())
    y = yaml.safe_load(PERSONA_YAML.read_text(encoding="utf-8"))
    ref = f"{y['id']}@{y['version']}"
    assert component["default_persona"] == {"ref": ref, "checksum": computed}
    dep = next(d for d in component["dependencies"] if d["kind"] == "persona")
    assert dep == {"kind": "persona", "identifier": ref, "digest": computed}
