"""`manage.py answering_snapshot`: what it holds, and that it can only read.

Driven through `call_command`, the way it is run. It is meant to be pointed at production, so being
read-only and leak-free is the feature: these tests seed secrets, count rows and watch every statement.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.admission.models import AdmissionRule
from apps.agents import answering_snapshot, roles
from apps.agents.models import Agent, AgentAdmin, AgentGrant, AgentRole
from apps.events.models import Activity, SettingChange
from apps.harness.models import Runner, RunnerAssignment, RunnerFlag, Turn
from apps.harness.testing import enrolled
from apps.host_grants import client_identity
from apps.tokens.models import AppCredential, AppCredentialAgent, PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "answering_interfaces.json"
SITE = "connect-labs"


def _interface(slug: str) -> dict:
    rows = json.loads(FIXTURE.read_text())["agents"]
    return next(r["interface"] for r in rows if r["slug"] == slug)


@pytest.fixture()
def w():
    user_model = get_user_model()
    owner = user_model.objects.create_user("jo", "jo@dimagi.com", "pw", first_name="Jo", last_name="Owner")
    admin = user_model.objects.create_user("adm", "adm@dimagi.com", "pw")
    reader = user_model.objects.create_user("read", "read@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    for person in (admin, reader):
        WorkspaceMembership.objects.create(user=person, workspace=ws, role=WorkspaceMembership.VIEWER)
    published = dt.datetime(2026, 10, 23, 3, 10, 37, tzinfo=dt.UTC)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=owner, email="ace@dimagi-ai.com",
                               interface=_interface("ace"), interface_published_by=owner,
                               interface_published_at=published)
    labsdata = Agent.objects.create(slug="labsdata", name="labsdata", workspace=ws, owner=owner,
                                    interface=_interface("labsdata"), interface_published_by=owner,
                                    interface_published_at=published)
    plain = Agent.objects.create(slug="plain", name="Plain", workspace=ws, owner=owner)
    AgentAdmin.objects.create(agent=ace, user=admin)
    AgentGrant.objects.create(agent=ace, kind=AgentGrant.PERSON, user=reader, role=roles.READ_ONLY)
    AgentGrant.objects.create(agent=ace, kind=AgentGrant.CONTACT, capability="connect", verified=False)
    app = AppCredential.create_credential(name=SITE, created_by=owner, workspace=ws)
    for agent in (ace, labsdata, plain):
        AppCredentialAgent.objects.create(app=app, agent=agent)
    box = enrolled(Runner.objects.create(
        name="sensitive-box", kind=Runner.EMDASH, host="box", owner=owner, status=Runner.ONLINE, ready=True,
        last_heartbeat_at=timezone.now(), capabilities={"data_mode": "sensitive_data"}))
    RunnerFlag.objects.create(runner=box, flag="sensitive_data", declared_by=owner)
    RunnerAssignment.objects.create(agent=labsdata, runner=box, rank=1, enabled=True)
    return {"owner": owner, "ace": ace, "labsdata": labsdata, "plain": plain, "app": app, "box": box}


def _run(*args) -> str:
    out = io.StringIO()
    call_command("answering_snapshot", *args, stdout=out)
    return out.getvalue()


def _doc() -> dict:
    return json.loads(_run("--json"))


def test_it_holds_everything_the_spec_lists(w):
    doc = _doc()
    assert set(doc) == {"agents", "sites", "topology", "shape"}
    ace, labsdata, plain = doc["agents"]["ace"], doc["agents"]["labsdata"], doc["agents"]["plain"]

    # profile() per stamp: the full profile, read mode either way, each host site, and a retired name.
    assert ace["profile"][""] is None
    assert ace["profile"]["connect"]["tools"][-1] == "mcp__*canopy-web__page_*"
    assert ace["profile"]["read"]["name"] == "read"
    assert ace["profile"]["ask"]["entry"] == "/ace:ask --thread {thread_id}"
    assert ace["profile"]["retired_probe"]["tools"] == []
    assert plain["profile"]["read"]["default_profile_version"] == 4

    # decide(): each posture, in each kind of conversation.
    assert ace["decide"]["owner"]["chat"]["(free-form)"]["access"] == "full"
    assert ace["decide"]["admin"]["no_turn"]["(free-form)"]["basis"] == "admin"
    assert ace["decide"]["editor"][f"site:{SITE}"]["(free-form)"]["access"] == "full"
    assert ace["decide"]["read_only"][f"site:{SITE}"]["(free-form)"]["capability"] == "ask"
    assert ace["decide"]["contact_use_connect"][f"site:{SITE}"]["(free-form)"]["capability"] == "connect"
    assert ace["decide"]["contact_use_connect"]["chat"]["(free-form)"]["access"] == "none"
    assert plain["decide"]["read_only"]["chat"]["(free-form)"]["capability"] == "read"
    # The sensitive rule, which confines even the owner and refuses every other way in.
    assert labsdata["decide"]["owner"][f"site:{SITE}"]["(free-form)"]["basis"] == "sensitive:data"
    assert labsdata["decide"]["owner"]["sensitive_chat"]["(free-form)"]["access"] == "none"
    # Sensitive data is about the conversation on that site: the owner's ordinary chat is the whole agent.
    assert labsdata["decide"]["owner"]["chat"]["(free-form)"]["access"] == "full"

    # What the site-agents API would sign, for the registered site.
    listed = {e["slug"]: e for e in doc["sites"][f"connect/{SITE}"]["agents"]}
    assert listed["labsdata"]["capabilities"][0]["handles_sensitive"] is True
    assert listed["ace"]["capabilities"][0]["name"] == "connect"
    assert [p["url"].rsplit("#")[-1] for p in listed["plain"]["problem_links"]] == ["reach"]

    # facts_for: only for an agent with sensitive data, on no runner and on the box that takes it.
    assert "facts_for" not in ace and "facts_for" not in plain
    statement = labsdata["facts_for"]["sensitive-box"]["data"]
    assert statement["declaration"]["declared_at"] == "2026-10-23T03:10:37+00:00"
    assert statement["runner"]["flags"][0]["flag"] == "sensitive_data"
    assert labsdata["facts_for"]["(no runner)"]["data"]["runner"]["id"] == ""

    # A2A: the public card is whatever outsiders reach, and an extended card per answer.
    assert [s["id"] for s in ace["a2a"]["public"]["skills"]] == ["connect"]
    assert {tuple(c["profiles"]) for c in ace["a2a"]["extended"]} >= {("ask",)}
    assert plain["a2a"]["public"] is None

    # Topology edges, per tree.
    assert {(e["source"], e["target"]) for e in doc["topology"]["connect"]} >= {("ace", "plain")}


def test_two_runs_print_the_same_bytes_even_as_a_boxs_heartbeat_ages(w):
    first = _run("--json")
    assert first == _run("--json")
    # Two days on, the box is no longer available. Nothing about how an agent answers has changed.
    later = timezone.now() + dt.timedelta(days=2)
    with mock.patch("django.utils.timezone.now", return_value=later):
        assert first == _run("--json")
    # The same comparison a reader of the two hashes makes.
    assert _run() == _run()
    assert _run().split()[0] == answering_snapshot.digest(json.loads(first))


def test_the_hash_moves_when_an_answer_does(w):
    before = _run().split()[0]
    w["ace"].interface["capabilities"]["connect"]["ceiling"] = ["mcp__*connect_labs__narrower"]
    w["ace"].save(update_fields=["interface"])
    assert _run().split()[0] != before


def test_it_only_reads(w):
    counted = (Agent, AgentGrant, Activity, SettingChange, Turn, PersonalToken)
    before = {m: m.objects.count() for m in counted}
    agent_before = Agent.objects.filter(pk=w["ace"].pk).values().get()
    with CaptureQueriesContext(connection) as ctx:
        _run("--json")
    statements = [q["sql"].lstrip().lower() for q in ctx.captured_queries]
    allowed = ("select", "(select", "savepoint", "release savepoint", "rollback to savepoint", "set transaction")
    assert statements and not [s for s in statements if not s.startswith(allowed)]
    assert not [s for s in statements if " for update" in s]
    assert {m: m.objects.count() for m in counted} == before
    assert Agent.objects.filter(pk=w["ace"].pk).values().get() == agent_before


def test_it_signs_mints_and_emits_nothing(w):
    refuse = mock.Mock(side_effect=AssertionError("the snapshot must not do this"))
    with (mock.patch.object(client_identity, "sign_site_agents", refuse),
          mock.patch.object(client_identity, "execution_statement", refuse),
          mock.patch.object(client_identity, "client_assertion", refuse),
          mock.patch("jwt.encode", refuse),
          mock.patch.object(PersonalToken, "create_for_user", refuse),
          mock.patch("apps.events.activity.emit", refuse)):
        _run("--json")
    refuse.assert_not_called()


def test_a_write_during_the_snapshot_is_refused_and_leaves_nothing(w):
    # The lock has to hold for code that has not been written yet, so the write here is a real one.
    def writes():
        Agent.objects.filter(pk=w["ace"].pk).update(description="written by the snapshot")
        return {}

    with mock.patch.object(answering_snapshot, "_build", writes), pytest.raises(CommandError, match="not a read"):
        _run("--json")
    w["ace"].refresh_from_db()
    assert w["ace"].description != "written by the snapshot"


def test_no_stored_secret_reaches_the_output(w):
    raw, _token = PersonalToken.create_for_user(user=w["owner"], label="seeded")
    w["app"].public_keys = ["-----BEGIN PUBLIC KEY-----\nseeded\n-----END PUBLIC KEY-----"]
    w["app"].save()
    AgentGrant.objects.filter(agent=w["ace"], kind=AgentGrant.PERSON).update(note="cpat_seeded_in_a_note")
    printed = _run("--json")
    for secret in (raw, "seeded_in_a_note", "BEGIN PUBLIC KEY"):
        assert secret not in printed


def test_it_refuses_to_print_a_token_someone_pasted_into_a_profile(w):
    # A profile is free text an admin wrote: a command with a key in it would be copied into the output.
    w["ace"].interface["capabilities"]["ask"]["bash"].append("wget --header 'X-Key: cpat_pastedvalue123'")
    w["ace"].save(update_fields=["interface"])
    with pytest.raises(CommandError) as exc:
        _run("--json")
    assert "agents/ace/profile/ask/bash" in str(exc.value)
    assert "pastedvalue" not in str(exc.value)


def test_shape_is_empty_for_agents_that_fit_the_plain_forms(w):
    assert _doc()["shape"]["agent_problems"] == {}


def test_shape_names_each_thing_the_plain_forms_cannot_hold(w):
    both = {"sites": ["a", "b"], "tools": ["Read"]}
    w["plain"].interface = {"capabilities": {
        "ask": {"tools": ["Read"], "sites": ["a"]}, "two": both, "bare": {"tools": ["Read"]},
        "again": {"sites": ["a"]}}}
    w["plain"].save(update_fields=["interface"])
    AgentGrant.objects.create(agent=w["ace"], kind=AgentGrant.CONTACT, capability="gone", verified=False)
    AgentRole.objects.create(workspace=w["ace"].workspace, key="site_role", label="Site", permissions=["use:connect"])
    AdmissionRule.objects.create(workspace=w["ace"].workspace, reason="r", capability="connect")
    shape = _doc()["shape"]
    assert shape["agent_problems"]["plain"] == [
        "a: named by again, ask, two", "ask: read mode names sites, a ceiling, a login or sensitive data",
        "bare: has no site", "two: names 2 sites"]
    assert shape["lines_naming_nothing_published"] == ["ace: gone"]
    assert shape["roles_naming"] == ["use:connect"]
    assert shape["admission_rules_naming"] == ["connect"]
