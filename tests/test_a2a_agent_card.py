"""A2A Agent Cards, generated from the declared interface (apps/agents/agent_card.py).

The card is discovery only: these tests pin what it SHOWS — which agents are
discoverable, which skills appear for whom, and that nothing internal leaks —
because the policy itself is enforced elsewhere (interface.capability_for).
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client, override_settings

from apps.agents.interface import parse
from apps.agents.models import Agent, AgentAdmin
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
BASE = "https://canopy.example.org/canopy"

ACE = {
    "full": ["contact@dimagi.com:verified", "member@dimagi.com"],
    "capabilities": {
        "ask": {
            "description": "Ask ACE about your programme.",
            "callers": ["contact", "member"],
            "entry": "/ace:ask --thread {thread_id}",
            "tools": ["Read", "mcp__canopy-web__who_is_asking"],
            "bash": ["canopy email read --repo . {thread_id}"],
            "read_paths": ["{cwd}/**"],
        },
        "marketplace": {
            "description": "Browse the marketplace.",
            "callers": ["contact@partner.org:verified"],
            "pages": ["labs-marketplace://*"],
            "tools": ["mcp__*connect_labs__marketplace_*"],
        },
        "summarise_opportunity": {
            "description": "Summarise an opportunity.",
            "callers": ["member"],
            "input": {"opportunity_id": "integer"},
            "tools": ["Skill"],
        },
        "deploy": {"description": "Ship it.", "callers": [], "bash": ["git push"]},
    },
}
INTERNAL = ["/ace:ask", "who_is_asking", "canopy email read", "{cwd}", "labs-marketplace://",
            "connect_labs", "git push", "opportunity_id", "entry", "read_paths", "bash",
            "op@dimagi.com", "mem@elsewhere.org"]


def _card_url(slug):
    return f"/api/a2a/agents/{slug}/.well-known/agent-card.json"


def _ext_url(slug):
    return f"/api/a2a/agents/{slug}/extendedAgentCard"


@pytest.fixture()
def w():
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    mem = User.objects.create_user("mem", "mem@elsewhere.org", "pw")
    admin = User.objects.create_user("adm", "adm@elsewhere.org", "pw")
    stranger = User.objects.create_user("str", "str@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=op)
    M.objects.create(user=op, workspace=ws, role=M.OWNER)
    M.objects.create(user=mem, workspace=ws, role=M.EDITOR)
    M.objects.create(user=admin, workspace=ws, role=M.EDITOR)
    ace = Agent.objects.create(slug="ace", name="ACE", description="Runs Connect opportunities.",
                               email="ace@dimagi-ai.com", workspace=ws, owner=op,
                               interface=parse(ACE))
    AgentAdmin.objects.create(agent=ace, user=admin)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=op, interface={})
    inner = Agent.objects.create(
        slug="inner", name="Inner", workspace=ws, owner=op,
        interface=parse({"full": ["contact@dimagi.com:verified"],
                         "capabilities": {"ask": {"callers": ["member"]}}}))
    return {"op": op, "mem": mem, "admin": admin, "stranger": stranger, "ws": ws,
            "ace": ace, "hal": hal, "inner": inner}


def _pat(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="t")
    return Client(HTTP_AUTHORIZATION=f"Bearer {raw}")


def _skills(body):
    return {s["id"]: s for s in body["skills"]}


# --- the public card -----------------------------------------------------------------

@override_settings(CANOPY_PUBLIC_BASE_URL=BASE)
def test_public_card_lists_only_what_an_outsider_can_reach(w):
    r = Client().get(_card_url("ace"))
    assert r.status_code == 200, r.content
    body = r.json()
    assert set(_skills(body)) == {"ask", "marketplace"}
    ask = _skills(body)["ask"]
    assert ask["name"] == "Ask"
    assert ask["description"] == "Ask ACE about your programme."
    # Only outsider classes become tags on the public card.
    assert ask["tags"] == ["caller:contact"]
    assert ask["securityRequirements"] == [{"schemes": {"canopyContactToken": {"list": []}}}]
    mkt = _skills(body)["marketplace"]
    assert mkt["tags"] == ["caller:contact", "domain:partner.org", "verified-sender"]
    # `contact:verified` is mail-only: no contact-token door for it.
    assert "securityRequirements" not in mkt


@override_settings(CANOPY_PUBLIC_BASE_URL=BASE)
def test_public_card_is_a_well_formed_a2a_card(w):
    body = Client().get(_card_url("ace")).json()
    for field in ("name", "description", "supportedInterfaces", "version", "capabilities",
                  "defaultInputModes", "defaultOutputModes", "skills"):
        assert body[field], field
    assert body["name"] == "ACE"
    assert body["provider"] == {"organization": "Dimagi", "url": "https://www.dimagi.com"}
    assert body["capabilities"] == {"streaming": False, "pushNotifications": False,
                                    "extendedAgentCard": True}
    ifaces = body["supportedInterfaces"]
    assert [i["url"] for i in ifaces] == [f"{BASE}/api/contact/sessions",
                                          "mailto:ace@dimagi-ai.com", f"{BASE}/api/mcp/"]
    assert all(i["protocolVersion"] == "1.0" and i["protocolBinding"] for i in ifaces)
    schemes = body["securitySchemes"]
    assert schemes["canopyPersonalAccessToken"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    assert schemes["canopyContactToken"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    assert schemes["canopySession"]["apiKeySecurityScheme"]["location"] == "cookie"
    # Each scheme is a proto oneof: exactly one variant set.
    assert all(len(s) == 1 for s in schemes.values())
    # Unset optional fields are omitted, never null.
    assert "null" not in json.dumps(body)
    # Unsigned until canopy has a card-signing key (see agent_card.py).
    assert "signatures" not in body


@override_settings(CANOPY_PUBLIC_BASE_URL=BASE)
def test_public_card_leaks_nothing_internal(w):
    text = Client().get(_card_url("ace")).content.decode()
    for needle in INTERNAL:
        assert needle not in text, needle
    # Neither the member-only nor the admin-only capability is advertised.
    assert "summarise_opportunity" not in text and "Ship it." not in text
    # Nor the full: rule granting a whole profile.
    assert "dimagi.com:verified" not in text


def test_a_private_agent_has_no_card(w):
    # No interface; a member-only interface whose outsider access is a `full:`
    # rule; and a slug that does not exist — indistinguishable 404s.
    bodies = []
    for slug in ("hal", "inner", "nope"):
        r = Client().get(_card_url(slug))
        assert r.status_code == 404, slug
        bodies.append(r.json()["detail"])
    assert len(set(bodies)) == 1


def test_public_card_is_cacheable_and_conditional(w):
    r = Client().get(_card_url("ace"))
    assert r["Cache-Control"] == "public, max-age=300"
    etag = r["ETag"]
    assert etag.startswith('"') and etag.endswith('"')
    again = Client().get(_card_url("ace"), HTTP_IF_NONE_MATCH=etag)
    assert again.status_code == 304
    assert again["ETag"] == etag and not again.content
    # Republishing the interface changes the card, and so the tag.
    iface = dict(ACE, capabilities={**ACE["capabilities"],
                                    "ask": {**ACE["capabilities"]["ask"], "description": "New."}})
    Agent.objects.filter(slug="ace").update(interface=parse(iface))
    assert Client().get(_card_url("ace"), HTTP_IF_NONE_MATCH=etag).status_code == 200


# --- the extended card ---------------------------------------------------------------

def test_extended_card_requires_a_login(w):
    assert Client().get(_ext_url("ace")).status_code == 401


def test_a_member_sees_public_plus_their_own(w):
    r = _pat(w["mem"]).get(_ext_url("ace"))
    assert r.status_code == 200, r.content
    skills = _skills(r.json())
    assert set(skills) == {"ask", "marketplace", "summarise_opportunity"}
    assert skills["summarise_opportunity"]["securityRequirements"] == [
        {"schemes": {"canopyPersonalAccessToken": {"list": []}}}]
    assert skills["summarise_opportunity"]["inputModes"] == ["text/plain", "application/json"]
    assert skills["ask"]["tags"] == ["caller:contact", "caller:member"]
    # Members are sent to MCP first.
    assert r.json()["supportedInterfaces"][0]["url"].endswith("/api/mcp/")
    assert r["Cache-Control"] == "private, max-age=60"
    assert "Authorization" in r["Vary"]
    text = r.content.decode()
    for needle in ("/ace:ask", "who_is_asking", "git push", "opportunity_id", "{cwd}"):
        assert needle not in text


def test_owner_and_admin_see_everything(w):
    for who in ("op", "admin"):
        c = Client()
        c.force_login(w[who])
        skills = _skills(c.get(_ext_url("ace")).json())
        assert set(skills) == {"ask", "marketplace", "summarise_opportunity", "deploy"}, who
        assert skills["deploy"]["tags"] == ["caller:admin"]


def test_a_full_rule_member_sees_everything(w):
    dimagi_member = User.objects.create_user("dm", "dm@dimagi.com", "pw")
    M.objects.create(user=dimagi_member, workspace=w["ws"], role=M.VIEWER)
    skills = _skills(_pat(dimagi_member).get(_ext_url("ace")).json())
    assert "deploy" in skills


def test_a_non_member_gets_the_public_card_or_nothing(w):
    c = _pat(w["stranger"])
    assert set(_skills(c.get(_ext_url("ace")).json())) == {"ask", "marketplace"}
    assert c.get(_ext_url("inner")).status_code == 404
    assert c.get(_ext_url("hal")).status_code == 404


def test_a_member_of_a_private_agent_gets_its_member_skills(w):
    skills = _skills(_pat(w["mem"]).get(_ext_url("inner")).json())
    assert set(skills) == {"ask"}
    # ...while the public still sees nothing.
    assert Client().get(_card_url("inner")).status_code == 404


def test_extended_card_is_conditional(w):
    c = _pat(w["mem"])
    etag = c.get(_ext_url("ace"))["ETag"]
    assert c.get(_ext_url("ace"), HTTP_IF_NONE_MATCH=etag).status_code == 304
    # Per-person: another caller's tag does not match.
    assert _pat(w["stranger"]).get(_ext_url("ace"), HTTP_IF_NONE_MATCH=etag).status_code == 200


def test_the_schema_publishes_camel_case(w):
    schema = Client().get("/api/openapi.json").json()
    card = schema["components"]["schemas"]["A2AAgentCardOut"]["properties"]
    assert "supportedInterfaces" in card and "supported_interfaces" not in card
