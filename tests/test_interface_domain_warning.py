"""The interface no longer carries who-may-use-it rules, so it no longer warns about their domains.

Until the access list (agent-roles design, 2026-10-30) an interface rule naming a mail domain that
can never prove its senders (`member@<domain>`, no DMARC and no aligned DKIM) was WARNED about,
never refused (canopy-web#1280). Those rules are gone: `parse` refuses the retired keys and points
at the access list, and the interface API carries neither a `summary` nor `warnings`. What survives
is the DMARC lookup (`apps.contacts.domain_proof`), which decides whether a sender is proven.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.interface import InterfaceError, parse
from apps.agents.models import Agent
from apps.contacts import domain_proof
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

SOURCE = """\
full: [member@nodmarc.org, contact@dimagi.com:verified]
capabilities:
  ask:
    callers: [contact, contact@nodmarc.org, member@signs.org:verified]
"""


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    c = Client()
    c.force_login(op)
    return {"op": op, "ws": ws, "agent": agent, "client": c}


@pytest.mark.parametrize("doc, key", [
    ({"full": ["member@nodmarc.org"]}, "full"),
    ({"editors": "confined"}, "editors"),
    ({"capabilities": {"ask": {"callers": ["member@a.org"]}}}, "callers"),
])
def test_parse_refuses_the_retired_rules_and_points_at_the_access_list(doc, key):
    with pytest.raises(InterfaceError) as exc:
        parse(doc)
    assert f"`{key}:`" in str(exc.value) and "access list" in str(exc.value)


def test_publishing_the_old_rules_is_refused_and_saves_nothing(w):
    r = w["client"].put("/api/agents/ace/interface", {"source": SOURCE}, content_type="application/json")
    assert r.status_code == 422, r.content
    assert "access list" in r.content.decode()
    w["agent"].refresh_from_db()
    assert not w["agent"].interface.get("capabilities")


def test_the_interface_api_carries_no_summary_or_warnings(w):
    body = w["client"].get("/api/agents/ace/interface").json()
    assert "summary" not in body and "warnings" not in body


def test_the_real_lookup_reads_the_dmarc_txt(monkeypatch):
    """`_lookup_dmarc` against a stubbed dnspython: v=DMARC1 → found; other TXT →
    missing; NXDOMAIN → missing; timeout → unknown."""
    import dns.exception
    import dns.resolver

    monkeypatch.undo()   # the autouse fixture stubbed _lookup_dmarc itself

    class R:
        def __init__(self, s):
            self.strings = [s]

    def fake(answer):
        def resolve(name, rtype, lifetime):
            assert name.startswith("_dmarc.") and rtype == "TXT" and lifetime <= 1.0
            if isinstance(answer, Exception):
                raise answer
            return [R(answer)]
        return resolve

    cases = [(b"v=DMARC1; p=none", "found"), (b"google-site-verification=x", "missing"),
             (dns.resolver.NXDOMAIN(), "missing"), (dns.exception.Timeout(), "unknown")]
    for answer, want in cases:
        monkeypatch.setattr(dns.resolver, "resolve", fake(answer))
        assert domain_proof._lookup_dmarc("x.org") == want
