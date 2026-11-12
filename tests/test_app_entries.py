"""An agent profile entry bound to ONE workspace app (`apps/agents/app_entries.py`).

The Jackson household's case: Addy is a GUEST of the family workspace for the chores app only. A `use:<key>`
line names her on Bailey's access list. She reaches Bailey from the app's chat panel, by email and from any
other chat, and every one of those turns runs in the app entry: canopy's app tools, as her, by her own role in
the app, recorded "bailey for addy". Never read mode, never the whole agent; selection never grants; the
caller token reaches nothing else.
"""
from __future__ import annotations

import contextlib
import fnmatch

import pytest
from allauth.account.models import EmailAddress
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import Client
from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents import app_entries, profile_settings, roles, turn_context
from apps.agents import published_profiles as pp
from apps.agents import services as agent_services
from apps.agents import session_context as sc
from apps.agents.models import Agent, AgentGrant, AgentProject, AgentTask, TaskEvent
from apps.canopy_sessions import access as session_access
from apps.canopy_sessions import send as session_send
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Session
from apps.harness import caller_context, caller_tokens, services
from apps.harness import initiator as who
from apps.harness.models import Turn
from apps.mcp.auth import CanopyPATVerifier
from apps.mcp.server import mcp
from apps.tokens.models import AppCredential, DelegatedToken
from apps.workspace_apps.models import AppInstallation, AppRecord, AppRoleGrant
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.access_helpers import allow
from tests.token_helpers import bearer_client

pytestmark = pytest.mark.django_db
M = WorkspaceMembership
J = "application/json"

MANIFEST = {
    "key": "chores", "name": "Chores",
    "roles": {"family": {"capability": "content.write"}, "helper": {"description": "checks chores off"}},
    "settings": {"task_project": {"type": "text", "label": "Chores project", "default": "chores"}},
    "collections": {"note": {"fields": {"text": {"type": "text", "required": True}}, "read": "helper"}},
    "actions": {"add_note": {"role": "helper", "op": "create", "collection": "note", "input": ["text"]}},
    "pages": [{"slug": "week", "code": "function View({app}){return <div>{app.name}</div>}"}],
    "tasks": {
        "project_setting": "task_project", "read": "helper",
        "actions": {"check": {"role": "helper", "do": "done"}, "note": {"role": "helper", "do": "comment"}},
    },
}
GARDEN = {**MANIFEST, "key": "garden", "name": "Garden", "tasks": None}
GARDEN.pop("tasks")
APP_TOOLS = {"app_query", "app_act", "app_tasks_query", "app_tasks_act"}


def _dmarc(domain):
    return [{"name": "Authentication-Results",
             "value": f"mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from={domain}"}]


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _person(name, email):
    u = User.objects.create_user(name, email, "pw")
    EmailAddress.objects.create(user=u, email=email, verified=True, primary=True)
    return u


@pytest.fixture()
def w():
    jon = _person("jon", "jon@marshellis.com")
    viewer = _person("viewer", "viewer@marshellis.com")
    editor = _person("editor", "editor@marshellis.com")
    addy = _person("addy", "addy@jacksonhome.org")
    stranger = _person("stranger", "stranger@elsewhere.org")
    ws = Workspace.objects.create(slug="marshellis", display_name="Marshellis", created_by=jon)
    for u, role in ((jon, M.OWNER), (viewer, M.VIEWER), (editor, M.EDITOR)):
        M.objects.create(user=u, workspace=ws, role=role)
    M.objects.create(user=addy, workspace=ws, role=M.GUEST, scope={"apps": {"chores": "helper"}})
    AgentProject.objects.create(workspace=ws, handle="chores", name="Chores", owner_user=jon)
    for manifest in (MANIFEST, GARDEN):
        r = _client(jon).post("/api/workspace-apps/marshellis/", {"manifest": manifest}, content_type=J)
        assert r.status_code == 201, r.content
    chores = AppInstallation.objects.get(workspace_id="marshellis", key="chores")
    for u in (viewer, editor):
        AppRoleGrant.objects.create(installation=chores, user=u, role="helper", granted_by=jon)
    bailey = Agent.objects.create(slug="bailey", name="Bailey", workspace=ws, owner=jon,
                                  email="bailey@marshellis.com")
    profile_settings.set_app_entry(bailey, "chores", {"description": "The chores"}, by=jon)
    profile_settings.set_app_entry(bailey, "garden", {}, by=jon)
    bailey.refresh_from_db()
    [trash] = agent_services.create_tasks(None, [{"title": "Take out the trash", "project": "marshellis/chores",
                                                   "status": "todo", "assignee": ""}],
                                          workspace="marshellis", by_user=jon)
    return dict(ws=ws, jon=jon, viewer=viewer, editor=editor, addy=addy, stranger=stranger, bailey=bailey,
                chores=chores, trash=trash, key=app_entries.entry_for_app(bailey.interface, "chores"),
                garden_key=app_entries.entry_for_app(bailey.interface, "garden"))


