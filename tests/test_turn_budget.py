"""The skill's turn-budget stop rule must exist, be singular, and fit the role.

A run that hits the role's `max_turns` ceiling loses its ruling: on a
delegated run Casa reports only that the specialist stopped, and in the
judge's own chat whatever text had been written is sent as it stands. Issue
#2 first met this as silent truncation, which Casa has since typed. The
countermeasure is procedural: SKILL.md carries one operative
imperative telling the model to stop gathering after a fixed number of
tool-calling messages and emit a degraded-but-typed contract instead. That
sentence is prose, so nothing structural fails when it is weakened or
deleted; this test is the thing that fails.

Pinned here, against the ways this guard has been defeated:

- Non-binding copies are refused by SHAPE, not by enumeration. Stripping
  code blocks kept missing one more syntax (backtick fences, then tilde
  fences), so the mechanism was cut: nothing is
  stripped. The header count runs over the RAW file, where a fenced decoy
  section ADDS a '## Turn budget' line and fails the exactly-one check
  closed; and the section itself must contain no code-block syntax at all
  (backtick or tilde fence, or a ≥4-space-indented line) — the stop rule
  is one flat prose paragraph, examples belong elsewhere.
- Raw HTML is refused for the whole FILE, by shape: '<' followed by a
  letter, '!', '/' or '?' — the entire section can hide in an HTML
  comment, which renders as nothing and binds nobody, and a comment can
  be closed after a dummy header so no section-scoped ban catches every
  placement; a processing instruction does the same. Those four characters are ALL the ways
  CommonMark raw HTML can open after '<' — tag, comment/declaration/
  CDATA, closer, processing instruction — so the shape is spec-complete.
  SKILL.md is markdown prose, lists and fences; the first legitimate HTML
  tag in it can amend this line consciously.
- Brackets are refused in the section (the canonical sentence can be
  hidden in a link-reference definition's TITLE, which
  never renders — and an inline link's title has the same property). Every
  markdown construct that can carry non-rendered text inside otherwise
  normal prose starts with '[', so the section — one flat, link-free
  paragraph — refuses the character rather than enumerating link forms.
  With that, the ban list is closed: CommonMark's only other constructs
  whose raw text renders as nothing are code blocks and raw HTML, both
  refused above. What remains (blockquotes, lists, headings, emphasis)
  RENDERS its text; and the guard's threat is drift and careless
  weakening — an operator determined to deceive their own guard can edit
  the guard itself, which no test defends against.
- The SECTION must exist exactly once, and its ENTIRE normalized text is
  pinned to the approved copy below: matching only the
  imperative sentence let surrounding prose negate it — "This rule is
  obsolete and must not be followed: after your 24th …" passed every
  check, and deprecating in prose while forgetting to delete is exactly
  how documents drift. Equality has no such gap: ANY change to the
  section — negation, addition, rewording, decoy — is a conscious edit
  of the pinned copy HERE in the same commit.
- The syntax bans stay alongside the pin, because normalization collapses
  whitespace: a 4-space-indented (code-block) copy of the section body
  would normalize EQUAL to the prose it no longer renders as. And NO
  fence line may precede the header at all:
  an unclosed fence in an earlier section swallows this one into a code
  block without changing a byte of it, and mere fence-line parity is
  beatable — a ``` line carrying an info string inside an open fence is
  content, not a closer, so four fence lines can still leave one open.
  Refusing every fence before the header needs no CommonMark modeling
  and fails closed; fences after the section cannot affect it.
- `max_turns` is read from parsed YAML at `tools.max_turns`, not grepped:
  a stray "max_turns: 30" inside some block scalar must not out-vote the
  operative ceiling.
- The constant is checked floor and ceiling: it must clear the measured
  legitimate 16-turn run (issue #2, attempt 2 — a stop that fires inside
  it would degrade a question the component is known to answer), and it
  must leave ≥5 turns of emission headroom (emit + the StructuredOutput
  validation retry the contract prescribes), so editing either number
  alone fails loudly.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "plugins/mtg/skills/mtg-judge/SKILL.md"
ROLE = ROOT / "role/role.yaml"

# The approved section, pinned in full (normalized: '*' removed, whitespace
# collapsed). Changing the instruction in SKILL.md is allowed — by changing
# it here in the same commit, consciously.
CANONICAL_SECTION = (
    "Every message in which you call tools spends one turn, and your own "
    "transcript shows each one you have spent, so the budget is observable. "
    "Running out costs the ruling: on a delegated run the answer is lost and "
    "the person is told only that you stopped; in your own chat whatever you "
    "had written is sent as it stands. So after your 24th tool-calling "
    "message, stop gathering and emit the result on the very next message — "
    "`tentative`, naming exactly what was not checked, or "
    "`needs_clarification` if identification is what failed. 24 clears every "
    "legitimate run yet measured with room for emission and the validation "
    "retry. A result that names its gaps is an answer; a ruling lost at the "
    "ceiling is not.")

IMPERATIVE = re.compile(
    r"after your (\d+)(?:st|nd|rd|th) tool-calling message, "
    r"stop gathering and emit the result on the very next message "
    r"— `tentative`, naming exactly what was not checked, "
    r"or `needs_clarification` if identification is what failed\.")

# The measured legitimate run (issue #2, attempt 2) took 16 turns total;
# the gathering budget must clear it.
MEASURED_LEGITIMATE_TURNS = 16
# Emission + the structured-output retry the contract prescribes, with slack.
EMISSION_HEADROOM_TURNS = 5


def _turn_budget_section() -> str:
    """The section body, refused rather than sanitised when decoys appear.

    Counting headers on the RAW text means a copy of the section inside any
    code block still adds a '## Turn budget' line and fails the exactly-one
    assertion — closed, demanding a conscious edit — instead of being
    stripped by syntax rules that kept proving incomplete.
    """
    text = SKILL.read_text(encoding="utf-8")
    prefix = text.split("\n## Turn budget", 1)[0]
    fence = re.search(r"^\s{0,3}(```|~~~)", prefix, flags=re.MULTILINE)
    assert fence is None, (
        "SKILL.md has a code fence BEFORE the Turn budget section — an "
        "unclosed one swallows the section into a code block without "
        "changing a byte of it, and whether a given fence line opens or "
        "closes is not decidable by counting, so none is allowed there")
    html = re.search(r"<[A-Za-z!/?]", text)
    assert html is None, (
        f"SKILL.md contains raw HTML at offset {html.start()} "
        f"({text[html.start():html.start() + 20]!r}) — an HTML comment or "
        "container can render an instruction non-binding while every "
        "text-level check still sees it, so none is allowed anywhere")
    headers = [h for h in re.findall(r"^## .*$", text, flags=re.MULTILINE)
               if h.strip() == "## Turn budget"]
    assert len(headers) == 1, (
        f"SKILL.md must contain exactly one '## Turn budget' line, found "
        f"{len(headers)} — a copy anywhere, fenced or not, is a decoy this "
        "guard refuses to disambiguate")
    body = text.split("\n## Turn budget", 1)[1]
    body = body.split("\n## ", 1)[0]
    # Together with the file-wide raw-HTML refusal above, this closes every
    # CommonMark construct whose raw text renders as nothing: code blocks,
    # and (via '[') link/image forms, whose TITLES never render. The rest
    # of markdown displays what it contains.
    # Tabs are banned outright rather than modeling CommonMark's tab-stop
    # arithmetic: a leading tab makes a code block that space-collapsing
    # normalization cannot see, and the pinned prose
    # needs no tab anywhere.
    for syntax, pattern in (("a backtick fence", r"```"),
                            ("a tilde fence", r"~~~"),
                            ("an indented code line", r"^ {4,}\S"),
                            ("a tab character", r"\t"),
                            ("a bracketed link form", r"\[")):
        assert not re.search(pattern, body, flags=re.MULTILINE), (
            f"the Turn budget section contains {syntax}; the stop rule is "
            "one flat prose paragraph — code examples make an instruction "
            "and its decoy indistinguishable, put them elsewhere")
    return re.sub(r"\s+", " ", body.replace("*", ""))


def _role_max_turns() -> int:
    role = yaml.safe_load(ROLE.read_text(encoding="utf-8"))
    ceiling = role["tools"]["max_turns"]
    assert isinstance(ceiling, int), (
        f"role.yaml tools.max_turns is {ceiling!r}, not an integer")
    return ceiling


def test_the_section_is_exactly_the_approved_text():
    section = _turn_budget_section().strip()
    assert section == CANONICAL_SECTION, (
        "the Turn budget section no longer equals its approved text — "
        "any change, including prose AROUND the stop rule that could "
        "negate or deprecate it, is made by updating CANONICAL_SECTION "
        "here in the same commit.\n"
        f"section:   {section!r}\n"
        f"canonical: {CANONICAL_SECTION!r}")
    hits = IMPERATIVE.findall(section)
    assert len(hits) == 1, (
        "CANONICAL_SECTION itself no longer contains exactly one operative "
        f"imperative (found {len(hits)}) — the pin was updated to a text "
        "that is not a stop rule")


def test_the_stop_rule_names_both_degraded_statuses():
    section = _turn_budget_section()
    for status in ("tentative", "needs_clarification"):
        assert f"`{status}`" in section, (
            f"the Turn budget section no longer names `{status}` — a stop "
            "rule that does not say what to emit is not a stop rule")


def test_the_constant_fits_the_role_ceiling():
    section = _turn_budget_section()
    stop = int(IMPERATIVE.search(section).group(1))
    max_turns = _role_max_turns()
    assert stop > MEASURED_LEGITIMATE_TURNS, (
        f"gathering budget {stop} would fire inside the measured legitimate "
        f"{MEASURED_LEGITIMATE_TURNS}-turn run (issue #2, attempt 2)")
    assert stop <= max_turns - EMISSION_HEADROOM_TURNS, (
        f"gathering budget {stop} leaves fewer than "
        f"{EMISSION_HEADROOM_TURNS} of role.yaml's max_turns={max_turns} "
        "for emission and the structured-output retry — one of the two "
        "numbers was edited without the other")
