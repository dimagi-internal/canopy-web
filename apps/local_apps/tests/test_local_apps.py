"""The promises local apps make: nothing runs on the server; nothing reaches anyone
unreviewed; a revoked version stops everywhere; and the runner's token gains no general
way to act as its owner."""

from __future__ import annotations

import base64
import io
import json
import uuid
import zipfile

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.approvals import services as approvals
from apps.approvals.models import ApprovalRequest
from apps.canopy_sessions.models import Session
from apps.harness.models import Runner, Turn
from apps.local_apps import package as pkg
from apps.local_apps import services, trust
from apps.local_apps.models import LocalAppInstallation, LocalAppRun, LocalAppSubmission
from apps.workspace_apps import services as web_store
from apps.workspace_apps.models import AppTemplate
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
J = "application/json"


@pytest.fixture(autouse=True)
def _approval_is_on(settings):
    settings.LOCAL_APPS_REQUIRE_APPROVAL = True


def manifest(**over) -> dict:
    return {
        "key": "meeting-assistant",
        "name": "Meeting assistant",
        "description": "Feedback in a meeting.",
        "version": "0.1.0",
        "changelog": "First.",
        "entry": ["{python}", "app.py"],
        "platforms": ["macos"],
        "max_minutes": 120,
        "notice": "It leaves your Mac.",
        "disclosure": {"reads": ["a transcript"], "sends": ["text to a session"]},
        "settings": {
            "agent": {"type": "agent", "label": "Agent", "required": True},
            "brief": {"type": "longtext", "label": "Brief"},
        },
        "checks": [],
        "canopy": ["sessions.create", "sessions.send", "turns.status"],
        **over,
    }