def _line(agent, user, *, capability="", role=""):
    from apps.agents import rolecatalog  # noqa: PLC0415 — only this helper needs the catalog

    return AgentGrant.objects.create(
        agent=agent, kind=AgentGrant.PERSON, user=user, capability=capability, role=role,
        role_def=rolecatalog.role_for(agent.workspace_id, role) if role else None,
        subject_key=AgentGrant.key_of(AgentGrant.PERSON, user_id=user.pk, site=capability))


def _app_session(w, user):
    return chat.create_session(workspace=w["ws"], created_by=user, agent=w["bailey"], title="t",
                               metadata={app_entries.WORKSPACE_APP_KEY: "marshellis/chores"})


def _ask(w, user, session=None):
    ctx = sc.build(session) if session is not None else sc.empty(channel="canopy_web_chat")
    return turn_context.resolve_turn_context(ctx, turn_context.Asker(who=user, verified=True), w["bailey"])


def _email(w, sender, key="e1"):
    turn, _ = services.enqueue_turn(
        agent=w["bailey"], origin=Turn.ORIGIN_EMAIL, idempotency_key=f"email-{key}", prompt="/bailey:turn",
        origin_ref={"from": sender, "thread_id": f"t-{key}", "subject": "trash",
                    "headers": _dmarc(sender.rpartition("@")[2])})
    return turn


def _chat_turn(w, user, session, key="c1"):
    _msg, turn = session_send.send_message(
        session=session, text="did the trash", user=user, client_id=key, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        initiator=who.for_user(user, via=Turn.ORIGIN_CANOPY_WEB_CHAT, assurance=who.SESSION))
    return turn


# --- the entry: what it may hold -------------------------------------------------------------


def test_an_app_entry_parses_to_one_app_with_canopys_fixed_tools(w):
    doc = pp.parse({"capabilities": {"household": {"apps": ["chores"], "ceiling": ["app_tasks_query"],
                                                   "entry": "/bailey:household-ask --thread {thread_id}"}}})
    assert doc["capabilities"]["household"] == {"description": "", "apps": ["chores"],
                                                "entry": "/bailey:household-ask --thread {thread_id}",
                                                "ceiling": ["app_tasks_query"]}


@pytest.mark.parametrize("entry,needle", [
    ({"apps": ["chores"], "tools": ["Bash"]}, "canopy's own"),
    ({"apps": ["chores"], "bash": ["ls"]}, "canopy's own"),
    ({"apps": ["chores"], "read_paths": ["/"]}, "canopy's own"),
    ({"apps": ["chores"], "sites": ["connect-labs"]}, "canopy's own"),
    ({"apps": ["chores"], "data": "sensitive"}, "canopy's own"),
    ({"apps": ["chores", "garden"]}, "exactly one app"),
    ({"apps": ["Not A Key"]}, "not an app key"),
    ({"apps": ["chores"], "ceiling": ["site_call"]}, "ceiling"),
    ({"apps": ["chores"], "ceiling": ["mcp__*"]}, "ceiling"),
])
def test_an_app_entry_cannot_list_or_widen_its_tools(entry, needle):
    with pytest.raises(pp.InterfaceError, match=needle):
        pp.parse({"capabilities": {"household": entry}})


def test_the_profile_is_canopys_scratch_only_and_its_mail_is_thread_pinned(w):
    p = pp.profile(w["bailey"], w["key"])
    assert sorted(p["tools"]) == sorted(["Read", "Write", "ToolSearch", "mcp__*canopy-web__who_is_asking",
                                         *(f"mcp__*canopy-web__{t}" for t in app_entries.NARROWABLE)])
    scratch = f"{{cwd}}/.bailey-{w['key']}/*"
    assert p["read_paths"] == [scratch] and p["write_paths"] == [scratch]
    # The agent has a mailbox: exactly read mode's thread-pinned read and reply, nothing else of its mail.
    assert p["bash"] == ["canopy email read --repo . {thread_id}",
                         f"canopy email review-receipt --repo . --body-file {scratch}",
                         f"bin/bailey-email --reply-all --thread-id {{thread_id}} --subject-file {scratch} "
                         f"--body-file {scratch}"]
    assert all("{thread_id}" in b or "review-receipt" in b for b in p["bash"])
    w["bailey"].email = ""
    assert pp.profile(w["bailey"], w["key"])["bash"] == []


