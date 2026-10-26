"""A turn's leading plugin command is parsed in one place (`harness.invocations`) and
handed to the claiming runner beside the prompt, so no turn executor re-parses
Claude's slash-command syntax."""
from __future__ import annotations

import pytest

from apps.harness import invocations
from apps.harness.dispatch_marker import stamp_dispatched


def test_a_plugin_command_is_read_off_the_first_line():
    inv = invocations.parse("/ace:ask --thread 7")
    assert (inv.plugin, inv.skill, inv.args, inv.rest) == ("ace", "ask", "--thread 7", "")
    assert inv.name == "ace:ask"


def test_what_follows_the_command_is_carried_as_it_is():
    prompt = stamp_dispatched("/echo:turn", sender="ada")
    inv = invocations.parse(prompt)
    assert inv.name == "echo:turn" and inv.args == ""
    assert "ada" in inv.rest


@pytest.mark.parametrize("prompt", ["", "draft the post", "/help", "/ace", "please /ace:ask",
                                    "/ace:ask x"])
def test_plain_words_are_no_command(prompt):
    assert invocations.parse(prompt) is None


def test_command_builds_the_canonical_text():
    assert invocations.command("echo", "turn") == "/echo:turn"
    assert invocations.command("echo", "turn", " --thread 9 ") == "/echo:turn --thread 9"
    with pytest.raises(ValueError):
        invocations.command("bad name", "turn")


@pytest.mark.django_db
def test_the_claim_carries_the_parsed_command():
    from types import SimpleNamespace

    from apps.harness.schemas import ClaimedTurnOut

    out = ClaimedTurnOut.resolve_invocation(SimpleNamespace(prompt="/hal:turn --scope ci"))
    assert (out.plugin, out.skill, out.args) == ("hal", "turn", "--scope ci")
    assert ClaimedTurnOut.resolve_invocation(SimpleNamespace(prompt="hello")) is None
