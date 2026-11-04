"""`access.may_edit_agent` — the one predicate for the `agent_edit` permission.

The owner, an admin and the agent's own login always may; anyone else only through an access-list
line whose role holds `agent_edit` (`editor`). A workspace editor with no such line may not, and a
read_write / read_only line is never enough. `agent_edit_check` lists who the switch would cost.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command

from apps.agents import access, agent_edit_check, roles
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.agents.standings import Standings
from apps.contacts.models import Contact
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M, G = WorkspaceMembership, AgentGrant


@pytest.fixture()
def w():
    mk = get_user_model().objects.create_user
    names = ("owner", "boss", "adm", "line_ed", "line_rw", "line_ro", "ws_ed", "ws_ed2", "viewer", "out", "bot")
    u = {n: mk(n, f"{n}@dimagi.com", "pw") for n in names}
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u["boss"])
    M.objects.create(user=u["boss"], workspace=ws, role=M.OWNER)
    for n in ("owner", "adm", "line_ed", "line_rw", "line_ro", "ws_ed", "ws_ed2", "bot"):
        M.objects.create(user=u[n], workspace=ws, role=M.EDITOR)
    M.objects.create(user=u["viewer"], workspace=ws, role=M.VIEWER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=u["owner"], user=u["bot"])
    AgentAdmin.objects.create(agent=agent, user=u["adm"], granted_by=u["owner"])
    for n, role in (("line_ed", roles.EDITOR), ("line_rw", roles.READ_WRITE), ("line_ro", roles.READ_ONLY)):
        G.objects.create(agent=agent, kind=G.PERSON, user=u[n], role=role)
    return {"ws": ws, "agent": agent, **u}


def _default(w, role):
    """The workspace's default line: every editor of it gets `role` on every agent in it."""
    return G.objects.create(workspace=w["ws"], kind=G.WORKSPACE, subject_workspace=w["ws"],
                            at_least=M.EDITOR, role=role)


@pytest.mark.parametrize("who,expected", [
    ("owner", True),       # the agent's owner
    ("adm", True),         # an AgentAdmin
    ("boss", True),        # a workspace owner is an admin of every agent in it
    ("bot", True),         # the agent's own login
    ("line_ed", True),     # an editor line
    ("line_rw", False),    # drives it, does not change it
    ("line_ro", False),
    ("ws_ed", False),      # a workspace editor with no line giving agent_edit
    ("viewer", False),
    ("out", False),        # not in the workspace at all
])
def test_who_may_edit_the_agent(w, who, expected):
    assert access.may_edit_agent(w[who], w["agent"]) is expected


def test_a_workspace_default_editor_line_gives_workspace_editors_the_right(w):
    _default(w, roles.EDITOR)
    assert access.may_edit_agent(w["ws_ed"], w["agent"])
    assert access.may_edit_agent(w["ws_ed2"], w["agent"])
    assert not access.may_edit_agent(w["viewer"], w["agent"])   # below the line's workspace role


def test_a_workspace_default_read_write_line_does_not(w):
    _default(w, roles.READ_WRITE)
    assert not access.may_edit_agent(w["ws_ed"], w["agent"])


def test_a_contact_and_an_anonymous_caller_may_not(w):
    contact = Contact.objects.create(workspace=w["ws"], email="x@example.org")
    assert not access.may_edit_agent(contact, w["agent"])
    assert not access.may_edit_agent(None, w["agent"])


def test_an_owner_who_left_the_workspace_may_not(w):
    M.objects.filter(user=w["owner"]).delete()
    assert not access.may_edit_agent(w["owner"], w["agent"])


def test_the_refusal_names_who_can_fix_it(w):
    why = access.edit_refusal(w["agent"])
    assert "owner@dimagi.com" in why and "give you the editor role on hal" in why


def test_the_bulk_answer_is_the_single_answer(w):
    people = [w[n] for n in ("owner", "adm", "boss", "bot", "line_ed", "line_rw", "ws_ed", "viewer")]
    st = Standings(people, [w["agent"]])
    for p in people:
        assert (access.may_edit_agent(p, w["agent"], standing=st.of(p, w["agent"]),
                                      lines=st.lines_of(w["agent"]))
                == access.may_edit_agent(p, w["agent"])), p.email


def test_an_agent_acting_for_its_asker_is_judged_as_the_asker(w, monkeypatch):
    from apps.harness import turn_asker

    class Ob:                       # what `turn_asker.declare` yields, as far as the rule reads it
        user, decided = w["line_rw"], []
        agent = w["agent"]
        turn = type("T", (), {"pk": 1})()

    monkeypatch.setattr(turn_asker, "note", lambda ob, text: ob.decided.append(text))
    bot = w["bot"]
    assert access.may_edit_agent(bot, w["agent"])
    setattr(bot, turn_asker.ATTR, Ob)
    assert not access.may_edit_agent(bot, w["agent"])          # asker is read_write
    Ob.user = w["line_ed"]
    assert access.may_edit_agent(bot, w["agent"])
    assert Ob.decided == ["edit hal"]


# --- the preflight --------------------------------------------------------------------

def test_the_preflight_lists_exactly_the_people_the_switch_would_cost(w):
    lost = {(r["agent"], r["email"]) for r in agent_edit_check.losses()}
    # Workspace editors with no line, and a read_write / read_only line holder who is also a
    # workspace editor; owner, admins, the editor line and the agent's own login keep it.
    assert lost == {("hal", "ws_ed@dimagi.com"), ("hal", "ws_ed2@dimagi.com"),
                    ("hal", "line_rw@dimagi.com"), ("hal", "line_ro@dimagi.com")}
    assert all(not r["after"] for r in agent_edit_check.losses())
    _default(w, roles.EDITOR)
    assert agent_edit_check.losses() == []


def test_the_command_exits_nonzero_while_anyone_loses_it(w, capsys):
    with pytest.raises(SystemExit) as e:
        call_command("agent_edit_gate_check")
    assert e.value.code == 1
    assert "ws_ed@dimagi.com" in capsys.readouterr().out
    _default(w, roles.EDITOR)
    call_command("agent_edit_gate_check")
    assert "none" in capsys.readouterr().out
