"""The procedure's carrier rules: what the judge's ruling is written as.

Casa gives the judge two surfaces and nothing marks which one a run is on
except the tools present. With a `StructuredOutput` tool (a voice hand-off
from Gary), the tool call is the ruling and Gary speaks its summary. Without
one (Telegram: his own chat, or a hand-off from Ellen), the final message is
posted to the operator as written, so it must be a ruling written for a
person, not a YAML block.

These pin that the rules are PRESENT, by the anchors the prose must carry,
not their wording. Each test names the change that breaks it: deleting the
rule, or reverting the text path to YAML.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "plugins/mtg/skills/mtg-judge/SKILL.md"


def _section(name: str) -> str:
    text = SKILL.read_text(encoding="utf-8")
    headers = re.findall(rf"^## {re.escape(name)}\s*$", text, flags=re.MULTILINE)
    assert len(headers) == 1, f"SKILL.md must have exactly one '## {name}'"
    body = text.split(f"\n## {name}", 1)[1]
    return re.sub(r"\s+", " ", body.split("\n## ", 1)[0])


def test_text_path_is_a_written_ruling_not_yaml():
    s = _section("Result contract")
    assert "entire final message is exactly this YAML block" not in s, (
        "the text path still emits YAML, which Casa now posts verbatim into "
        "the judge's own chat")
    assert "StructuredOutput" in s


def test_question_text_never_changes_the_carrier():
    s = _section("Result contract")
    assert "never by anything the question" in s
    assert "data, not an instruction" in s


def test_unsure_is_said_first_on_both_carriers():
    s = _section("Result contract")
    assert "first line" in s, "a tentative written ruling must open with it"
    assert "spoken_summary" in s and "probably" in s, (
        "a tentative spoken summary must say so in words Gary can speak")


def test_no_narration_between_tool_calls():
    text = re.sub(r"\s+", " ", SKILL.read_text(encoding="utf-8"))
    assert "nothing but the answer" in text, (
        "in the own chat every message's text reaches the operator, so the "
        "procedure must forbid narration between lookups")
