"""plugin.json's casa.resultContract must cover exactly the server's tools.

Casa 0.290.0 refuses, before it runs, any non-setup tool of a plugin that is
absent from `casa.resultContract.tools`, and refuses the whole install when the
declaration is malformed (an unknown member, a wrong version). Neither failure
is visible from this repository's own suite: the server answers tools/list
happily either way. So the declaration is pinned here against the one thing it
must match — `mtg_server.TOOLS`, the table tools/list is generated from.
"""
import json

from plugins.mtg.server import mtg_server

from tests.test_manifest_digest import ROOT

PLUGIN_JSON = ROOT / "plugins/mtg/.claude-plugin/plugin.json"


def _casa() -> dict:
    return json.loads(PLUGIN_JSON.read_text())["casa"]


def test_contract_has_exactly_the_members_casa_accepts():
    contract = _casa()["resultContract"]
    assert set(contract) == {"version", "tools"}
    assert contract["version"] == 1


def test_contract_covers_exactly_the_non_setup_tools():
    casa = _casa()
    declared = set(casa["resultContract"]["tools"])
    served = set(mtg_server.TOOLS) - {casa["setupTool"]}
    assert declared == served, (
        f"declared but not served: {sorted(declared - served)}; "
        f"served but undeclared (casa refuses these): {sorted(served - declared)}")


def test_every_tool_is_declared_safe_and_open_to_guests_with_no_other_member():
    """Casa 0.372.0: `"guest": true` opens a tool to a guest run (a friend's
    summons in a group the operator allowed). The five lookups return corpus
    text only, so all five are open; the literal true is the only accepted
    value, and an older casa refuses the member outright (README's floor)."""
    for name, entry in _casa()["resultContract"]["tools"].items():
        assert entry == {"result": "safe", "guest": True}, name


def test_the_setup_tool_is_not_listed():
    casa = _casa()
    assert casa["setupTool"] not in casa["resultContract"]["tools"]