def test_a_ceiling_only_narrows(w):
    profile_settings.set_app_entry(w["bailey"], "chores", {"ceiling": ["app_tasks_query"]}, by=w["jon"])
    w["bailey"].refresh_from_db()
    tools = pp.profile(w["bailey"], w["key"])["tools"]
    assert [t for t in tools if t.startswith("mcp")] == ["mcp__*canopy-web__who_is_asking",
                                                        "mcp__*canopy-web__app_tasks_query"]


def test_the_writer_binds_only_an_installed_app_of_its_own_workspace_and_sets_no_tools(w):
    with pytest.raises(profile_settings.SettingsError, match="no app 'nope'"):
        profile_settings.set_app_entry(w["bailey"], "nope", {}, by=w["jon"])
    with pytest.raises(profile_settings.SettingsError, match="canopy's own"):
        profile_settings.set_app_entry(w["bailey"], "chores", {"tools": ["Bash"]}, by=w["jon"])
    # One entry per app: a second save changes the same entry.
    profile_settings.set_app_entry(w["bailey"], "chores", {"description": "again"}, by=w["jon"])
    w["bailey"].refresh_from_db()
    assert app_entries.entries(w["bailey"].interface) == {w["key"]: "chores", w["garden_key"]: "garden"}
    assert profile_settings.document_problems(w["bailey"].interface["capabilities"]) == []


def test_the_routes_read_change_and_remove_an_entry_for_an_admin_only(w):
    base = "/api/agents/bailey/app-entries"
    got = _client(w["viewer"]).get(base)
    assert got.status_code == 200
    assert {e["app"] for e in got.json()["entries"]} == {"chores", "garden"}
    assert set(got.json()["available"]) == {"chores", "garden"}
    assert _client(w["viewer"]).put(f"{base}/chores", {"description": "x"}, content_type=J).status_code == 403
    r = _client(w["jon"]).put(f"{base}/chores", {"ceiling": ["app_tasks_act"]}, content_type=J)
    assert r.status_code == 200, r.content
    assert _client(w["jon"]).put(f"{base}/chores", {"ceiling": ["Bash"]}, content_type=J).status_code == 422
    assert _client(w["jon"]).delete(f"{base}/garden").status_code == 200
    assert _client(w["jon"]).delete(f"{base}/garden").status_code == 404


# --- selection: the access list decides, the conversation only selects ----------------------


def test_a_use_line_only_person_gets_the_app_entry_on_every_channel_and_never_more(w):
    _line(w["bailey"], w["addy"], capability=w["key"])
    _line(w["bailey"], w["viewer"], capability=w["key"])
    for person in (w["addy"], w["viewer"]):
        # The app's chat panel, any other chat, and a question with no conversation at all.
        for session in (_app_session(w, person), None):
            ctx = _ask(w, person, session)
            assert (ctx.access, ctx.stamped, ctx.profile.kind) == ("confined", w["key"], turn_context.APP_ENTRY)
            assert ctx.held_permissions == [roles.use(w["key"])]
            assert ctx.profile.app == "marshellis/chores"
        # Naming read mode or the whole agent opens nothing.
        assert turn_context.for_asker(w["bailey"], person, verified=True, requested="ask").refused
    # Email: her own proven mail resolves to her, and runs in the entry.
    turn = _email(w, "addy@jacksonhome.org")
    assert turn.initiator_user_id == w["addy"].pk and turn.capability == w["key"]
    env = caller_context.build(turn)
    assert env["access"] == "confined" and env["granted_by"] == f"capability:{w['key']}"
    assert env["app"]["workspace"] == "marshellis" and env["app"]["key"] == "chores"
    assert env["capability"]["name"] == w["key"]


