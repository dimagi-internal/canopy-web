"""An agent acts AS the person who started its turn, on canopy-web, only under an act-as-you
permission they approved with their passkey (`apps/harness/act_as.py`).

The rules this file pins:

* no token without a passkey-confirmed approval — made for THIS request, in the person's
  own browser (a session cookie, a step-up window, the person's PAT: none is enough);
* 30 minutes from the passkey touch, never extended;
* the token dies with the turn it was asked from;
* a read grant refuses every write; a write grant still never reaches the excluded routes
  (one list, `act_as.EXCLUDED_*`), over REST or MCP;
* only the turn's own asker: the agent never names the person, and a stored token, a
  confined turn, a contact's, canopy's own or an unverified asker's turn asks no one;
* the token is returned once, and only its hash is kept;
* every request is attributed "<agent> as <person>", and every transition is an event.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import uuid

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.utils import timezone
from fastmcp.exceptions import ToolError
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents.models import Agent
from apps.common.token_hash import hash_token
from apps.contacts.models import Contact
from apps.events.models import Event
from apps.harness import act_as, turn_agent_tokens
from apps.harness.models import ActAsGrant, Runner, RunnerAssignment, Turn
from apps.mcp import api_tools
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.models import MCPAuditLog
from apps.mcp.server import mcp
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace
from tests.fixtures.passkey_confirm import give_passkey

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
JJ = "jj@dimagi.com"
REQUESTS = "/api/me/act-as-requests/"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()


@pytest.fixture
def w():
    ws = a_workspace()
    jj = User.objects.create_user("jj", JJ, "pw")
    wsvc.ensure_member(ws, jj, M.OWNER)
    col = User.objects.create_user("col", "col@dimagi.com", "pw")
    wsvc.ensure_member(ws, col, M.EDITOR)
    hal_login = User.objects.create_user("hal-agent", "hal@dimagi-ai.com", "pw")
    wsvc.ensure_member(ws, hal_login, M.EDITOR)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=jj, user=hal_login)
    box = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=jj, workspace=ws,
                                last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=hal, runner=box, rank=0)
    eva_login = User.objects.create_user("eva-agent", "eva@dimagi-ai.com", "pw")
    wsvc.ensure_member(ws, eva_login, M.EDITOR)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj, user=eva_login)
    RunnerAssignment.objects.create(agent=eva, runner=box, rank=1)
    passkey = give_passkey(jj)
    give_passkey(col)
    return {"ws": ws, "jj": jj, "col": col, "hal": hal, "eva": eva, "box": box, "passkey": passkey}


def _turn(w, *, by=None, assurance="session", agent=None, **kw):
    if kw.get("chat_session") is not None:
        agent = None  # a chat turn's agent is its session's
    else:
        agent = agent or w["hal"]
    kw.setdefault("status", Turn.RUNNING)
    kw.setdefault("claimed_by", w["box"])
    return Turn.objects.create(
        agent=agent, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="tidy my board",
        idempotency_key=uuid.uuid4().hex, initiator_kind="user",
        initiator_via="chat", initiator_assurance=assurance,
        initiator_user=w["jj"] if by is None else by, **kw)


def _login(w, turn) -> str:
    return turn_agent_tokens.issue(turn, w["box"])["token"]


def _bearer(raw: str) -> dict:
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _ask(login: str, scope="either", reason="Move my three stale tasks to Done", **extra):
    return Client().post(REQUESTS, json.dumps({"reason": reason, "scope": scope, **extra}),
                         content_type="application/json", **_bearer(login))


def _status(login: str, grant_id):
    return Client().get(f"{REQUESTS}{grant_id}", **_bearer(login))


def _browser(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _approve(w, grant_id, scope="write", *, user=None, passkey=None):
    """The person's browser: begin, sign the challenge with their passkey, finish."""
    browser = _browser(user or w["jj"])
    started = browser.post(f"{REQUESTS}{grant_id}/approve/begin")
    assert started.status_code == 200, started.content
    cred = (passkey or w["passkey"]).get(started.json()["options"])
    return browser.post(f"{REQUESTS}{grant_id}/approve/finish",
                        json.dumps({"credential": cred, "scope": scope}), content_type="application/json")


