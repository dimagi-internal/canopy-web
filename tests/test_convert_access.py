"""Translating an agent's published rules into its access list changes nobody's access.

`apps/agents/convert_access.py` rewrites `full:`, each door's `callers`, `editors:` and the
implicit "a workspace editor is an editor" rule as access-list lines, then asks
`access.decide` the old way and the new way about everyone who could plausibly ask. These are
the access-relevant parts of the interfaces actually published on 2026-10-30 (tools and paths
trimmed to stubs), so the translation is held to the real configurations, not to invented ones.
"""
from __future__ import annotations

import pytest
import yaml
from django.contrib.auth import get_user_model

from apps.agents import access, convert_access, roles
from apps.agents.interface import parse
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.contacts.models import Contact
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

REAL = {
    "ace": """
full: [contact@dimagi.com:verified, contact@dimagi-associate.com:verified, member@dimagi.com, member@dimagi-associate.com]
capabilities:
  ask: {callers: [contact, member], tools: [Read]}
  canopy: {callers: [member], sites: [canopy-web], tools: [Read]}
  connect: {callers: [contact, member], sites: [connect-labs], tools: [Read]}
""",
    "echo": """
capabilities:
  ask:
    callers: [member@dimagi.com, member@dimagi-associate.com, contact@dimagi.com:verified, contact@dimagi-associate.com:verified]
    tools: [Read]
""",
    "eva": """
capabilities:
  ask: {callers: [member, "contact:verified"], tools: [Read]}
""",
    "hal": """
capabilities:
  ask:
    callers: [member@dimagi.com, member@dimagi-ai.com, member@dimagi-associate.com, contact@dimagi.com:verified, contact@dimagi-ai.com:verified, contact@dimagi-associate.com:verified]
    tools: [Read]
""",
    "oolong": """
capabilities:
  ask: {callers: [member, "contact@dimagi.com:verified"], tools: [Read]}
""",
    "trellis": """
editors: confined
capabilities:
  ask: {callers: [member], tools: [Read]}
  canopy: {callers: [member], sites: [canopy-web], tools: [Read]}
""",
    "labsdata": """
capabilities:
  data: {callers: [contact, member], sites: [connect-labs], data: sensitive, tools: [Read]}
""",
    "default": None,
}


@pytest.fixture()
def pop():
    mk = get_user_model().objects.create_user
    names = ("boss", "owner", "wsadmin", "ed", "vw", "parented", "stranger", "agentadmin")
    u = {n: mk(n, f"{n}@dimagi.com", "pw") for n in names}
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=u["boss"])
    home = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u["boss"], parent=root)
    M.objects.create(user=u["boss"], workspace=root, role=M.OWNER)
    for n, role in (("owner", M.EDITOR), ("wsadmin", M.ADMIN), ("ed", M.EDITOR), ("vw", M.VIEWER),
                    ("agentadmin", M.EDITOR)):
        M.objects.create(user=u[n], workspace=home, role=role)
    M.objects.create(user=u["parented"], workspace=root, role=M.EDITOR)     # an editor of the PARENT only
    return {"root": root, "home": home, **u}


def _agent(pop, slug, ws=None):
    doc = yaml.safe_load(REAL[slug]) if REAL[slug] else None
    agent = Agent.objects.create(slug=slug, name=slug, workspace=ws or pop["home"], owner=pop["owner"],
                                 interface=parse(doc) if doc else {})
    AgentAdmin.objects.create(agent=agent, user=pop["agentadmin"], granted_by=pop["owner"])
    return agent


@pytest.mark.parametrize("slug", sorted(REAL))
def test_the_translation_changes_no_ones_access(pop, slug):
    agent = _agent(pop, slug)
    conv = convert_access.plan(agent)
    assert conv.unsupported == []
    assert conv.probed >= 10                                   # it really asked about people
    assert [str(d) for d in conv.differences] == []
    assert conv.ok