def test_a_read_line_gets_the_app_entry_inside_the_app_and_read_mode_elsewhere(w):
    _line(w["bailey"], w["viewer"], role=roles.READ_ONLY)
    inside = _ask(w, w["viewer"], _app_session(w, w["viewer"]))
    assert (inside.access, inside.stamped) == ("confined", w["key"])
    elsewhere = _ask(w, w["viewer"])
    assert elsewhere.access == "confined" and pp.is_read_mode(elsewhere.stamped)
    assert pp.is_read_mode(_email(w, "viewer@marshellis.com").capability)


def test_selection_never_grants(w):
    # A member with no line, in a conversation held in the app: refused, whatever the stamp.
    assert _ask(w, w["viewer"], _app_session(w, w["viewer"])).refused
    assert _email(w, "addy@jacksonhome.org").status == Turn.CANCELLED   # a guest no line names
    # A line for ANOTHER app's entry does not open this one.
    _line(w["bailey"], w["viewer"], capability=w["garden_key"])
    ctx = _ask(w, w["viewer"], _app_session(w, w["viewer"]))
    assert ctx.stamped == w["garden_key"]          # her own line's entry, never the conversation's


def test_write_and_the_owner_keep_the_whole_agent_in_the_app(w):
    _line(w["bailey"], w["editor"], role=roles.READ_WRITE)
    for person in (w["jon"], w["editor"]):
        ctx = _ask(w, person, _app_session(w, person))
        assert (ctx.access, ctx.stamped) == ("full", "")


def test_a_stamp_survives_the_claim_and_a_removed_entry_denies_all(w):
    _line(w["bailey"], w["addy"], capability=w["key"])
    turn = _email(w, "addy@jacksonhome.org")
    assert turn_context.at_claim(turn).stamped == w["key"]
    profile_settings.remove_app_entry(w["bailey"], "chores", by=w["jon"])
    w["bailey"].refresh_from_db()
    turn.refresh_from_db()
    assert turn_context.of_turn(turn, w["bailey"]).profile.kind == turn_context.DENY_ALL
    assert turn_context.at_claim(turn).refused


# --- the tools: one app, as the verified asker -----------------------------------------------


@contextlib.contextmanager
def _as_token(raw):
    """The caller token through the real verifier, as the mounted server sees it."""
    access = async_to_sync(CanopyPATVerifier().verify_token)(raw)
    assert access is not None, "the token did not verify"
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield access
    finally:
        auth_context_var.reset(tok)


def _call(name, args=None):
    return async_to_sync(mcp.call_tool)(name, args or {}).structured_content


def _names():
    return {t.name for t in async_to_sync(mcp.list_tools)()}


def test_the_confined_token_lists_exactly_the_entrys_tools(w):
    _line(w["bailey"], w["addy"], capability=w["key"])
    turn = _email(w, "addy@jacksonhome.org")
    with _as_token(caller_tokens.mint(turn)):
        names = _names()
    assert {n for n in names if not n.startswith("page_")} == {"who_is_asking", "current_page", *APP_TOOLS}
    # Nothing else canopy serves is callable, a write or a read.
    with _as_token(caller_tokens.mint(turn)):
        for other in ("list_tasks", "act_on_task", "site_call", "query_workspace_app", "send_mail"):
            with pytest.raises(ToolError, match="not part of"):
                _call(other)


def test_a_guest_checks_off_a_chore_by_email_recorded_as_the_agent_for_her(w):
    _line(w["bailey"], w["addy"], capability=w["key"])
    turn = _email(w, "addy@jacksonhome.org")
    with _as_token(caller_tokens.mint(turn)):
        about = _call("app_query")
        assert about["app"]["key"] == "chores" and about["your_roles"] == ["helper"]
        assert [t["key"] for t in _call("app_tasks_query")["tasks"]] == [w["trash"].key]
        done = _call("app_tasks_act", {"name": "check", "key": w["trash"].key})
        assert done["task"]["status"] == AgentTask.DONE
        note = _call("app_act", {"action": "add_note", "input": {"text": "bins out"}})
    event = TaskEvent.objects.filter(task=w["trash"], kind=TaskEvent.ACTION).latest("pk")
    assert event.action.by == "bailey for addy@jacksonhome.org"
    assert AppRecord.objects.get(pk=note["id"]).created_by_label == "bailey for addy@jacksonhome.org"