def _live_token(w, scope="write", *, turn=None):
    """Ask, approve, collect: (token, grant, turn login)."""
    turn = turn or _turn(w)
    login = _login(w, turn)
    asked = _ask(login, scope="either" if scope != "read" else "read")
    assert asked.status_code == 201, asked.content
    gid = asked.json()["id"]
    assert _approve(w, gid, scope).status_code == 200
    token = _status(login, gid).json()["token"]
    assert token and token.startswith(act_as.PREFIX)
    return token, ActAsGrant.objects.get(pk=gid), login


# --- asking: only the turn's own asker ------------------------------------------------------


def test_the_agent_asks_and_canopy_works_out_the_person_from_its_turn(w, monkeypatch):
    pushed = []
    monkeypatch.setattr("apps.push.services.send_to_user",
                        lambda user, **kw: pushed.append((user.email, kw["url"])) or 1)
    r = _ask(_login(w, _turn(w)))
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["status"] == "pending" and body["approve_url"].endswith(f"/act-as/{body['id']}")
    grant = ActAsGrant.objects.get(pk=body["id"])
    assert (grant.person, grant.agent, grant.requested_scope) == (w["jj"], w["hal"], "either")
    assert pushed == [(JJ, f"/act-as/{body['id']}")]
    # It is on their Settings / feed list.
    listed = _browser(w["jj"]).get(REQUESTS).json()
    assert [row["id"] for row in listed] == [body["id"]]
    assert listed[0]["reason"] == "Move my three stale tasks to Done"


def test_the_person_is_always_the_turns_asker_never_one_the_agent_names(w):
    """A colleague's turn: the request is the colleague's to decide, whatever the body says."""
    r = _ask(_login(w, _turn(w, by=w["col"])), person=JJ, email=JJ)
    assert r.status_code == 201, r.content
    grant = ActAsGrant.objects.get(pk=r.json()["id"])
    assert grant.person == w["col"]
    # Jonathan cannot approve it — it is not his.
    assert _browser(w["jj"]).post(f"{REQUESTS}{grant.pk}/approve/begin").status_code == 404


def test_naming_someone_else_on_behalf_is_refused(w):
    r = Client().post(REQUESTS, json.dumps({"reason": "x", "scope": "read"}),
                      content_type="application/json",
                      HTTP_X_CANOPY_ON_BEHALF_OF="col@dimagi.com", **_bearer(_login(w, _turn(w))))
    assert r.status_code == 403


def test_a_stored_token_asks_no_one(w):
    raw, _ = PersonalToken.create_for_user(user=w["hal"].user, label="canopy-pat")
    r = _ask(raw)
    assert r.status_code == 403 and "no one to act as" in r.json()["detail"]


def test_the_persons_own_pat_asks_no_one(w):
    raw, _ = PersonalToken.create_for_user(user=w["jj"], label="laptop")
    assert _ask(raw).status_code == 403


@pytest.mark.parametrize("change", [
    {"capability": "ask"},                                          # a confined (caller's) turn
    {"initiator_kind": "system", "initiator_assurance": "internal"},  # canopy started it
    {"initiator_assurance": "host_signed"},                          # an unverified asker
    {"status": Turn.DONE},                                       # not executing
])
def test_confined_canopys_unverified_or_finished_turns_ask_no_one(w, change):
    turn = _turn(w)
    login = _login(w, turn)
    Turn.objects.filter(pk=turn.pk).update(**change)
    r = _ask(login)
    assert r.status_code == 403 and "no one to act as" in r.json()["detail"]


def test_a_contacts_turn_asks_no_one(w):
    """A contact has no canopy account (and no passkey): nobody to act as."""
    contact = Contact.objects.create(workspace=w["ws"], email="llo@example.org")
    turn = _turn(w)
    login = _login(w, turn)
    Turn.objects.filter(pk=turn.pk).update(initiator_kind="contact", initiator_user=None,
                                           initiator_contact=contact, initiator_assurance="dmarc")
    assert _ask(login).status_code == 403


