"""System accounts — canopy-web#1253, apps/workspaces/system_accounts.py.

A system account is a workspace member that cannot sign in, reached only by
inbound mail through a sender binding in its own workspace. These pin:

* every credential door refuses it;
* aligned mail from a bound address (+ matching subject) becomes ITS turn, in
  the editor tier (whole agent, manual) — and every way that must NOT happen;
* the envelope tells the agent no person is there;
* it can never be raised above editor;
* the REST surface, and the refusal that now names the fix.

Origin: every CloudWatch alarm to Hal was cancelled at enqueue for nine days
(2026-09-28 → 10-07): `no-reply@sns.amazonaws.com` was an unlisted contact under
Hal's ask-only interface, and nothing said so.
"""
from __future__ import annotations

import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.interface import parse
from apps.agents.models import Agent
from apps.harness import caller_context, services, turn_mode
from apps.harness.models import Turn
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces import system_accounts as sa
from apps.workspaces.models import SystemAccount, Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership

SNS = "no-reply@sns.amazonaws.com"
#: Hal's real interface shape: ask-only, Dimagi callers. SNS is not listed.
HAL = {"capabilities": {"ask": {"description": "Ask", "callers": ["member@dimagi.com",
                                                                  "contact@dimagi.com:verified"],
                                "tools": ["Read"]}}}


def _dkim(domain):
    """What Gmail stamps on real SNS alarm mail: DKIM signed by the From: domain."""
    return [{"name": "Authentication-Results",
             "value": f"mx.google.com; dkim=pass header.i=@{domain} header.d={domain}; "
                      f"spf=pass smtp.mailfrom=bounce.{domain}"}]