def test_the_app_tools_refuse_outside_an_app_entry(w):
    _line(w["bailey"], w["viewer"], role=roles.READ_ONLY)
    read_turn = _email(w, "viewer@marshellis.com")
    with _as_token(caller_tokens.mint(read_turn)):
        assert not APP_TOOLS & _names()                 # read mode lists none of them
        with pytest.raises(ToolError):
            _call("app_tasks_query")
    # A colleague's full turn (a scoped, read-only token) may not reach them either.
    _line(w["bailey"], w["editor"], role=roles.READ_WRITE)
    full = _chat_turn(w, w["editor"], chat.create_session(workspace=w["ws"], created_by=w["editor"],
                                                          agent=w["bailey"], title="t"))
    assert full.capability == "" and caller_tokens.is_caller_scoped(full)
    with _as_token(caller_tokens.mint(full, scoped=True)):
        with pytest.raises(ToolError, match="confined to an app entry"):
            _call("app_tasks_query")


def test_the_app_tools_refuse_an_unproven_asker(w):
    allow(w["bailey"], capability=w["key"], contact=True)    # anyone who writes in may ask, unproven
    turn, _ = services.enqueue_turn(
        agent=w["bailey"], origin=Turn.ORIGIN_EMAIL, idempotency_key="email-spoof", prompt="/bailey:turn",
        origin_ref={"from": "addy@jacksonhome.org", "thread_id": "t-spoof", "subject": "trash"})
    assert turn.capability == w["key"] and turn.initiator_user_id is None
    with _as_token(caller_tokens.mint(turn)):
        with pytest.raises(ToolError, match="verified person"):
            _call("app_tasks_act", {"name": "check", "key": w["trash"].key})
    w["trash"].refresh_from_db()
    assert w["trash"].status != AgentTask.DONE


def test_the_app_comes_from_the_entry_never_an_argument(w):
    _line(w["bailey"], w["addy"], capability=w["garden_key"])
    turn = _email(w, "addy@jacksonhome.org")
    assert turn.capability == w["garden_key"]
    with _as_token(caller_tokens.mint(turn)):
        # Bound to garden, where she holds no role: nothing of chores is reachable from here.
        with pytest.raises(ToolError, match="no role in the app 'garden'"):
            _call("app_query")
        with pytest.raises(FastMCPValidationError):     # there is no `app` to name
            _call("app_tasks_act", {"name": "check", "key": w["trash"].key, "app": "chores"})
        with pytest.raises(ToolError):
            _call("app_tasks_act", {"name": "check", "key": w["trash"].key})
    w["trash"].refresh_from_db()
    assert w["trash"].status != AgentTask.DONE


def test_a_use_only_person_reaches_neither_read_mode_nor_the_whole_agent_anywhere(w):
    _line(w["bailey"], w["addy"], capability=w["key"])
    for requested in (None, "ask", "read", pp.SESSION_REVIEW):
        ctx = turn_context.for_asker(w["bailey"], w["addy"], verified=True, requested=requested)
        assert ctx.refused or (ctx.access, ctx.stamped) == ("confined", w["key"])
        assert ctx.access != "full"
    assert pp.offered_to(w["addy"], w["bailey"]) == [w["key"]]


# --- the guest: named on the list, heard by email, in the app's own chat ---------------------


def test_a_guest_is_nameable_by_email_for_one_entry_and_never_a_role(w):
    base = "/api/agents/bailey/access-list"
    r = _client(w["jon"]).post(base, {"kind": "person", "email": "addy@jacksonhome.org", "host_site": w["key"]},
                               content_type=J)
    assert r.status_code == 201, r.content
    r = _client(w["jon"]).post(base, {"kind": "person", "email": "addy@jacksonhome.org", "role": "read_only"},
                               content_type=J)
    assert r.status_code == 422
    r = _client(w["jon"]).post(base, {"kind": "person", "email": "stranger@elsewhere.org", "host_site": w["key"]},
                               content_type=J)
    assert r.status_code == 422


def test_a_guests_mail_is_hers_only_when_the_list_names_her(w):
    assert _email(w, "addy@jacksonhome.org", "a").initiator_user_id is None
    _line(w["bailey"], w["addy"], capability=w["key"])
    assert _email(w, "addy@jacksonhome.org", "b").initiator_user_id == w["addy"].pk
    _line(w["bailey"], w["stranger"], capability=w["key"])     # named, but no guest of the workspace
    assert _email(w, "stranger@elsewhere.org", "c").initiator_user_id is None


def _panel_app():
    owner = User.objects.create_user("site-admin", "site-admin@dimagi.com", "pw")
    host = Workspace.objects.create(slug="canopyhost", display_name="Host", created_by=owner)
    app = AppCredential.create_credential(name="canopy-web", created_by=owner, workspace=host)
    app.show_on_canopy_pages = True
    app.save(update_fields=["show_on_canopy_pages"])
    return app