def test_a_person_with_no_passkey_is_not_asked(w):
    w["jj"].passkeys.all().delete()
    r = _ask(_login(w, _turn(w)))
    assert r.status_code == 403 and "no passkey" in r.json()["detail"]


def test_one_request_waits_per_turn(w):
    login = _login(w, _turn(w))
    assert _ask(login).status_code == 201
    assert _ask(login).status_code == 409


# --- approving: a fresh passkey touch for THIS request, in the browser ---------------------


def test_no_token_until_the_person_approves_with_their_passkey(w):
    login = _login(w, _turn(w))
    gid = _ask(login).json()["id"]
    pending = _status(login, gid).json()
    assert pending["status"] == "pending" and pending["token"] is None
    assert _approve(w, gid, "read").status_code == 200
    approved = _status(login, gid).json()
    assert approved["status"] == "approved" and approved["scope"] == "read"
    assert approved["token"].startswith(act_as.PREFIX)


def test_a_session_cookie_or_a_step_up_window_alone_does_not_approve(w):
    gid = _ask(_login(w, _turn(w))).json()["id"]
    browser = _browser(w["jj"])
    grant_step_up(browser)  # a passkey touch for something else, minutes ago
    r = browser.post(f"{REQUESTS}{gid}/approve/finish",
                     json.dumps({"credential": {"id": "x"}, "scope": "write"}),
                     content_type="application/json")
    assert r.status_code == 400  # no ceremony begun for THIS request
    assert ActAsGrant.objects.get(pk=gid).status == "pending"


def test_a_ceremony_begun_for_one_request_cannot_approve_another(w):
    first = _ask(_login(w, _turn(w))).json()["id"]
    second = _ask(_login(w, _turn(w, agent=w["eva"]))).json()["id"]
    browser = _browser(w["jj"])
    options = browser.post(f"{REQUESTS}{first}/approve/begin").json()["options"]
    r = browser.post(f"{REQUESTS}{second}/approve/finish",
                     json.dumps({"credential": w["passkey"].get(options), "scope": "write"}),
                     content_type="application/json")
    assert r.status_code == 400
    assert ActAsGrant.objects.get(pk=second).status == "pending"


def test_someone_elses_passkey_does_not_approve(w):
    gid = _ask(_login(w, _turn(w))).json()["id"]
    stranger = give_passkey(w["col"])
    browser = _browser(w["jj"])
    options = browser.post(f"{REQUESTS}{gid}/approve/begin").json()["options"]
    r = browser.post(f"{REQUESTS}{gid}/approve/finish",
                     json.dumps({"credential": stranger.get(options), "scope": "write"}),
                     content_type="application/json")
    assert r.status_code == 400


@pytest.mark.parametrize("action", ["approve/begin", "approve/finish", "decline"])
def test_approve_and_decline_refuse_every_token(w, action):
    gid = _ask(_login(w, _turn(w))).json()["id"]
    raw, _ = PersonalToken.create_for_user(user=w["jj"], label="laptop")
    r = Client().post(f"{REQUESTS}{gid}/{action}", json.dumps({"credential": {}, "scope": "write"}),
                      content_type="application/json", **_bearer(raw))
    assert r.status_code == 403
    assert ActAsGrant.objects.get(pk=gid).status == "pending"


def test_a_read_request_cannot_be_granted_write(w):
    gid = _ask(_login(w, _turn(w)), scope="read").json()["id"]
    assert _approve(w, gid, "write").status_code == 422


def test_decline(w):
    login = _login(w, _turn(w))
    gid = _ask(login).json()["id"]
    assert _browser(w["jj"]).post(f"{REQUESTS}{gid}/decline").status_code == 200
    body = _status(login, gid).json()
    assert body["status"] == "declined" and body["token"] is None


