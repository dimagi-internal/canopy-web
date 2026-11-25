"""Host tools bound to one subject, for a host site's own contacts (contract S4,
docs/architecture/host-systems-of-record.md).

A contact has no passkey, so never an act-as-you grant. On an entry its owner marked
`contact_scoped`, a contact's turn still reaches the site's tools: canopy's gateway sends a
canopy-signed subject statement naming THAT contact (`Authorization: CanopySubject`) with a
DPoP proof and no token, and the host authorizes every call against `sub`.

Driven end to end, as `tests/test_site_gateway.py` is: the MOUNTED canopy MCP server with a
real caller token, the gateway, and a real FastMCP host that verifies every request with the
SDK's own `verify_subject` and `verify_execution`. Most of the file is refusals, each
asserting the host was never called."""
from __future__ import annotations

import pytest
from canopy_sdk.host import verify_execution, verify_subject
from django.test import Client

from apps.agents import profile_settings
from apps.agents.published_profiles import contact_scoped, parse
from apps.contacts.contact_api import _for_contacts
from apps.contacts.models import Contact
from apps.harness.models import Turn
from apps.tokens.models import AppCredential
from tests.access_helpers import declare
from tests.fixtures import site_gateway as sg

pytestmark = pytest.mark.django_db

CAPS = {"connect": {"sites": ["connect-labs"], "contact_scoped": True,
                    "tools": ["mcp__*canopy-web__who_is_asking"]},
        "ask": {"tools": ["mcp__*canopy-web__who_is_asking"]}}


class SubjectHost(sg.HostMCP):
    """The host as the contract tells it to be: every request verified with the SDK —
    the subject statement, its DPoP proof and `Canopy-Execution` — and refused otherwise."""

    def __init__(self):
        super().__init__()
        self.config, self.keys, self.jtis = sg.host_side()
        self.subjects: list[str] = []
        self.execution_subs: list[str] = []

    def _check(self, scope, headers) -> str:
        if headers.get("authorization", "").startswith("DPoP "):
            return "a grant's token on a subject request"
        try:
            subject = verify_subject(headers, config=self.config, client_keys=self.keys,
                                     replay_store=self.jtis, method=scope["method"])
            ex = verify_execution(headers, config=self.config, client_keys=self.keys,
                                  replay_store=self.jtis, jkt=subject.jkt, access_token="")
        except Exception as exc:  # noqa: BLE001 - refused, and said why
            return f"refused {getattr(exc, 'code', exc)}"
        self.subjects.append(subject.sub)
        self.execution_subs.append(ex.sub)
        return ""


@pytest.fixture()
def host(monkeypatch):
    import httpx2

    from apps.host_grants import host_gateway

    h = SubjectHost()
    monkeypatch.setattr(host_gateway, "_transport_override", httpx2.ASGITransport(app=h.app))
    return h


@pytest.fixture()
def w():
    """The world's conversation, as one of the site's own contacts: Gillian, `u-42` at the site."""
    w = sg.world()
    declare(w["agent"], CAPS)
    contact, session = w["contact"], w["session"]
    session.created_by, session.contact = None, contact
    session.save()
    Turn.objects.filter(pk=w["turn"].pk).update(initiator_user=None, initiator_contact=contact,
                                                initiator_kind="contact")
    return w


def _as_contact(w):
    return sg.as_token(sg.caller_tokens.mint(Turn.objects.get(pk=w["turn"].pk)))


def _refused(w, host, match, name="site_call", args=None):
    with _as_contact(w), pytest.raises(Exception, match=match):
        sg.call(host, name, args if args is not None else {"tool": "marketplace_orgs_get"})
    assert host.calls == [] and host.subjects == [], "a refused call must not reach the host by any route"


# --- the happy path -------------------------------------------------------------------------


def test_a_contacts_turn_calls_the_sites_tool_bound_to_that_contact(w, host):
    with _as_contact(w):
        out = sg.call(host, "site_call", {"tool": "marketplace_orgs_get", "arguments": {}}).structured_content
    assert out["is_error"] is False and host.calls == ["marketplace_orgs_get"]
    # Every request of the MCP session (initialize, list, call) named the contact, and
    # Canopy-Execution said the same `sub`.
    assert host.subjects and set(host.subjects) == {"u-42"} and set(host.execution_subs) == {"u-42"}
    assert all(h["authorization"].startswith("CanopySubject ") for h in host.seen)
    assert all("dpop" in h for h in host.seen)


def test_site_tools_lists_for_the_contact(w, host):
    with _as_contact(w):
        out = sg.call(host, "site_tools", {}).structured_content
    assert out["site"] == "connect-labs" and out["scope"] == ""
    assert {t["name"] for t in out["tools"]} >= {"marketplace_orgs_get"}


def test_the_subject_comes_from_the_turn_never_from_the_arguments(w, host):
    with _as_contact(w):
        sg.call(host, "site_call", {"tool": "marketplace_orgs_get",
                                    "arguments": {"sub": "u-99", "subject": "u-99"}})
    assert set(host.subjects) == {"u-42"}