@pytest.fixture()
def w():
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")
    op = User.objects.create_user("op", "op@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss)
    other = Workspace.objects.create(slug="elsewhere", display_name="Elsewhere", created_by=boss)
    for u, role in ((boss, M.OWNER), (op, M.EDITOR)):
        M.objects.create(user=u, workspace=ws, role=role)
        EmailAddress.objects.create(user=u, email=u.email, verified=True, primary=True)
    M.objects.create(user=boss, workspace=other, role=M.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=op, turn_mode="auto")
    agent.interface = parse(HAL)
    agent.save(update_fields=["interface"])
    return {"ws": ws, "other": other, "agent": agent, "boss": boss, "op": op}


@pytest.fixture()
def alarms(w):
    a = sa.create(w["ws"], name="AWS CloudWatch alarms", description="labs alarms", by=w["boss"])
    sa.add_sender(a, address=SNS, subject_pattern=r'^(ALARM|OK): "labs-', by=w["boss"])
    return a


def _alarm(w, key, *, subject='ALARM: "labs-jj-web-cpu-high-actionable" in US East',
           sender=SNS, aligned=True, agent=None):
    ref = {"from": sender, "thread_id": f"t-{key}", "subject": subject}
    if aligned:
        ref["headers"] = _dkim(sender.rpartition("@")[2])
    turn, _ = services.enqueue_turn(agent=agent or w["agent"], origin=Turn.ORIGIN_EMAIL,
                                    idempotency_key=f"email-{key}", origin_ref=ref,
                                    prompt="/hal:turn")
    return turn


# --- the bug this exists for --------------------------------------------------------

def test_without_a_binding_an_alarm_is_refused_and_the_refusal_names_the_fix(w):
    t = _alarm(w, "unbound")
    assert t.status == Turn.CANCELLED
    assert "system account" in t.result_note


def test_a_bound_alarm_runs_as_the_system_account_in_the_editor_tier(w, alarms):
    t = _alarm(w, "bound")
    assert t.status == Turn.QUEUED, t.result_note
    assert t.initiator_user_id == alarms.user_id
    env = caller_context.build(t)
    assert env["profile"] == "full"
    assert env["granted_by"] == "editor"
    assert env["relationship"] == "member"
    # The agent is told nobody is there.
    assert env["system_account"]["name"] == "AWS CloudWatch alarms"
    assert env["who"]["system_account"]["id"] == alarms.pk
    # Editor tier: manual, even though the agent's own switch is auto.
    assert turn_mode.for_turn(t, fresh=True).mode == "manual"


def test_a_person_has_no_system_account_in_the_envelope(w):
    ref = {"from": "op@dimagi.com", "thread_id": "t-op", "subject": "hi",
           "headers": [{"name": "Authentication-Results",
                        "value": "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=dimagi.com"}]}
    t, _ = services.enqueue_turn(agent=w["agent"], origin=Turn.ORIGIN_EMAIL,
                                 idempotency_key="email-op", origin_ref=ref, prompt="x")
    env = caller_context.build(t)
    assert env["system_account"] is None
    assert "system_account" not in env["who"]


# --- every way a binding must NOT resolve ---------------------------------------------

def test_a_subject_outside_the_pattern_stays_a_contact(w, alarms):
    t = _alarm(w, "other-alarm", subject='ALARM: "someone-elses-alarm" in US East')
    assert t.status == Turn.CANCELLED
    assert t.initiator_user_id is None and t.initiator_contact_id is not None


def test_unaligned_mail_from_the_bound_address_is_not_the_account(w, alarms):
    """`From:` is forgeable; only THIS message's alignment ties it to the address."""
    t = _alarm(w, "forged", aligned=False)
    assert t.initiator_user_id is None
    assert t.status == Turn.CANCELLED


def test_a_binding_in_another_workspace_does_not_apply(w):
    """`no-reply@sns.amazonaws.com` is every AWS customer's: a binding is per tenant."""
    theirs = sa.create(w["other"], name="Their alarms", by=w["boss"])
    sa.add_sender(theirs, address=SNS, by=w["boss"])
    t = _alarm(w, "cross-tenant")
    assert t.initiator_user_id is None
    assert t.status == Turn.CANCELLED


def test_a_disabled_account_stops_resolving_and_re_enables(w, alarms):
    sa.update(alarms, disabled=True, by=w["boss"])
    assert _alarm(w, "off").initiator_user_id is None
    sa.update(alarms, disabled=False, by=w["boss"])
    assert _alarm(w, "on").initiator_user_id == alarms.user_id


def test_removing_its_membership_switches_it_off(w, alarms):
    wsvc.remove_member(workspace=w["ws"], user_id=alarms.user_id, by=w["boss"])
    assert _alarm(w, "removed").initiator_user_id is None


def test_two_accounts_matching_one_message_is_ambiguous_and_resolves_to_neither(w, alarms):
    dup = sa.create(w["ws"], name="Also alarms", by=w["boss"])
    sa.add_sender(dup, address=SNS, by=w["boss"])
    assert _alarm(w, "ambiguous").initiator_user_id is None


def test_a_bound_address_is_never_a_verified_email_for_anything_else(w, alarms):
    """The binding is not an identity: a site asserting `email_verified` for the
    SNS address must not land on the system account."""
    from apps.contacts.services import user_for_verified_email

    assert user_for_verified_email(SNS) is None
    assert user_for_verified_email(alarms.user.email) is None


# --- it can never sign in -------------------------------------------------------------

def test_it_has_no_usable_password_and_an_unroutable_email(alarms):
    assert not alarms.user.has_usable_password()
    assert alarms.user.email.endswith("@" + sa.SYNTHETIC_EMAIL_DOMAIN)
    assert not alarms.user.is_staff and not alarms.user.is_superuser


def test_no_token_can_be_minted_for_it(alarms):
    with pytest.raises(ValueError):
        PersonalToken.create_for_user(user=alarms.user, label="nope")


def test_a_token_that_somehow_exists_does_not_authenticate(alarms):
    """`lookup` is the enforcement point for bearer auth AND MCP."""
    import hashlib

    raw = "sys-token-raw"
    PersonalToken.objects.create(user=alarms.user, label="legacy",
                                 token_hash=hashlib.sha256(raw.encode()).hexdigest())
    assert PersonalToken.lookup(raw) is None
    r = Client().get("/api/workspaces/", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code in (401, 403)


def test_a_delegated_token_for_it_does_not_resolve(alarms):
    import hashlib
    from datetime import timedelta

    from django.utils import timezone

    from apps.tokens.models import AppCredential, DelegatedToken

    app = AppCredential.objects.create(name="site", workspace=alarms.workspace)
    raw = "sys-delegated-raw"
    DelegatedToken.objects.create(user=alarms.user, app=app,
                                  token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                                  expires_at=timezone.now() + timedelta(hours=1))
    assert DelegatedToken.lookup(raw) is None


def test_allauth_refuses_to_log_it_in(rf, alarms):
    from allauth.core.exceptions import ImmediateHttpResponse

    from apps.common.auth_adapter import CustomAccountAdapter

    with pytest.raises(ImmediateHttpResponse):
        CustomAccountAdapter().pre_login(rf.get("/"), alarms.user)


# --- never above editor -----------------------------------------------------------------

def test_it_cannot_be_created_or_raised_above_editor(w, alarms):
    with pytest.raises(sa.SystemAccountError):
        sa.create(w["ws"], name="Root", role=M.ADMIN, by=w["boss"])
    with pytest.raises(sa.SystemAccountError):
        sa.update(alarms, role=M.OWNER, by=w["boss"])
    with pytest.raises(wsvc.MemberError) as exc:
        wsvc.set_member_role(workspace=w["ws"], user_id=alarms.user_id, role=M.ADMIN, by=w["boss"])
    assert exc.value.code == "system_role"
    # Down to viewer is fine, through either door.
    wsvc.set_member_role(workspace=w["ws"], user_id=alarms.user_id, role=M.VIEWER, by=w["boss"])
    assert sa.role_of(alarms) == M.VIEWER


def test_a_viewer_system_account_is_confined_by_the_interface_like_a_viewer(w, alarms):
    """Its role decides, exactly as for a person: a VIEWER gets what the
    interface lists for `member`, and Hal's lists only `member@dimagi.com`."""
    sa.update(alarms, role=M.VIEWER, by=w["boss"])
    t = _alarm(w, "viewer")
    assert t.initiator_user_id == alarms.user_id
    assert t.status == Turn.CANCELLED


def test_bad_input_is_refused(w, alarms):
    with pytest.raises(sa.SystemAccountError):
        sa.add_sender(alarms, address="not-an-address", by=w["boss"])
    with pytest.raises(sa.SystemAccountError):
        sa.add_sender(alarms, address=SNS, subject_pattern="(unclosed", by=w["boss"])
    with pytest.raises(sa.SystemAccountError):
        sa.create(w["ws"], name="AWS CloudWatch alarms", by=w["boss"])   # duplicate name


# --- the REST surface -----------------------------------------------------------------

def _client(user):
    c = Client()
    c.force_login(user)
    return c


def test_api_create_list_update_and_delete(w):
    c = _client(w["boss"])
    r = c.post("/api/workspaces/connect/system-accounts/",
               {"name": "AWS CloudWatch alarms", "description": "labs",
                "senders": [{"address": SNS, "subject_pattern": '^(ALARM|OK): "labs-'}]},
               content_type="application/json")
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["role"] == "editor" and not body["disabled"]
    assert body["senders"][0]["address"] == SNS
    aid = body["id"]

    listed = c.get("/api/workspaces/connect/system-accounts/").json()
    assert [a["id"] for a in listed] == [aid]

    members = c.get("/api/workspaces/connect/members/").json()
    row = next(m for m in members if m["user_id"] == body["user_id"])
    assert row["system"] is True and row["role"] == "editor"
    assert all(not m["system"] for m in members if m["user_id"] != body["user_id"])

    r = c.patch(f"/api/workspaces/connect/system-accounts/{aid}/", {"disabled": True},
                content_type="application/json")
    assert r.status_code == 200 and r.json()["disabled"] is True

    r = c.post(f"/api/workspaces/connect/system-accounts/{aid}/senders/",
               {"address": "alerts@example.org"}, content_type="application/json")
    assert r.status_code == 201
    sid = r.json()["id"]
    assert c.delete(f"/api/workspaces/connect/system-accounts/{aid}/senders/{sid}/").status_code == 204

    assert c.delete(f"/api/workspaces/connect/system-accounts/{aid}/").status_code == 204
    assert not SystemAccount.objects.filter(pk=aid).exists()
    assert not User.objects.filter(pk=body["user_id"]).exists()


def test_api_refuses_an_admin_role_and_non_managers(w):
    c = _client(w["boss"])
    r = c.post("/api/workspaces/connect/system-accounts/", {"name": "x", "role": "admin"},
               content_type="application/json")
    assert r.status_code == 422
    # An editor may not manage members, so may not create a system account…
    r = _client(w["op"]).post("/api/workspaces/connect/system-accounts/", {"name": "y"},
                              content_type="application/json")
    assert r.status_code == 403
    # …but may see them, like the member list.
    assert _client(w["op"]).get("/api/workspaces/connect/system-accounts/").status_code == 200
    # A non-member sees nothing (404, no existence leak).
    stranger = User.objects.create_user("s", "s@dimagi.com", "pw")
    assert _client(stranger).get("/api/workspaces/connect/system-accounts/").status_code == 404


def test_api_member_role_route_refuses_raising_a_system_account(w, alarms):
    r = _client(w["boss"]).patch(f"/api/workspaces/connect/members/{alarms.user_id}/",
                                 {"role": "admin"}, content_type="application/json")
    assert r.status_code == 422
    assert "editor or a viewer" in r.json()["detail"]


def test_changes_are_written_to_the_event_log(w, alarms):
    from apps.events.models import Event

    kinds = set(Event.objects.filter(workspace=w["ws"], source="workspaces.system_accounts")
                .values_list("kind", flat=True))
    assert {"system_account.created", "system_account.sender_added"} <= kinds
