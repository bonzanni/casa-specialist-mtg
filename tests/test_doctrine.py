"""The doctrine says what Casa serves, and says it where Casa serves it.

Casa gives a delegated or own-chat specialist the TEXT projection only:
Core doctrine plus `## Text projection`, with the other projection subtrees
cut out (`tools.py` `projection_for(..., channel="text")`). Text under
`## Voice projection` or `## Restricted webhook projection` reaches no model,
so a rule placed there is a rule nobody follows. These tests fail when an
operative rule drifts back into an unserved section, when the voice rules
leave the served part, or when the doctrine again claims the turn ceiling is
silent (Casa types it now).
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCTRINE = ROOT / "role/doctrine.md"
ROLE = ROOT / "role/role.yaml"

UNSERVED_MAX_CHARS = 120


def _text() -> str:
    return DOCTRINE.read_text(encoding="utf-8")


def _section(title: str) -> str:
    """Body of `## <title>` up to the next header of any level; '' if absent."""
    parts = re.split(rf"^## {re.escape(title)}\s*$", _text(), flags=re.MULTILINE)
    if len(parts) == 1:
        return ""
    assert len(parts) == 2, f"doctrine has more than one '## {title}'"
    return re.split(r"^#{1,6} ", parts[1], maxsplit=1, flags=re.MULTILINE)[0]


def _core() -> str:
    body = re.split(r"^# Core doctrine\s*$", _text(), flags=re.MULTILINE)
    assert len(body) == 2, "doctrine must have exactly one '# Core doctrine'"
    return re.split(r"^#{1,6} ", body[1], maxsplit=1, flags=re.MULTILINE)[0]


def test_nothing_operative_lives_in_unserved_projections():
    for title in ("Voice projection", "Restricted webhook projection"):
        body = _section(title).strip()
        assert len(body) <= UNSERVED_MAX_CHARS, (
            f"'## {title}' holds {len(body)} chars that no model is served")


def test_voice_rules_are_in_the_served_part():
    served = re.sub(r"\s+", " ", _core() + _section("Text projection"))
    assert "StructuredOutput" in served and "spoken_summary" in served


def test_doctrine_no_longer_claims_silent_truncation():
    d = re.sub(r"\s+", " ", _text())
    assert "Nothing downstream marks it" not in d
    assert "handed to the caller as an ordinary answer" not in d


def test_own_chat_texts_fit_casas_schema():
    oc = yaml.safe_load(ROLE.read_text(encoding="utf-8"))["own_chat"]
    assert 1 <= len(oc["short_description"]) <= 120
    assert 1 <= len(oc["description"]) <= 512