def _mint(user, **query):
    q = "&".join(f"{k}={v}" for k, v in query.items())
    r = _client(user).post(f"/api/embed/token?{q}", {}, content_type=J)
    assert r.status_code == 200, r.content
    return r.json()["token"]


def test_the_panel_on_an_app_page_stamps_the_conversation_for_who_may_open_the_app(w):
    _panel_app()
    _line(w["bailey"], w["addy"], capability=w["key"])
    raw = _mint(w["addy"], workspace="marshellis", app="chores")
    assert DelegatedToken.lookup(raw).page_app == "marshellis/chores"
    # An app she is not a guest of, or a stranger naming hers: no app at all.
    assert DelegatedToken.lookup(_mint(w["addy"], workspace="marshellis", app="garden")).page_app == ""
    assert DelegatedToken.lookup(_mint(w["stranger"], workspace="marshellis", app="chores")).page_app == ""

    guest = bearer_client(raw)
    agents = guest.get("/api/embed/agents")
    assert [a["slug"] for a in agents.json()] == ["bailey"]
    made = guest.post("/api/canopy-sessions/", {"agent_slug": "bailey",
                                                "metadata": {"workspace_app": "marshellis/garden"}},
                      content_type=J)
    assert made.status_code == 200, made.content
    session = Session.objects.get(pk=made.json()["id"])
    # Server-stamped from the token; the body's own value was dropped.
    assert session.metadata["workspace_app"] == "marshellis/chores" and session.workspace_id == "marshellis"
    assert session_access.can_read(w["addy"], session)
    assert guest.get(f"/api/canopy-sessions/{session.pk}").status_code == 200
    assert _ask(w, w["addy"], session).stamped == w["key"]
    sent = guest.post(f"/api/canopy-sessions/{session.pk}/send", {"text": "did the trash", "client_id": "c1"},
                      content_type=J)
    assert sent.status_code in (200, 201, 202), sent.content
    turn = Turn.objects.get(chat_session=session)
    assert (turn.initiator_user_id, turn.capability) == (w["addy"].pk, w["key"])
    # Her app's conversation is hers alone: a member reads it only as any other chat they are not in.
    assert not session_access.can_read(w["viewer"], session)


def test_a_guest_starts_no_chat_outside_her_app(w):
    _panel_app()
    other = Agent.objects.create(slug="other", name="Other", workspace=w["ws"], owner=w["jon"])
    guest = bearer_client(_mint(w["addy"], workspace="marshellis", app="chores"))
    r = guest.post("/api/canopy-sessions/", {"agent_slug": other.slug}, content_type=J)
    assert r.status_code == 404
    # With no app on the token, a guest has no workspace to start one in.
    plain = bearer_client(_mint(w["addy"], workspace="marshellis"))
    assert plain.post("/api/canopy-sessions/", {"agent_slug": "bailey"}, content_type=J).status_code in (404, 422)


def test_the_guest_can_use_the_task_routes_of_her_app_only(w):
    c = _client(w["addy"])
    assert c.get("/api/workspace-apps/marshellis/chores/tasks").status_code == 200
    assert c.get("/api/workspace-apps/marshellis/garden/tasks").status_code == 404


def test_the_runner_writes_no_bash_or_read_beyond_the_entry(w):
    from canopy_runner.native_permissions import settings_for  # noqa: PLC0415 — the runner's own renderer

    p = pp.profile(w["bailey"], w["key"])
    rules = settings_for(p, worktree="/tmp/wt", thread_id="th-1",
                         servers={"plugin_canopy_canopy-web", "plugin_bailey_gmail"})
    bash = [a for a in rules["allow"] if a.startswith("Bash(")]
    assert len(bash) == 3 and all("th-1" in b or "review-receipt" in b for b in bash)
    reads = [a for a in rules["allow"] if a.startswith("Read(")]
    assert reads and all(".bailey-" in r for r in reads)
    assert "mcp__plugin_bailey_gmail" in rules["deny"]
    assert "mcp__plugin_canopy_canopy-web" not in rules["deny"]
    assert any(fnmatch.fnmatchcase("mcp__plugin_canopy_canopy-web__app_tasks_act", a)
               for a in rules["allow"] if a.startswith("mcp__"))