def package(m: dict | None = None, *, files: dict | None = None) -> bytes:
    body = {"manifest.json": json.dumps(m or manifest()), "app.py": "print('hi')\n", **(files or {})}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, text in body.items():
            # A fixed entry time: writestr(name, ...) stamps "now" at 2-second resolution, so two
            # packages of the same files built across a tick differ in bytes (and in sha256).
            z.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), text)
    return out.getvalue()


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def client(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture()
def world():
    root = User.objects.create_superuser("root", "root@dimagi.com", "pw")
    author = User.objects.create_user("author", "author@dimagi.com", "pw")
    person = User.objects.create_user("person", "person@dimagi.com", "pw")
    other = User.objects.create_user("other", "other@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=root, settings={"local_apps": True})
    WorkspaceMembership.objects.create(user=root, workspace=ws, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(user=author, workspace=ws, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(user=person, workspace=ws, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(user=other, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(
        slug="helper",
        name="Helper",
        workspace=ws,
        owner=person,
        user=User.objects.create_user("helper-bot", "helper@dimagi-ai.com", "pw"),
    )
    laptop = Runner.objects.create(name="laptop", kind=Runner.EMDASH, owner=person)
    return dict(ws=ws, root=root, author=author, person=person, other=other, agent=agent, laptop=laptop)


def submit(world, m=None, *, files=None, user=None) -> LocalAppSubmission:
    r = client(user or world["author"]).post(
        "/api/local-apps/submissions/",
        {"package_b64": b64(package(m, files=files)), "workspace": "dimagi"},
        content_type=J,
    )
    assert r.status_code in (200, 201), r.content
    return LocalAppSubmission.objects.get(pk=r.json()["id"])


def approve(world, sub) -> LocalAppSubmission:
    decided = approvals.decide(sub.approval_id, world["root"], "approve")
    assert decided.status == ApprovalRequest.DONE, decided.error
    sub.refresh_from_db()
    return sub


def released(world, m=None, **kw) -> LocalAppSubmission:
    return approve(world, submit(world, m, **kw))


def install(world, sub=None, settings=None) -> LocalAppInstallation:
    sub = sub or LocalAppSubmission.objects.get(key="meeting-assistant", version="0.1.0")
    r = client(world["person"]).post(
        "/api/local-apps/store/apps/meeting-assistant/install",
        {
            "runner_id": str(world["laptop"].pk),
            "settings": settings or {"agent": "helper"},
            "accept_digest": trust.digest(sub.manifest),
        },
        content_type=J,
    )
    assert r.status_code == 201, r.content
    return LocalAppInstallation.objects.get(pk=r.json()["id"])


def runner_base(world) -> str:
    return f"/api/harness/runners/{world['laptop'].pk}/local-apps"


def start(world, version="0.1.0") -> str:
    r = client(world["person"]).post(
        f"{runner_base(world)}/meeting-assistant/runs/", {"version": version}, content_type=J
    )
    assert r.status_code == 201, r.content
    return r.json()["run_id"]


def call(world, run, op, args=None) -> dict:
    r = client(world["person"]).post(
        f"{runner_base(world)}/runs/{run}/canopy", {"op": op, "args": args or {}}, content_type=J
    )
    assert r.status_code == 200, r.content
    return r.json()


def beat(world, run) -> dict:
    return (
        client(world["person"])
        .post(
            f"{runner_base(world)}/runs/{run}/heartbeat", {"state": "listening", "counts": {"sent": 1}}, content_type=J
        )
        .json()
    )


# --- packages: never run, only read ------------------------------------------------------


@pytest.mark.parametrize("name", ["../evil.py", "/etc/evil.py", "a/../../evil.py", "a\\evil.py"])
def test_a_path_that_climbs_out_is_refused(name):
    with pytest.raises(pkg.BadPackage):
        pkg.read(package(files={name: "x"}))


def test_a_symlink_is_refused():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest()))
        z.writestr("app.py", "x")
        link = zipfile.ZipInfo("link")
        link.external_attr = 0o120777 << 16
        z.writestr(link, "/etc/passwd")
    with pytest.raises(pkg.BadPackage, match="symlink"):
        pkg.read(out.getvalue())


def test_a_binary_is_refused_because_a_reviewer_cant_read_it():
    with pytest.raises(pkg.BadPackage, match="binary"):
        pkg.read(package(files={"blob.so": "\0\0\0"}))


def test_an_entry_program_that_isnt_there_is_refused():
    with pytest.raises(pkg.BadPackage, match="isn't in the package"):
        pkg.read(package(manifest(entry=["{python}", "missing.py"])))


def test_a_declared_size_over_the_limit_is_refused_before_inflating(monkeypatch):
    monkeypatch.setattr(pkg, "MAX_UNPACKED", 10)
    with pytest.raises(pkg.BadPackage, match="unpacks to over"):
        pkg.read(package())


def test_facts_point_the_reviewer_at_what_reaches_outside():
    p = pkg.read(package(files={"app.py": "import urllib.request\nimport subprocess, os\nos.system('x')\neval('1')\n"}))
    facts = pkg.facts(p)
    assert facts["network"] == {"urllib": ["app.py"]}
    assert set(facts["process"]) == {"subprocess", "os.system"}
    assert facts["risky"] == {"eval": ["app.py"]}


def test_unknown_manifest_keys_are_refused():
    with pytest.raises(pkg.BadPackage, match="unknown keys"):
        pkg.read(package({**manifest(), "sneaky": True}))


# --- submit, approve, release ------------------------------------------------------------


def test_a_submission_reaches_nobody_until_approved(world):
    sub = submit(world)
    assert sub.status == ApprovalRequest.PENDING
    assert services.catalog() == []
    assert client(world["person"]).get("/api/local-apps/store/").json()["apps"] == []
    approve(world, sub)
    assert [a["key"] for a in client(world["person"]).get("/api/local-apps/store/").json()["apps"]] == [
        "meeting-assistant"
    ]


def test_nobody_approves_their_own_submission(world):
    User.objects.create_superuser("root2", "root2@dimagi.com", "pw")
    sub = submit(world, user=world["root"])
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(sub.approval_id, world["root"], "approve")


def test_a_source_in_the_package_is_part_of_what_people_accept():
    old, new = manifest(source=["{python}", "a.py"]), manifest(source=["{python}", "b.py"])
    assert trust.digest(old) != trust.digest(new)
    assert "Listens through a different program" in trust.changes(old, new)[0]


def test_a_lone_superuser_cant_submit_since_nobody_could_approve_it(world):
    r = client(world["root"]).post(
        "/api/local-apps/submissions/", {"package_b64": b64(package()), "workspace": "dimagi"}, content_type=J
    )
    assert r.status_code == 422 and "nobody can approve" in r.json()["detail"]
    assert not LocalAppSubmission.objects.exists()


def test_an_ordinary_member_cant_approve(world):
    with pytest.raises(approvals.ApprovalError):
        approvals.decide(submit(world).approval_id, world["other"], "approve")


def test_a_declined_version_never_lists(world):
    sub = submit(world)
    approvals.decide(sub.approval_id, world["root"], "decline")
    sub.refresh_from_db()
    assert sub.status == ApprovalRequest.DECLINED and services.catalog() == []


def test_the_approval_preview_names_the_exact_bytes_and_the_code(world):
    req = submit(world).approval
    plan = "\n".join(req.plan)
    assert f"sha256 {req.params['sha256']}" in plan and "/apps/personal/review/" in plan


def test_a_version_never_changes(world):
    submit(world)
    r = client(world["author"]).post(
        "/api/local-apps/submissions/",
        {"package_b64": b64(package(files={"app.py": "other\n"})), "workspace": "dimagi"},
        content_type=J,
    )
    assert r.status_code == 409 and "Versions never" in r.json()["detail"]


def test_resubmitting_the_same_bytes_returns_the_same_submission(world):
    assert submit(world).pk == submit(world).pk
    assert ApprovalRequest.objects.filter(kind="local_app.release").count() == 1


def test_a_version_must_be_newer_than_the_release(world):
    released(world)
    r = client(world["author"]).post(
        "/api/local-apps/submissions/",
        {"package_b64": b64(package(manifest(version="0.0.9"))), "workspace": "dimagi"},
        content_type=J,
    )
    assert r.status_code == 409 and "isn't newer" in r.json()["detail"]


def test_a_viewer_cant_submit(world):
    viewer = User.objects.create_user("viewer", "viewer@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=world["ws"], role=WorkspaceMembership.VIEWER)
    r = client(viewer).post(
        "/api/local-apps/submissions/", {"package_b64": b64(package()), "workspace": "dimagi"}, content_type=J
    )
    assert r.status_code == 403


def test_a_review_shows_the_diff_and_what_changed_in_the_promise(world):
    released(world)
    sub = submit(
        world,
        manifest(version="0.2.0", disclosure={"reads": ["a transcript", "your calendar"], "sends": []}),
        files={"app.py": "print('changed')\n"},
    )
    review = client(world["root"]).get(f"/api/local-apps/submissions/{sub.pk}").json()
    assert review["prior_version"] == "0.1.0"
    assert "Now reads: your calendar" in review["trust_changes"]
    assert any("+print('changed')" in line for f in review["diff"] for line in f["lines"])


def test_only_the_reviewer_or_author_reads_a_submission(world):
    sub = submit(world)
    assert client(world["other"]).get(f"/api/local-apps/submissions/{sub.pk}").status_code == 404
    assert client(world["author"]).get(f"/api/local-apps/submissions/{sub.pk}").status_code == 200


# --- one catalog, two kinds --------------------------------------------------------------


def test_the_web_store_never_sees_a_local_app(world):
    released(world)
    assert AppTemplate.objects.get(key="meeting-assistant").runtime == AppTemplate.LOCAL
    assert "meeting-assistant" not in [a["key"] for a in web_store.store("dimagi")]
    with pytest.raises(LookupError):
        web_store.store_detail("dimagi", "meeting-assistant")


def test_a_local_app_cant_take_a_web_apps_name(world):
    web_store.sync_bundled()
    m = manifest(key="hiring")
    r = client(world["author"]).post(
        "/api/local-apps/submissions/", {"package_b64": b64(package(m)), "workspace": "dimagi"}, content_type=J
    )
    assert r.status_code == 409


def test_a_release_is_an_immutable_catalog_version(world):
    sub = released(world)
    assert sub.release.version == "0.1.0" and sub.release.manifest["name"] == "Meeting assistant"
    assert sub.release.template.owner_id is None


# --- who may install --------------------------------------------


def test_only_your_own_laptop_can_take_an_install(world):
    sub = released(world)
    cloud = Runner.objects.create(name="cloud", kind=Runner.CLOUD, owner=world["person"])
    someone_elses = Runner.objects.create(name="theirs", kind=Runner.EMDASH, owner=world["other"])
    for runner in (cloud, someone_elses):
        r = client(world["person"]).post(
            "/api/local-apps/store/apps/meeting-assistant/install",
            {"runner_id": str(runner.pk), "settings": {"agent": "helper"}, "accept_digest": trust.digest(sub.manifest)},
            content_type=J,
        )
        assert r.status_code == 404


def test_install_needs_the_promise_they_actually_read(world):
    released(world)
    r = client(world["person"]).post(
        "/api/local-apps/store/apps/meeting-assistant/install",
        {"runner_id": str(world["laptop"].pk), "settings": {"agent": "helper"}, "accept_digest": "0" * 64},
        content_type=J,
    )
    assert r.status_code == 409 and "changed while you were reading" in r.json()["detail"]


def test_a_required_setting_is_required(world):
    sub = released(world)
    r = client(world["person"]).post(
        "/api/local-apps/store/apps/meeting-assistant/install",
        {"runner_id": str(world["laptop"].pk), "settings": {}, "accept_digest": trust.digest(sub.manifest)},
        content_type=J,
    )
    assert r.status_code == 422


def test_cant_pick_an_agent_from_a_workspace_youre_not_in(world):
    sub = released(world)
    elsewhere = Workspace.objects.create(slug="elsewhere", display_name="E", created_by=world["root"])
    Agent.objects.create(
        slug="alien",
        name="Alien",
        workspace=elsewhere,
        owner=world["person"],
        user=User.objects.create_user("alien-bot", "alien@dimagi-ai.com", "pw"),
    )
    r = client(world["person"]).post(
        "/api/local-apps/store/apps/meeting-assistant/install",
        {
            "runner_id": str(world["laptop"].pk),
            "settings": {"agent": "alien"},
            "accept_digest": trust.digest(sub.manifest),
        },
        content_type=J,
    )
    assert r.status_code == 403


# --- updates -----------------------------------------------------------------------------


def _listed(world) -> dict:
    return client(world["person"]).get(f"{runner_base(world)}/").json()["apps"][0]


def test_a_fix_reaches_the_next_start_with_no_click(world):
    install(world, released(world))
    released(world, manifest(version="0.1.1"), files={"app.py": "print('fixed')\n"})
    app = _listed(world)
    assert app["version"] == "0.1.1" and app["needs_acceptance"] is False


def test_a_changed_promise_waits_for_acceptance_on_the_old_version(world):
    install(world, released(world))
    new = released(world, manifest(version="0.2.0", disclosure={"reads": ["your calendar"], "sends": []}))
    app = _listed(world)
    assert app["version"] == "0.1.0" and app["needs_acceptance"] is True
    assert "Now reads: your calendar" in app["changes"]
    inst = LocalAppInstallation.objects.get(user=world["person"])
    r = client(world["person"]).post(
        f"/api/local-apps/installations/{inst.pk}/accept", {"digest": trust.digest(new.manifest)}, content_type=J
    )
    assert r.status_code == 200
    assert _listed(world)["version"] == "0.2.0"


# --- revocation and stopping -------------------------------------------------------------


def test_revoking_stops_every_door(world):
    sub = released(world)
    install(world, sub)
    run = start(world)
    assert beat(world, run)["stop"] is False
    session = call(world, run, "sessions.create", {"title": "t"})["body"]["id"]
    r = client(world["root"]).post(
        f"/api/local-apps/submissions/{sub.pk}/revoke", {"reason": "it was bad"}, content_type=J
    )
    assert r.status_code == 200
    assert beat(world, run) == {"stop": True, "reason": "revoked"}
    assert call(world, run, "sessions.send", {"session_id": session, "text": "hi", "client_id": "a"})["status"] == 410
    assert _listed(world)["refusal"]
    assert (
        client(world["person"])
        .post(f"{runner_base(world)}/meeting-assistant/runs/", {"version": "0.1.0"}, content_type=J)
        .status_code
        == 403
    )
    assert (
        client(world["person"]).get(f"{runner_base(world)}/meeting-assistant/package", {"version": "0.1.0"}).status_code
        == 404
    )


def test_only_a_superuser_revokes(world):
    sub = released(world)
    assert (
        client(world["author"])
        .post(f"/api/local-apps/submissions/{sub.pk}/revoke", {"reason": "x"}, content_type=J)
        .status_code
        == 403
    )


def test_revoking_the_newest_falls_back_to_the_last_good_version(world):
    install(world, released(world))
    bad = released(world, manifest(version="0.1.1"))
    assert _listed(world)["version"] == "0.1.1"
    client(world["root"]).post(f"/api/local-apps/submissions/{bad.pk}/revoke", {"reason": "bad"}, content_type=J)
    assert _listed(world)["version"] == "0.1.0"


def test_stop_from_the_website_reaches_the_next_heartbeat_and_it_can_never_start_one(world):
    install(world, released(world))
    run = start(world)
    assert client(world["other"]).post(f"/api/local-apps/runs/{run}/stop").status_code == 404
    assert client(world["person"]).post(f"/api/local-apps/runs/{run}/stop").status_code == 200
    assert beat(world, run) == {"stop": True, "reason": "stopped_from_website"}


def test_removing_an_app_answers_the_next_heartbeat_404(world):
    inst = install(world, released(world))
    run = start(world)
    assert client(world["person"]).delete(f"/api/local-apps/installations/{inst.pk}").status_code == 204
    r = client(world["person"]).post(f"{runner_base(world)}/runs/{run}/heartbeat", {}, content_type=J)
    assert r.status_code == 404


def test_a_heartbeat_keeps_numbers_only(world):
    install(world, released(world))
    run = start(world)
    client(world["person"]).post(
        f"{runner_base(world)}/runs/{run}/heartbeat",
        {"state": "x", "counts": {"sent": 3, "said": "the salary is 90k"}},
        content_type=J,
    )
    assert LocalAppRun.objects.get(pk=run).counts == {"sent": 3}


# --- the runner's token gains no general way to act as its owner -------------------------


def test_a_run_reaches_only_sessions_it_opened(world):
    install(world, released(world))
    run = start(world)
    mine = Session.objects.create(workspace=world["ws"], title="private")
    r = call(world, run, "sessions.send", {"session_id": str(mine.pk), "text": "hi", "client_id": "a"})
    assert r["status"] == 403
    assert call(world, run, "turns.status", {"turn_id": str(uuid.uuid4())})["status"] == 403


def test_a_run_gets_only_ops_its_manifest_declares(world):
    sub = released(world, manifest(canopy=["sessions.create"]))
    install(world, sub)
    assert call(world, start(world), "sessions.send", {})["status"] == 403


def test_the_agent_comes_from_the_installation_not_the_call(world):
    install(world, released(world))
    run = start(world)
    body = call(world, run, "sessions.create", {"title": "t", "agent": "something-else"})["body"]
    session = Session.objects.get(pk=body["id"])
    assert session.agent_id == world["agent"].pk and session.workspace_id == "dimagi"
    assert "/w/dimagi/" in body["url"]
    assert (
        LocalAppInstallation.objects.get(user=world["person"], template__key="meeting-assistant").workspace_id
        == "dimagi"
    )


def test_a_send_starts_a_turn_as_the_person_and_a_retry_collapses(world):
    install(world, released(world))
    run = start(world)
    session = call(world, run, "sessions.create", {"title": "t"})["body"]["id"]
    args = {"session_id": session, "text": "batch one", "client_id": "same"}
    first = call(world, run, "sessions.send", args)
    assert first["status"] == 200 and first["body"]["turn_id"]
    assert call(world, run, "sessions.send", args)["body"]["turn_id"] == first["body"]["turn_id"]
    turn = Turn.objects.get(pk=first["body"]["turn_id"])
    assert turn.chat_session_id is not None
    status = call(world, run, "turns.status", {"turn_id": first["body"]["turn_id"]})
    assert status["status"] == 200 and status["body"]["status"]


def test_someone_who_administers_the_box_cant_reach_its_owners_apps(world):
    install(world, released(world))
    assert client(world["root"]).get(f"{runner_base(world)}/").status_code == 404


def test_losing_access_to_the_agent_stops_the_next_start(world, monkeypatch):
    install(world, released(world))
    monkeypatch.setattr(services, "_usable_agent", lambda *a, **k: (_ for _ in ()).throw(services.Refused("no", 403)))
    assert _listed(world)["refusal"] == "no"


def test_a_real_runner_token_reaches_its_routes(world):
    """Through `runner_auth`, not a person's login. No new door was needed."""
    from apps.harness.runner_auth import reaches

    rid = world["laptop"].pk
    assert reaches("GET", f"/api/harness/runners/{rid}/local-apps", rid)
    assert reaches("POST", f"/api/harness/runners/{rid}/local-apps/runs/{uuid.uuid4()}/canopy", rid)
    assert not reaches("POST", "/api/local-apps/submissions", rid)
    assert not reaches("POST", f"/api/canopy-sessions/{uuid.uuid4()}/send", rid)


# --- what independent review found -------------------------------------------------------


def test_losing_the_agent_mid_run_stops_it(world, monkeypatch):
    install(world, released(world))
    run = start(world)
    monkeypatch.setattr(services, "_usable_agent", lambda *a, **k: (_ for _ in ()).throw(services.Refused("no", 403)))
    assert beat(world, run) == {"stop": True, "reason": "no_longer_allowed_to_use_agent"}


def test_being_removed_from_the_session_stops_the_sends(world, monkeypatch):
    install(world, released(world))
    run = start(world)
    session = call(world, run, "sessions.create", {"title": "t"})["body"]["id"]
    monkeypatch.setattr(services.session_access, "can_write", lambda *a: False)
    assert call(world, run, "sessions.send", {"session_id": session, "text": "hi", "client_id": "a"})["status"] == 403


def test_a_refused_turn_isnt_reported_as_sent(world, monkeypatch):
    install(world, released(world))
    run = start(world)
    session = call(world, run, "sessions.create", {"title": "t"})["body"]["id"]

    class Cancelled:
        pk = uuid.uuid4()
        status = Turn.CANCELLED

    monkeypatch.setattr(services.doors, "send_from", lambda *a, **k: (None, Cancelled))
    assert call(world, run, "sessions.send", {"session_id": session, "text": "hi", "client_id": "a"})["status"] == 403


@pytest.mark.parametrize("route", ["install", "configure", "accept"])
def test_an_agent_acting_for_the_person_cant_change_what_they_agreed_to(world, monkeypatch, route):
    sub = released(world)
    inst = install(world, sub)
    monkeypatch.setattr("apps.local_apps.api.kinds.speaks_for_a_turn", lambda request: True)
    c = client(world["person"])
    if route == "install":
        r = c.post(
            "/api/local-apps/store/apps/meeting-assistant/install",
            {
                "runner_id": str(world["laptop"].pk),
                "settings": {"agent": "helper"},
                "accept_digest": trust.digest(sub.manifest),
            },
            content_type=J,
        )
    elif route == "configure":
        r = c.patch(f"/api/local-apps/installations/{inst.pk}", {"settings": {"brief": "x"}}, content_type=J)
    else:
        r = c.post(f"/api/local-apps/installations/{inst.pk}/accept", {"digest": "x"}, content_type=J)
    assert r.status_code == 403


def test_a_runs_routes_arent_tools_for_an_agent():
    from apps.mcp.api_tools import EXCLUDED

    for op in (
        "list_runner_local_apps",
        "get_runner_local_app_package",
        "start_runner_local_app",
        "beat_runner_local_app",
        "finish_runner_local_app",
        "call_canopy_for_local_app",
    ):
        assert op in EXCLUDED


def test_code_a_reviewer_couldnt_read_cant_be_submitted(world, monkeypatch):
    monkeypatch.setattr(pkg, "MAX_DIFF_LINES", 3)
    r = client(world["author"]).post(
        "/api/local-apps/submissions/",
        {"package_b64": b64(package(files={"a_data.txt": "x\n" * 50})), "workspace": "dimagi"},
        content_type=J,
    )
    assert r.status_code == 422 and "reviewer" in r.json()["detail"]


@pytest.mark.parametrize("files", [{"./app.py": "x"}, {"APP.py": "x"}])
def test_the_server_refuses_what_a_runner_couldnt_unpack(files):
    with pytest.raises(pkg.BadPackage):
        pkg.read(package(files=files))


def test_a_package_fits_in_a_request_body():
    from django.conf import settings

    assert (pkg.MAX_PACKED * 4) // 3 + 1024 < settings.DATA_UPLOAD_MAX_MEMORY_SIZE


def test_settings_remember_the_authors_order_through_jsonb():
    from apps.local_apps import manifest as mf

    assert [s["order"] for s in mf.validate(manifest())["settings"].values()] == [0, 1]


# --- publishing with no approval, while the framework is practised on --------------------


def test_with_approval_off_a_submission_is_live_at_once_and_says_nobody_reviewed_it(world, settings):
    settings.LOCAL_APPS_REQUIRE_APPROVAL = False
    sub = submit(world)
    assert sub.status == "released" and sub.approval_id is None and not sub.reviewed
    assert not ApprovalRequest.objects.exists()
    detail = client(world["person"]).get("/api/local-apps/store/apps/meeting-assistant").json()
    assert detail["reviewed"] is False and detail["version"] == "0.1.0"


def test_an_approved_version_says_it_was_reviewed(world):
    released(world)
    detail = client(world["person"]).get("/api/local-apps/store/apps/meeting-assistant").json()
    assert detail["reviewed"] is True


def test_with_approval_off_a_viewer_still_cant_publish(world, settings):
    settings.LOCAL_APPS_REQUIRE_APPROVAL = False
    viewer = User.objects.create_user("viewer2", "viewer2@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=world["ws"], role=WorkspaceMembership.VIEWER)
    r = client(viewer).post(
        "/api/local-apps/submissions/", {"package_b64": b64(package()), "workspace": "dimagi"}, content_type=J
    )
    assert r.status_code == 403


# --- a user app belongs to the person -----------------------------------------------------


def test_anyone_signed_in_can_see_and_install_with_no_switch(world):
    Workspace.objects.filter(slug="dimagi").update(settings={})
    sub = released(world)
    r = client(world["other"]).get("/api/local-apps/store/")
    assert [a["key"] for a in r.json()["apps"]] == ["meeting-assistant"]
    assert client(world["person"]).get(f"/api/local-apps/store/apps/{sub.key}").status_code == 200


def test_an_anonymous_visitor_sees_nothing():
    assert Client().get("/api/local-apps/store/").status_code in (401, 403)


def test_the_agent_can_come_from_any_of_your_workspaces_and_decides_where_sessions_land(world):
    sub = released(world)
    second = Workspace.objects.create(slug="second", display_name="Second", created_by=world["root"])
    WorkspaceMembership.objects.create(user=world["person"], workspace=second, role=WorkspaceMembership.EDITOR)
    Agent.objects.create(
        slug="elsewhere",
        name="Elsewhere",
        workspace=second,
        owner=world["person"],
        user=User.objects.create_user("elsewhere-bot", "elsewhere@dimagi-ai.com", "pw"),
    )
    detail = client(world["person"]).get("/api/local-apps/store/apps/meeting-assistant").json()
    assert {a["slug"]: a["workspace"] for a in detail["agents"]} == {"helper": "dimagi", "elsewhere": "second"}
    inst = install(world, sub, settings={"agent": "elsewhere"})
    assert inst.workspace_id == "second"
    run = start(world)
    assert "/w/second/" in call(world, run, "sessions.create", {"title": "t"})["body"]["url"]
    r = client(world["person"]).patch(
        f"/api/local-apps/installations/{inst.pk}", {"settings": {"agent": "helper"}}, content_type=J
    )
    assert r.status_code == 200 and r.json()["workspace"] == "dimagi"


def test_leaving_the_agents_workspace_stops_the_run_and_refuses_the_next_start(world):
    install(world, released(world))
    run = start(world)
    WorkspaceMembership.objects.filter(user=world["person"], workspace=world["ws"]).delete()
    assert beat(world, run) == {"stop": True, "reason": "no_longer_allowed_to_use_agent"}
    assert "can't use" in _listed(world)["refusal"]


def test_only_the_workspace_that_published_an_app_can_change_it(world, settings):
    settings.LOCAL_APPS_REQUIRE_APPROVAL = False
    submit(world)
    rival = Workspace.objects.create(slug="rival", display_name="Rival", created_by=world["other"])
    WorkspaceMembership.objects.create(user=world["other"], workspace=rival, role=WorkspaceMembership.OWNER)
    r = client(world["other"]).post(
        "/api/local-apps/submissions/",
        {"package_b64": b64(package(manifest(version="0.2.0"))), "workspace": "rival"},
        content_type=J,
    )
    assert r.status_code == 403 and "another workspace" in r.json()["detail"]
    assert submit(world, manifest(version="0.2.0")).status == "released"