def test_a_workspace_editor_is_kept_as_a_gated_editor_unless_the_agent_confined_them(pop):
    plain = convert_access.translate(_agent(pop, "echo"))[0]
    gate = [ln for ln in plain if ln.kind == AgentGrant.WORKSPACE and ln.at_least == "editor"]
    assert [(ln.role, ln.manual_only) for ln in gate] == [(roles.EDITOR, True)]
    confined = convert_access.translate(_agent(pop, "trellis"))[0]
    assert not [ln for ln in confined if ln.at_least == "editor"]


def test_what_ace_says_about_staff_becomes_four_editor_lines(pop):
    lines = convert_access.translate(_agent(pop, "ace"))[0]
    got = sorted(ln.describe() for ln in lines if ln.role == roles.EDITOR and not ln.manual_only)
    assert got == [
        "A known visitor or sender at @dimagi-associate.com (verified) -> editor",
        "A known visitor or sender at @dimagi.com (verified) -> editor",
        "Everyone in connect at @dimagi-associate.com -> editor",
        "Everyone in connect at @dimagi.com -> editor",
    ]


def test_a_door_is_a_viewer_door_when_it_was_open_to_contacts_and_a_user_door_when_only_to_members(pop):
    iface = convert_access.translate(_agent(pop, "ace"))[1]
    assert {n: c["role"] for n, c in iface["capabilities"].items()} == {
        "ask": roles.VIEWER, "canopy": roles.USER, "connect": roles.VIEWER}
    assert all(c["callers"] == [] for c in iface["capabilities"].values())
    assert iface["full"] == []


def test_an_unknown_class_is_reported_not_translated(pop):
    agent = Agent.objects.create(slug="odd", name="odd", workspace=pop["home"], owner=pop["owner"],
                                 interface=parse({"capabilities": {"ask": {"callers": ["unknown"], "tools": ["Read"]}}}))
    conv = convert_access.plan(agent)
    assert conv.unsupported and not conv.ok
    with pytest.raises(ValueError):
        convert_access.apply(conv)


def test_applying_it_writes_the_lines_switches_the_agent_and_decides_the_same(pop):
    agent = _agent(pop, "hal")
    probe = Contact.objects.create(workspace=pop["home"], email="idiene@dimagi.com")
    before = access.decide(agent, probe, verified=True)
    conv = convert_access.plan(agent)
    convert_access.apply(conv, by=pop["owner"])
    agent.refresh_from_db()
    assert agent.uses_access_list is True
    assert AgentGrant.objects.filter(agent=agent).count() == len(conv.lines)
    assert all(c["callers"] == [] for c in agent.interface["capabilities"].values())
    after = access.decide(agent, probe, verified=True)
    assert (after.access, after.capability) == (before.access, before.capability) == (access.CONFINED, "ask")
    assert after.source.startswith("list:contact:dimagi.com")
    # And someone the old rules refused is still refused, now with a reason that names the fix.
    nobody = Contact.objects.create(workspace=pop["home"], email="x@evil.example")
    refused = access.decide(agent, nobody, verified=True)
    assert refused.access == access.NONE and "access list" in refused.reason


def test_applying_twice_changes_nothing(pop):
    agent = _agent(pop, "echo")
    convert_access.apply(convert_access.plan(agent))
    n = AgentGrant.objects.filter(agent=agent).count()
    agent.refresh_from_db()
    convert_access.apply(convert_access.plan(agent))      # an already-converted agent plans to itself
    assert AgentGrant.objects.filter(agent=agent).count() == n


def test_the_command_prints_a_dry_run_and_applies_only_on_request(pop):
    from io import StringIO

    from django.core.management import call_command

    agent = _agent(pop, "oolong")
    out = StringIO()
    call_command("convert_access", agent=["oolong"], stdout=out)
    text = out.getvalue()
    assert "oolong: OK" in text and "A known visitor or sender at @dimagi.com (verified) -> viewer" in text
    agent.refresh_from_db()
    assert agent.uses_access_list is False and not AgentGrant.objects.filter(agent=agent).exists()
    call_command("convert_access", agent=["oolong"], apply=True, stdout=StringIO())
    agent.refresh_from_db()
    assert agent.uses_access_list is True and AgentGrant.objects.filter(agent=agent).exists()
