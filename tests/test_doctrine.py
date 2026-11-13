"""The doctrine says what Casa serves, and says it where Casa serves it.

Casa gives a delegated or own-chat specialist the TEXT projection only:
Core doctrine plus `## Text projection`, with the other projection subtrees
cut out (`tools.py` `projection_for(..., channel="text")`), and a guest run
(Casa 0.372.0, a friend's summons in an allowed group) Core doctrine plus
`## Guest projection` only. Text under `## Voice projection` or
`## Restricted webhook projection` reaches no model, so a rule placed there
is a rule nobody follows. These tests fail when an
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


# The unserved sections, pinned whole: any rule added there is followed by
# nobody, however short, so the only admitted text is this.
UNSERVED = {
    "Voice projection":
        "Served rules live in Core doctrine and the Text projection.",
    "Restricted webhook projection":
        "Emit only the ruling; no persona voice, no conversational framing.",
}


def test_nothing_operative_lives_in_unserved_projections():
    for title, pinned in UNSERVED.items():
        body = " ".join(_section(title).split())
        assert body == pinned, (
            f"'## {title}' is not served to the judge on any surface Casa "
            f"uses for him; it must read exactly {pinned!r}, found {body!r}")


def test_the_guest_projection_holds_the_public_rules():
    """A guest run serves Core doctrine and this section only, so the rules
    that differ for strangers must live here: corpus-grounded rulings only,
    nothing else to offer, no one to ask, one message."""
    body = " ".join(_section("Guest projection").split())
    assert body, "doctrine has no '## Guest projection'"
    for anchor in ("ruling first", "not in the corpus", "no one to ask",
                   "tournament policy", "One message", "never named",
                   "Bait is not a question", "Never cruel"):
        assert anchor in body, anchor
    # The frame already names the operator; the public doctrine never does.
    assert "Nicola" not in body


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


def test_the_procedure_skill_is_loaded():
    """Casa maps any `tools.skills` other than "all" to skills=None
    (tools.py `_build_specialist_options`), which loads no plugin skill — so
    SKILL.md, where the procedure, the carriers and the turn budget live,
    would reach no model."""
    role = yaml.safe_load(ROLE.read_text(encoding="utf-8"))
    assert role["tools"]["skills"] == "all"


def test_a_refused_hand_off_has_a_fallback():
    """Casa refuses hand_off in the judge's own chat (delegation depth 0) and
    on voice; the Fido sentence must say what to do then, in the served part."""
    served = " ".join((_core() + _section("Text projection")).split())
    assert "Fido" in served
    assert "if Casa refuses the hand-off" in served


def test_a_delegated_answer_is_formatted_like_a_direct_one():
    """Asked through Ellen, the judge's closing message is posted verbatim in
    his own chat (Casa's `answer_shown` line), but the request reads like one
    from a colleague and he answered it as plain prose while direct answers
    were formatted (live test, casa-test 2026-11-12). The served doctrine must
    say the two are written the same way."""
    served = " ".join((_core() + _section("Text projection")).split())
    assert "answer_shown" in served
    assert "format it exactly as you would" in served