def test_an_unanswered_request_lapses_after_ten_minutes(w):
    login = _login(w, _turn(w))
    gid = _ask(login).json()["id"]
    ActAsGrant.objects.filter(pk=gid).update(request_expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert _status(login, gid).json()["status"] == "expired"
    browser = _browser(w["jj"])
    assert browser.post(f"{REQUESTS}{gid}/approve/begin").status_code == 409


# --- the token: once, 30 minutes, dies with the turn ---------------------------------------


def test_the_token_is_returned_once_and_only_its_hash_is_kept(w):
    token, grant, login = _live_token(w)
    again = _status(login, grant.pk).json()
    assert again["status"] == "approved" and again["token"] is None
    grant.refresh_from_db()
    assert grant.token_hash == hash_token(token)
    assert token not in json.dumps(list(ActAsGrant.objects.values()), default=str)


def test_only_the_login_that_asked_reads_it(w):
    turn = _turn(w)
    login = _login(w, turn)
    gid = _ask(login).json()["id"]
    _approve(w, gid)
    other_login = _login(w, turn)  # another login for the same turn
    assert _status(other_login, gid).status_code == 404
    # The person's browser reads it, never with the token.
    mine = _browser(w["jj"]).get(f"{REQUESTS}{gid}").json()
    assert mine["status"] == "approved" and mine["token"] is None
    assert _status(login, gid).json()["token"]  # still uncollected: the asker gets it


def test_the_token_signs_in_as_the_person(w):
    token, grant, _ = _live_token(w)
    r = Client().get("/api/me/attention/", **_bearer(token))
    assert r.status_code == 200, r.content


def test_thirty_minutes_from_the_touch_never_extended(w):
    token, grant, login = _live_token(w)
    assert grant.expires_at - grant.approved_at == dt.timedelta(minutes=30)
    end = grant.expires_at
    for _ in range(3):  # use, re-read, try to approve again: the end never moves
        Client().get("/api/me/attention/", **_bearer(token))
        _status(login, grant.pk)
        assert _browser(w["jj"]).post(f"{REQUESTS}{grant.pk}/approve/begin").status_code == 409
    grant.refresh_from_db()
    assert grant.expires_at == end
    ActAsGrant.objects.filter(pk=grant.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    r = Client().get("/api/me/attention/", **_bearer(token))
    assert r.status_code == 401 and "30 minutes" in r.json()["detail"]
    assert ActAsGrant.objects.get(pk=grant.pk).status == "expired"


def test_the_token_dies_with_the_turn(w):
    token, grant, _ = _live_token(w)
    Turn.objects.filter(pk=grant.turn_id).update(status=Turn.DONE)
    r = Client().get("/api/me/attention/", **_bearer(token))
    assert r.status_code == 401 and "turn" in r.json()["detail"]


def test_approval_after_the_turn_ended_is_refused(w):
    turn = _turn(w)
    gid = _ask(_login(w, turn)).json()["id"]
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert _browser(w["jj"]).post(f"{REQUESTS}{gid}/approve/begin").status_code == 409


@pytest.mark.parametrize("who", ["agent", "person"])
def test_stop_ends_it_at_once(w, who):
    token, grant, login = _live_token(w)
    caller = Client(**_bearer(login)) if who == "agent" else _browser(w["jj"])
    assert caller.post(f"{REQUESTS}{grant.pk}/stop").json()["status"] == "stopped"
    assert Client().get("/api/me/attention/", **_bearer(token)).status_code == 401


def test_an_unknown_token_signs_in_as_no_one(w):
    assert Client().get("/api/me/attention/", **_bearer(act_as.PREFIX + "nope")).status_code == 401


# --- what it reaches -------------------------------------------------------------------------


def test_a_read_grant_reads_and_refuses_every_write(w):
    token, _, _ = _live_token(w, "read")
    assert Client().get("/api/agents/hal/", **_bearer(token)).status_code == 200
    r = Client().patch("/api/agents/hal/turn-mode", json.dumps({"turn_mode": "auto"}),
                       content_type="application/json", **_bearer(token))
    assert r.status_code == 403 and "read-only" in r.json()["detail"]


def test_a_write_grant_writes_as_the_person(w):
    token, _, _ = _live_token(w, "write")
    r = Client().patch("/api/agents/hal/turn-mode", json.dumps({"turn_mode": "auto"}),
                       content_type="application/json", **_bearer(token))
    assert r.status_code == 200, r.content


EXCLUDED_UNDER_WRITE = [
    ("GET", "/api/tokens/"),
    ("DELETE", "/api/tokens/1/"),
    ("POST", "/api/agents/hal/canopy-token"),
    ("POST", "/api/agents/hal/pat-rotation/rotate"),
    ("GET", "/api/people/me/memory-collections/"),
    ("PUT", "/api/people/me/grants/1/collection/"),
    ("GET", "/api/passkeys/"),
    ("POST", "/api/passkeys/step-up/begin"),
    ("DELETE", "/api/passkeys/1"),
    ("GET", "/api/vault/keyset"),
    ("PUT", "/api/agents/hal/vault/zk/values"),
    ("GET", "/api/canopy-sessions/my-keys"),
    ("POST", "/api/canopy-sessions/00000000-0000-4000-8000-000000000001/key-copies"),
    ("GET", REQUESTS),
    ("POST", f"{REQUESTS}00000000-0000-4000-8000-000000000001/stop"),
    ("DELETE", "/api/me/host-grants/1"),
    ("POST", "/api/me/agent-login-requests/1/accept"),
    ("POST", "/api/runner-device-keys/1/approve"),
    ("POST", "/api/harness/runners/00000000-0000-4000-8000-000000000001/mint"),
    ("POST", "/api/w/{ws}/tokens/"),  # the tenant URL is the same route
    ("GET", "/api/workspaces/{ws}/vaults"),
    ("PATCH", "/api/workspaces/{ws}/connected-apps/1"),
]


@pytest.mark.parametrize("method,path", EXCLUDED_UNDER_WRITE)
def test_a_write_grant_never_reaches_the_excluded_routes(w, method, path):
    token, _, _ = _live_token(w, "write")
    path = path.replace("{ws}", w["ws"].slug)
    r = Client().generic(method, path, "{}", content_type="application/json", **_bearer(token))
    assert r.status_code in (401, 403), (method, path, r.status_code)
    if r.status_code == 403:
        assert "act-as-you permission" in r.json()["detail"]


def test_every_exclusion_names_a_real_route_family():
    """The list cannot rot: each entry still matches some route MCP serves (or the whole
    family has left MCP, in which case it is dropped from here)."""
    ops = api_tools._tool_operations()
    paths = {p.pattern for _m, p, _op in ops}

    def matches(predicate):
        for _m, pattern, _op in ops:
            # Turn the pattern back into a sample path: each parameter becomes "x".
            sample = re.sub(r"\[\^/\]\+", "x", pattern.pattern.strip("^$")).replace("\\", "")
            if predicate(sample):
                return True
        return False

    assert paths
    for prefix in act_as.EXCLUDED_PREFIXES:
        if prefix in ("/api/embed/",):
            continue  # not a tool family at all; listed so it can never become one quietly
        assert matches(lambda s, p=prefix: s.startswith(p)), prefix
    for pattern in act_as.EXCLUDED_PATTERNS:
        assert matches(lambda s, p=pattern: re.search(p, s) is not None), pattern


def test_a_route_mcp_does_not_serve_is_refused(w):
    token, _, _ = _live_token(w, "write")
    r = Client().post("/api/harness/runners/heartbeat", "{}", content_type="application/json",
                      **_bearer(token))
    assert r.status_code == 403


def test_the_token_cannot_mint_and_never_steps_up(w):
    token, _, _ = _live_token(w, "write")
    r = Client().post("/api/agents/hal/canopy-login", "{}", content_type="application/json",
                      **_bearer(token))
    assert r.status_code == 403


# --- MCP: the same line --------------------------------------------------------------------------


@contextlib.contextmanager
def _mcp_as(token: str):
    access = async_to_sync(CanopyPATVerifier().verify_token)(token)
    assert access is not None
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


def _tool_names():
    return {t.name for t in async_to_sync(mcp.list_tools)()}


def test_mcp_lists_no_excluded_tool_and_a_read_grant_lists_no_write(w):
    token, _, _ = _live_token(w, "read")
    with _mcp_as(token):
        names = _tool_names()
    assert "get_agent" in names and "list_my_tasks" in names
    for gone in ("set_turn_mode", "list_tokens", "list_passkeys", "mint_agent_canopy_token",
                 "list_my_memory_collections", "site_call", "site_tools", "close_proposed_sessions"):
        assert gone not in names, gone


def test_mcp_refuses_writes_under_read_and_excluded_tools_under_write(w):
    read_token, _, _ = _live_token(w, "read")
    with _mcp_as(read_token), pytest.raises(ToolError, match="read-only"):
        async_to_sync(mcp.call_tool)("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    write_token, grant, _ = _live_token(w, "write", turn=_turn(w, agent=w["eva"]))
    with _mcp_as(write_token), pytest.raises(ToolError, match="act-as-you permission"):
        async_to_sync(mcp.call_tool)("list_tokens", {})
    with _mcp_as(write_token):
        async_to_sync(mcp.call_tool)("set_turn_mode", {"slug": "hal", "turn_mode": "auto"})
    row = MCPAuditLog.objects.filter(tool="set_turn_mode", ok=True).get()
    assert (row.user, row.acting_agent, row.act_as_grant) == (w["jj"], "eva", str(grant.pk))


def test_the_in_process_request_refuses_an_excluded_route_even_past_the_mcp_filter(w, monkeypatch):
    """Defence in depth: with the MCP filter out of the way, the route itself refuses."""
    from apps.mcp import act_as_scope

    monkeypatch.setattr(act_as_scope, "refusal", lambda tool, claims: None)
    token, _, _ = _live_token(w, "write")
    with _mcp_as(token), pytest.raises(ToolError, match="act-as-you permission"):
        async_to_sync(mcp.call_tool)("list_tokens", {})


def test_mcp_refuses_the_token_once_the_turn_ends(w):
    token, grant, _ = _live_token(w, "write")
    Turn.objects.filter(pk=grant.turn_id).update(status=Turn.DONE)
    assert async_to_sync(CanopyPATVerifier().verify_token)(token) is None


# --- attribution --------------------------------------------------------------------------------


def test_every_rest_request_is_a_call_log_row_naming_the_agent(w):
    token, grant, _ = _live_token(w, "write")
    Client().get("/api/agents/hal/", **_bearer(token))
    row = MCPAuditLog.objects.get(tool="get_agent")
    assert (row.user, row.acting_agent, row.act_as_grant, row.ok) == (w["jj"], "hal", str(grant.pk), True)
    listed = _browser(w["jj"]).get("/api/events/mcp-calls").json()["items"]
    assert any(i["acting_agent"] == "hal" and i["user_email"] == JJ for i in listed)


def test_every_transition_is_an_admin_event_without_the_reason(w):
    token, grant, login = _live_token(w, "write")
    Client(**_bearer(login)).post(f"{REQUESTS}{grant.pk}/stop")
    kinds = list(Event.objects.filter(source="harness.act_as").values_list("kind", flat=True))
    assert set(kinds) == {"act_as.requested", "act_as.approved", "act_as.stopped"}
    approved = Event.objects.get(kind="act_as.approved")
    assert approved.payload["scope"] == "write" and approved.payload["person"] == JJ
    assert approved.payload["agent"] == "hal" and approved.payload["turn"] == str(grant.turn_id)
    for ev in Event.objects.filter(source="harness.act_as"):
        assert "stale tasks" not in json.dumps(ev.payload) + ev.summary


def test_the_sweep_records_expiry_once(w):
    _, grant, _ = _live_token(w, "read")
    ActAsGrant.objects.filter(pk=grant.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert act_as.expire_due() == 1
    assert act_as.expire_due() == 0
    assert Event.objects.filter(kind="act_as.expired").count() == 1


def test_a_turn_the_token_starts_is_not_a_verified_askers(w):
    """So it can never chain into another act-as-you permission."""
    from apps.harness.caller_context import is_verified

    token, _, _ = _live_token(w, "write")
    payload = {"agent_slug": "hal", "origin": "api", "idempotency_key": uuid.uuid4().hex,
               "prompt": "and again"}
    r = Client().post("/api/harness/turns/", json.dumps(payload), content_type="application/json",
                      **_bearer(token))
    assert r.status_code in (201, 403), r.content
    if r.status_code == 201:
        made = Turn.objects.get(pk=r.json()["id"])
        assert made.initiator_user == w["jj"] and not is_verified(made)


# --- a laptop: the turn finishes, the session (and the person's work in it) goes on ----------


def _laptop_session(w):
    from apps.canopy_sessions.models import RunnerBinding, Session

    laptop = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, owner=w["jj"], workspace=w["ws"],
                                   last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    session = Session.objects.create(workspace=w["ws"], agent=w["hal"], status=Session.ACTIVE)
    RunnerBinding.objects.create(session=session, runner=laptop, session_key="k1")
    turn = _turn(w, chat_session=session, status=Turn.DONE, claimed_by=laptop)
    login = turn_agent_tokens.issue_for_session(turn, laptop, session)["token"]
    return session, turn, login


def test_a_laptop_session_login_asks_and_its_token_lives_while_the_session_is_held(w):
    session, turn, login = _laptop_session(w)
    asked = _ask(login)
    assert asked.status_code == 201, asked.content
    gid = asked.json()["id"]
    assert _approve(w, gid, "read").status_code == 200
    token = _status(login, gid).json()["token"]
    assert Client().get("/api/me/attention/", **_bearer(token)).status_code == 200
    # The session closes: the permission is over.
    type(session).objects.filter(pk=session.pk).update(status="archived")
    r = Client().get("/api/me/attention/", **_bearer(token))
    assert r.status_code == 401


def test_a_laptop_session_permission_ends_when_someone_else_drives_the_session(w):
    session, turn, login = _laptop_session(w)
    gid = _ask(login).json()["id"]
    _approve(w, gid, "read")
    token = _status(login, gid).json()["token"]
    _turn(w, by=w["col"], chat_session=session, status=Turn.DONE)  # a colleague's message
    assert Client().get("/api/me/attention/", **_bearer(token)).status_code == 401


# --- a chat: the link reaches the person only when the asking turn ends -----------------------


def _chat(w):
    from apps.canopy_sessions.models import Session

    return Session.objects.create(workspace=w["ws"], agent=w["hal"], status=Session.ACTIVE)


def _chat_turn(w, session, *, by=None, status=Turn.RUNNING):
    return _turn(w, chat_session=session, by=by, status=status)


def test_a_request_waits_thirty_minutes(w):
    gid = _ask(_login(w, _turn(w))).json()["id"]
    grant = ActAsGrant.objects.get(pk=gid)
    assert grant.request_expires_at - grant.created_at >= dt.timedelta(minutes=29, seconds=59)


def test_in_a_chat_the_request_outlives_the_turn_and_a_later_turn_collects_it_once(w):
    session = _chat(w)
    first = _chat_turn(w, session)
    first_login = _login(w, first)
    gid = _ask(first_login, scope="read").json()["id"]
    # The turn ends, and only now does its reply (with the link) reach Jonathan.
    Turn.objects.filter(pk=first.pk).update(status=Turn.DONE)
    assert _status(first_login, gid).json()["status"] == "pending"
    assert _approve(w, gid, "read").status_code == 200
    # He answers in the chat: a new turn, with a new login, picks it up.
    Turn.objects.filter(pk=first.pk).update(status=Turn.DONE)
    second = _chat_turn(w, session)
    second_login = _login(w, second)
    got = _status(second_login, gid).json()
    assert got["status"] == "approved" and got["token"].startswith(act_as.PREFIX)
    assert _status(second_login, gid).json()["token"] is None
    assert _status(first_login, gid).json()["token"] is None  # once, whoever collects it
    token = got["token"]
    assert Client().get("/api/me/attention/", **_bearer(token)).status_code == 200
    # The asking turn has long ended; the conversation is what keeps it.
    Turn.objects.filter(pk=second.pk).update(status=Turn.DONE)
    assert Client().get("/api/me/attention/", **_bearer(token)).status_code == 200


def test_in_a_chat_the_permission_ends_when_someone_else_speaks_or_the_chat_closes(w):
    session = _chat(w)
    turn = _chat_turn(w, session)
    login = _login(w, turn)
    gid = _ask(login, scope="read").json()["id"]
    _approve(w, gid, "read")
    token = _status(login, gid).json()["token"]
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert Client().get("/api/me/attention/", **_bearer(token)).status_code == 200
    type(session).objects.filter(pk=session.pk).update(status="archived")
    assert Client().get("/api/me/attention/", **_bearer(token)).status_code == 401


def test_in_a_chat_a_colleagues_message_ends_a_waiting_request(w):
    session = _chat(w)
    turn = _chat_turn(w, session)
    login = _login(w, turn)
    gid = _ask(login).json()["id"]
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    _chat_turn(w, session, by=w["col"], status=Turn.DONE)
    assert _status(login, gid).json()["status"] == "expired"


def test_a_turn_outside_the_chat_or_another_agent_cannot_collect_it(w):
    session = _chat(w)
    turn = _chat_turn(w, session)
    gid = _ask(_login(w, turn)).json()["id"]
    _approve(w, gid, "read")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    elsewhere = _login(w, _turn(w))  # hal, Jonathan, but not in this chat
    assert _status(elsewhere, gid).status_code == 404
    eva = _login(w, _turn(w, agent=w["eva"]))
    assert _status(eva, gid).status_code == 404


def test_a_later_turn_in_the_chat_may_stop_it(w):
    session = _chat(w)
    turn = _chat_turn(w, session)
    gid = _ask(_login(w, turn)).json()["id"]
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    later = _login(w, _chat_turn(w, session))
    assert Client(**_bearer(later)).post(f"{REQUESTS}{gid}/stop").json()["status"] == "stopped"


def test_one_request_waits_per_chat(w):
    session = _chat(w)
    turn = _chat_turn(w, session)
    assert _ask(_login(w, turn)).status_code == 201
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    assert _ask(_login(w, _chat_turn(w, session))).status_code == 409


def test_the_agent_lists_its_requests_in_this_chat_without_tokens(w):
    session = _chat(w)
    turn = _chat_turn(w, session)
    gid = _ask(_login(w, turn), scope="read").json()["id"]
    _approve(w, gid, "read")
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE)
    later = _login(w, _chat_turn(w, session))
    listed = Client().get(REQUESTS, **_bearer(later)).json()
    assert [r["id"] for r in listed] == [gid] and "token" not in listed[0]
    other = _login(w, _turn(w, agent=w["eva"]))
    assert Client().get(REQUESTS, **_bearer(other)).json() == []
    # The token is still uncollected: the later turn collects it.
    assert _status(later, gid).json()["token"]


def test_the_push_is_recorded(w, monkeypatch):
    monkeypatch.setattr("apps.push.services.send_to_user", lambda user, **kw: 2)
    gid = _ask(_login(w, _turn(w))).json()["id"]
    assert ActAsGrant.objects.get(pk=gid).push_sent == 2
    assert Event.objects.get(kind="act_as.requested").payload["pushed"] == 2


def test_an_unreviewed_turn_may_act_as_you_to_read_only(w):
    # `auto` is a turn nobody reviews: write authority as the person would have no human in it.
    login = _login(w, _turn(w, turn_mode="auto"))
    for scope in ("write", "either"):
        r = _ask(login, scope=scope)
        assert r.status_code == 403 and "unreviewed" in r.json()["detail"], scope
    assert _ask(login, scope="read").status_code in (200, 201)


def test_a_reviewed_turn_may_still_ask_for_write(w):
    assert _ask(_login(w, _turn(w, turn_mode="manual")), scope="write").status_code in (200, 201)