# --- refusals: the subject mode is reachable only as the contract says ----------------------


def test_an_entry_without_contact_scoped_never_reaches_the_subject_mode(w, host):
    declare(w["agent"], {**CAPS, "connect": {k: v for k, v in CAPS["connect"].items() if k != "contact_scoped"}})
    _refused(w, host, "back on the page")


def test_a_users_turn_never_reaches_the_subject_mode(w, host):
    """The world's visitor as a canopy USER on the same contact_scoped entry: the grant path,
    and with no grant, a refusal — never a subject statement."""
    session = w["session"]
    session.created_by, session.contact = w["visitor"], None
    session.save()
    Turn.objects.filter(pk=w["turn"].pk).update(initiator_user=w["visitor"], initiator_contact=None,
                                                initiator_kind="user")
    _refused(w, host, "back on the page")


def test_a_site_with_no_mcp_resource_is_refused(w, host):
    AppCredential.objects.filter(pk=w["app"].pk).update(host_mcp_resource="")
    _refused(w, host, "does not let me reach its tools")


def test_another_sites_contact_is_refused(w, host):
    other = AppCredential.create_credential(name="other-site", created_by=w["owner"], workspace=w["ws"])
    Contact.objects.filter(pk=w["contact"].pk).update(app=other)
    _refused(w, host, "does not know you as one of its own")


def test_a_contact_with_no_site_id_or_an_email_contact_is_refused(w, host):
    Contact.objects.filter(pk=w["contact"].pk).update(external_id="")
    _refused(w, host, "does not know you as one of its own")
    Contact.objects.filter(pk=w["contact"].pk).update(external_id="u-42", source=Contact.SOURCE_EMAIL)
    _refused(w, host, "does not know you as one of its own")


def test_a_blocked_contact_is_refused(w, host):
    from django.utils import timezone

    Contact.objects.filter(pk=w["contact"].pk).update(blocked_at=timezone.now())
    _refused(w, host, "does not know you as one of its own")


def test_a_conversation_on_another_site_is_refused(w, host):
    w["session"].refresh_from_db()
    w["session"].metadata = {"embed_app": "some-other-site"}
    w["session"].save()
    _refused(w, host, "does not let me act for you|not set up to use")


def test_a_statement_lifted_into_a_different_request_is_refused_by_the_host(w, host):
    """The host checks `rpc`: the SDK refuses a subject statement for another call."""
    from canopy_sdk import consumer, contract
    from canopy_sdk.host import SubjectRefused

    from apps.host_grants import client_identity

    facts = {"sub": "u-42", "site": "connect-labs", "agent": "ace", "turn": "t", "session": "s",
             "capability": "connect"}
    stmt = client_identity.subject_statement(sg.RESOURCE, facts,
                                             rpc=contract.rpc_digest("tools/call", {"name": "a", "arguments": {}}))
    headers = {"Authorization": consumer.subject_authorization(stmt),
               "DPoP": client_identity.dpop_proof("POST", sg.RESOURCE)}
    config, keys, jtis = sg.host_side()
    with pytest.raises(SubjectRefused) as exc:
        verify_subject(headers, config=config, client_keys=keys, replay_store=jtis,
                       rpc=("tools/call", {"name": "admin_delete_everything", "arguments": {}}))
    assert exc.value.code == "wrong_request"


# --- the entry flag -----------------------------------------------------------------------


def test_the_flag_is_strict_and_needs_a_site():
    assert parse({"capabilities": {"c": {"sites": ["s"], "contact_scoped": True}}})["capabilities"]["c"][
        "contact_scoped"] is True
    assert "contact_scoped" not in parse({"capabilities": {"c": {"sites": ["s"]}}})["capabilities"]["c"]
    for bad in ({"sites": ["s"], "contact_scoped": "yes"}, {"contact_scoped": True}):
        with pytest.raises(Exception):  # noqa: B017 - InterfaceError
            parse({"capabilities": {"c": bad}})


def test_only_a_contact_scoped_entry_is_offered_to_the_sites_contacts(w):
    assert contact_scoped(w["agent"], "connect-labs")
    assert _for_contacts(w["app"], [w["agent"]]) == [w["agent"]]
    declare(w["agent"], {"connect": {"sites": ["connect-labs"]}})
    assert _for_contacts(w["app"], [w["agent"]]) == []


def test_an_admin_sets_it_through_the_host_site_route(w):
    declare(w["agent"], {"connect": {"sites": ["connect-labs"]}})
    c = Client()
    c.force_login(w["owner"])
    r = c.put(f"/api/agents/{w['agent'].slug}/host-sites/connect-labs", {"contact_scoped": True},
              content_type="application/json")
    assert r.status_code == 200, r.content
    row = next(x for x in r.json()["sites"] if x["site"] == "connect-labs")
    assert row["contact_scoped"] is True and r.json()["new_site_defaults"]["contact_scoped"] is False
    w["agent"].refresh_from_db()
    assert contact_scoped(w["agent"], "connect-labs")
    assert profile_settings.host_sites_of(w["agent"])[0]["contact_scoped"] is True
