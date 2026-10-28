"""Workspace apps: the store, the role rules, the public door, and the bundled Hiring app end to end."""
from __future__ import annotations

import copy
import json

import pytest
from django.core import mail

from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services
from apps.workspace_apps.models import AppRecord, AppRecordEvent

pytestmark = pytest.mark.django_db

from tests.fixtures.workspace_apps import BASE, PUB, J, _act, _client, _job, _user  # noqa: F401


def _change(client, **op):
    return client.post(f"{BASE}/hiring/draft/ops", op, content_type=J)


# --- the manifest and the store --------------------------------------------------------


def test_the_hiring_template_is_in_the_store_and_its_own_checks_pass(world, hiring):
    store = _client(world["viewer"]).get("/api/workspace-app-store/dimagi/").json()
    assert [(a["key"], a["publisher"]) for a in store] == [("hiring", "canopy")]
    m = services.bundled_manifest("hiring")
    assert {"review", "pipeline", "jobs", "job-setup", "candidate", "interviews", "kit", "approvals", "offers",
            "prospects", "refer", "templates", "reports", "self-id", "careers", "job", "pick-a-time",
            "your-offer"} == {p["slug"] for p in m["pages"]}
    admin = _client(world["admin"])
    assert admin.post(f"{BASE}/hiring/draft", {}, content_type=J).status_code == 201
    review = admin.post(f"{BASE}/hiring/draft/checks").json()["review"]
    assert review["checks"]["ok"], review["checks"]
    assert len(review["checks"]["results"]) == len(m["scenarios"]) >= 15


def _careers(m):
    return next(p for p in m["pages"] if p["slug"] == "careers")


@pytest.mark.parametrize("mutate,needle", [
    (lambda m: m["roles"]["recruiter"].update(capability="editor"), "capability"),
    (lambda m: m["actions"]["apply"].update(op="update"), "may only create"),
    (lambda m: m["actions"]["apply"]["refs"].pop("job"), "must say which records"),
    (lambda m: m["actions"]["move_stage"].update(input=["stage", "secret"]), "input"),
    (lambda m: m["collections"]["application"].update(read="public"), "read"),
    (lambda m: _careers(m)["sources"].append({"alias": "apps", "collection": "application"}), "no public projection"),
    (lambda m: _careers(m)["sources"].append({"alias": "t", "model": "tasks"}), "only the app's own"),
    (lambda m: m["pages"][0].update(code="alert(1)"), "View"),
    (lambda m: m["pages"][0].update(code="function View(){return <form onSubmit={f}/>}"), "allow-forms"),
    (lambda m: m["actions"]["add_scorecard"]["set"].update(interviewer="$env.SECRET"), "tokens"),
    (lambda m: m["actions"]["apply"]["effects"][0].pop("enabled_setting"), "switchable"),
    (lambda m: m["collections"]["job"]["public"]["fields"].append("hiring_manager") or
     m["collections"]["application"].update(public={"fields": ["resume"]}), "never public"),
])
def test_a_bad_manifest_is_refused_naming_what_is_wrong(mutate, needle):
    m = copy.deepcopy(services.bundled_manifest("hiring"))
    mutate(m)
    with pytest.raises(ValueError, match=needle):
        mf.validate(m)


# --- managing apps ----------------------------------------------------------------------


def test_only_the_integrations_tier_installs_and_switches_apps(world, hiring):
    for who in ("editor", "viewer"):
        c = _client(world[who])
        assert c.patch(f"{BASE}/hiring", {"enabled": False}, content_type=J).status_code == 403
        assert c.delete(f"{BASE}/hiring").status_code == 403
    assert _client(world["editor"]).post(f"{BASE}/", {"template": "hiring"}, content_type=J).status_code == 403
    assert _client(world["outsider"]).get(f"{BASE}/").status_code == 404
    assert _client(world["admin"]).post(f"{BASE}/", {"template": "hiring"}, content_type=J).status_code == 422


def test_a_switched_off_app_is_gone_for_its_users_but_its_data_stays(world, hiring):
    editor, admin = _client(world["editor"]), _client(world["admin"])
    _job(editor)
    assert admin.patch(f"{BASE}/hiring", {"enabled": False}, content_type=J).json()["enabled"] is False
    assert editor.get(f"{BASE}/hiring").status_code == 404
    assert [a["key"] for a in editor.get(f"{BASE}/").json()] == []
    admin.patch(f"{BASE}/hiring", {"enabled": True}, content_type=J)
    assert len(editor.get(f"{BASE}/hiring/query?collection=job").json()) == 1


def test_a_workspace_can_install_an_app_it_wrote(world):
    m = {"key": "visitors", "name": "Visitor log",
         "collections": {"visit": {"fields": {"who": {"type": "text", "required": True}}}},
         "actions": {"log": {"role": "member", "op": "create", "collection": "visit", "input": ["who"]}},
         "pages": [{"slug": "log", "code": "function View({data}){return <Table rows={data.v} columns={[]}/>}",
                    "sources": [{"alias": "v", "collection": "visit"}]}]}
    r = _client(world["admin"]).post(f"{BASE}/", {"manifest": m}, content_type=J)
    assert r.status_code == 201, r.content
    assert r.json()["template"] == "dimagi/visitors"
    viewer = _client(world["viewer"])
    assert _act(viewer, "log", {"who": "Ada"}, base=f"{BASE}/visitors").status_code == 200
    # Its own template is listed in its own store, and nowhere else.
    assert "dimagi/visitors" in [a["key"] for a in viewer.get("/api/workspace-app-store/dimagi/").json()]
    assert "dimagi/visitors" not in [a["key"] for a in
                                     _client(world["outsider"]).get("/api/workspace-app-store/other/").json()]


# --- roles -----------------------------------------------------------------------------


def test_app_roles_follow_workspace_capabilities_and_grants(world, hiring):
    editor, viewer, admin = _client(world["editor"]), _client(world["viewer"]), _client(world["admin"])
    job = _job(editor)
    assert viewer.get(f"{BASE}/hiring/query?collection=job").status_code == 200
    assert viewer.get(f"{BASE}/hiring/query?collection=application").status_code == 403
    assert _act(viewer, "create_job", {"title": "x", "description": "y"}).status_code == 403
    r = admin.post(f"{BASE}/hiring/grants", {"email": "viewer@dimagi.com", "role": "interviewer"}, content_type=J)
    assert r.status_code == 201, r.content
    cand = _act(editor, "add_candidate", {"job": job, "name": "Grace", "email": "grace@example.org"}).json()["id"]
    assert _act(viewer, "add_scorecard", {"application": cand, "rating": "Yes"}).status_code == 200
    assert _act(viewer, "move_stage", {"stage": "Hired"}, record=cand).status_code == 403
    assert viewer.get(f"{BASE}/hiring/query?collection=scorecard").json()[0]["interviewer"] == "viewer@dimagi.com"
    assert admin.post(f"{BASE}/hiring/grants", {"email": "outsider@dimagi.com", "role": "interviewer"},
                      content_type=J).status_code == 422
    assert editor.post(f"{BASE}/hiring/grants", {"email": "viewer@dimagi.com", "role": "recruiter"},
                       content_type=J).status_code == 403


def test_an_action_validates_input_and_records_history(world, hiring):
    editor = _client(world["editor"])
    job = _job(editor)
    assert _act(editor, "add_candidate", {"job": job, "name": "X", "email": "nope"}).status_code == 422
    assert _act(editor, "add_candidate", {"job": 99999, "name": "X", "email": "x@y.org"}).status_code == 422
    assert _act(editor, "add_candidate", {"job": job, "name": "X", "email": "x@y.org", "admin": True}).status_code == 422
    cand = _act(editor, "add_candidate", {"job": job, "name": "Ada", "email": "ada@example.org"}).json()
    assert (cand["stage"], cand["source"]) == ("Applied", "Careers page")
    assert _act(editor, "move_stage", {"stage": "Screen"}, record=cand["id"]).status_code == 200
    log = editor.get(f"{BASE}/hiring/records/{cand['id']}/history").json()
    assert [e["action"] for e in log] == ["move_stage", "add_candidate"]
    assert log[0]["changes"]["stage"] == ["Applied", "Screen"]


def test_reject_and_email_sends_only_when_email_is_on(world, hiring, settings):
    editor = _client(world["editor"])
    job = _job(editor, title="Designer")
    cand = _act(editor, "add_candidate", {"job": job, "name": "Kofi", "email": "kofi@example.org"}).json()["id"]
    out = _act(editor, "reject_and_email", {"rejection_reason": "Position filled"}, record=cand).json()
    assert out["stage"] == "Rejected"
    assert out["_effects"][0]["status"].startswith("not sent") and mail.outbox == []
    settings.WORKSPACE_APPS_SEND_EMAIL = True
    cand2 = _act(editor, "add_candidate", {"job": job, "name": "Ama", "email": "ama@example.org"}).json()["id"]
    out = _act(editor, "reject_and_email", {"rejection_reason": "Withdrew"}, record=cand2).json()
    assert out["_effects"][0]["status"] == "sent"
    assert mail.outbox[0].to == ["ama@example.org"]
    assert mail.outbox[0].subject == "Your application for Designer"
    assert "Hi Ama," in mail.outbox[0].body and "at Dimagi" in mail.outbox[0].body
    # Rejecting without email sends nothing.
    cand3 = _act(editor, "add_candidate", {"job": job, "name": "Bo", "email": "bo@example.org"}).json()["id"]
    assert _act(editor, "reject", {"rejection_reason": "Other"}, record=cand3).json()["_effects"] == []
    assert len(mail.outbox) == 1


# --- the life cycle: draft, change, test copy, checks, publish -----------------------------


def test_a_maintainer_customizes_hiring_on_a_test_copy_and_publishes(world, hiring):
    """The head-of-HR story: a viewer given `maintainer` adds a question and a
    stage, tries them on sample data, and publishes — live data untouched."""
    admin, hr, editor = _client(world["admin"]), _client(world["viewer"]), _client(world["editor"])
    _job(editor, title="Live role")
    assert hr.post(f"{BASE}/hiring/draft", {}, content_type=J).status_code == 403
    admin.post(f"{BASE}/hiring/grants", {"email": "viewer@dimagi.com", "role": "maintainer"}, content_type=J)

    draft = hr.post(f"{BASE}/hiring/draft", {"note": "visa question"}, content_type=J).json()
    did = draft["id"]
    # The test copy holds sample data only.
    sample = hr.get(f"{BASE}/hiring/query?collection=job&draft={did}").json()
    assert {j["title"] for j in sample} >= {"Senior Software Engineer (sample)"}
    assert "Live role" not in {j["title"] for j in sample}
    # On the test copy the maintainer holds every app role, to try the app as a
    # recruiter or interviewer; on live they hold only their own.
    assert hr.get(f"{BASE}/hiring/query?collection=application&draft={did}").status_code == 200
    assert hr.get(f"{BASE}/hiring/query?collection=application").status_code == 403

    ask = "Ask applicants whether they need visa sponsorship, and add a Phone screen stage before Interview"
    r = _change(hr, op="add_field", collection="application", key="x_visa",
                field={"type": "bool", "label": "Needs visa sponsorship"}, intent=ask)
    assert r.status_code == 201, r.content
    assert _change(hr, op="set_action", action="apply", add_input=["x_visa"], intent=ask).status_code == 201
    r = _change(hr, op="add_option", collection="application", field="stage", option="Phone screen",
                before="Interview", intent=ask)
    review = r.json()["review"]
    assert [c["summary"] for c in review["changes"]] == [
        "Adds a bool field “Needs visa sponsorship” to Candidate.",
        "“Apply”: now also asks for “Needs visa sponsorship” on Candidate.",
        "Adds “Phone screen” to “Stage” on Candidate."]
    # Adding to the PUBLIC form is a change an admin must approve.
    assert [c["risk"] for c in review["risks"]] == ["changes what the public form accepts"]

    # Refused changes say why and leave the draft alone.
    r = _change(hr, op="set_field", collection="application", field="email", required=False)
    assert r.status_code == 422 and "only added" in r.json()["detail"]
    r = _change(hr, op="add_field", collection="application", key="visa", field={"type": "bool"})
    assert r.status_code == 422 and "x_" in r.json()["detail"]

    # Try it on the test copy: the draft's app takes the new question and stage.
    sample_job = next(j["id"] for j in sample if j["status"] == "open" and not j.get("confidential"))
    app = hr.get(f"{BASE}/hiring?draft={did}").json()
    assert "x_visa" in app["data_model"]["application"]["fields"]
    assert "Phone screen" in app["data_model"]["application"]["fields"]["stage"]["options"]
    a = hr.post(f"{BASE}/hiring/actions/apply?draft={did}&as_public=true",
                {"input": {"job": sample_job, "name": "Visa Test", "email": "v@example.org", "x_visa": True}},
                content_type=J)
    assert a.status_code == 200, a.content
    assert AppRecord.objects.filter(space=f"draft-{did}", data__x_visa=True).count() == 1
    assert not AppRecord.objects.filter(space="live", data__name="Visa Test").exists()

    # The maintainer can't publish a public-form change; the admin can.
    r = hr.post(f"{BASE}/hiring/draft/publish")
    assert r.status_code == 403 and "admin" in r.json()["detail"]
    r = admin.post(f"{BASE}/hiring/draft/publish")
    assert r.status_code == 200, r.content
    live = r.json()
    assert live["live_version"] == 2 and live["has_draft"] is False
    assert "x_visa" in live["data_model"]["application"]["fields"]
    # The test copy is gone; live data never moved.
    assert not AppRecord.objects.filter(space=f"draft-{did}").exists()
    assert AppRecord.objects.filter(space="live", collection="job").count() == 1
    assert "x_visa" in editor.get(f"{BASE}/hiring").json()["data_model"]["_actions"]["apply"]["input"]


def test_a_failing_check_blocks_publishing(world, hiring):
    admin = _client(world["admin"])
    admin.post(f"{BASE}/hiring/draft", {}, content_type=J)
    # Requiring a new field the scenarios' applications don't give breaks "a visitor applies".
    _change(admin, op="add_field", collection="application", key="x_pronouns",
            field={"type": "text", "label": "Pronouns", "required": True})
    r = admin.post(f"{BASE}/hiring/draft/publish")
    assert r.status_code == 409 and "A visitor applies" in r.json()["detail"]
    checks = admin.get(f"{BASE}/hiring/draft").json()["review"]["checks"]
    assert checks["ok"] is False
    # A scenario the workspace adds runs too.
    admin.delete(f"{BASE}/hiring/draft")
    admin.post(f"{BASE}/hiring/draft", {}, content_type=J)
    _change(admin, op="add_scenario", scenario={"name": "Nobody is hired on day one", "steps": [
        {"expect": {"collection": "application", "where": {"stage": "Hired"}, "count": 0}}]})
    assert admin.post(f"{BASE}/hiring/draft/publish").status_code == 200


def test_rollback_restores_an_earlier_version_as_a_new_one(world, hiring):
    admin = _client(world["admin"])
    admin.post(f"{BASE}/hiring/draft", {}, content_type=J)
    _change(admin, op="set_setting", key="company_name", value="Dimagi Inc.")
    admin.post(f"{BASE}/hiring/draft/publish")
    assert admin.get(f"{BASE}/hiring").json()["settings"]["company_name"] == "Dimagi Inc."
    r = admin.post(f"{BASE}/hiring/rollback", {"number": 1}, content_type=J)
    assert r.json()["live_version"] == 3 and r.json()["settings"]["company_name"] == "Dimagi"
    versions = admin.get(f"{BASE}/hiring/versions").json()
    assert [(v["number"], v["status"], v["origin"]) for v in versions] == [
        (3, "published", "rollback"), (2, "superseded", "edit"), (1, "superseded", "install")]


def test_a_template_update_keeps_customizations_and_quarantines_what_no_longer_fits(world, hiring):
    """Hiring 2.1 adds a field, drops the `interview` page slot it once offered,
    and the workspace's changes ride along — the one that lost its anchor is set
    aside with its reason, not dropped and not breaking the app."""
    admin = _client(world["admin"])
    admin.post(f"{BASE}/hiring/draft", {}, content_type=J)
    _change(admin, op="add_option", collection="application", field="source", option="Job fair")
    _change(admin, op="fill_slot", page="candidate", slot="sidebar", key="refs",
            code="function Fill({ record }) { return <Card title='References'>none yet</Card> }")
    assert admin.post(f"{BASE}/hiring/draft/publish").status_code == 200

    tmpl = hiring.template
    m = copy.deepcopy(services.bundled_manifest("hiring"))
    m["version"], m["changelog"] = "2.1.0", "2.1.0 — adds a salary expectation field; the candidate page is redesigned without a sidebar."
    m["collections"]["application"]["fields"]["salary_expectation"] = {"type": "text", "label": "Salary expectation"}
    next(p for p in m["pages"] if p["slug"] == "candidate")["slots"] = []
    tmpl.versions.create(version="2.1.0", manifest=mf.validate(m), changelog=m["changelog"])

    assert admin.get(f"{BASE}/").json()[0]["upgrade_available"] == "2.1.0"
    d = admin.post(f"{BASE}/hiring/draft", {"upgrade": True}, content_type=J).json()
    review = d["review"]
    assert review["template_change"]["to"] == "2.1.0"
    assert [c["summary"] for c in review["changes"]] == ["Adds “Job fair” to “Source” on Candidate."]
    assert [q["reason"] for q in review["quarantine"]] == ["page 'candidate' has no slot 'sidebar'"]
    assert admin.post(f"{BASE}/hiring/draft/publish").status_code == 409
    # Take the orphan out (or move it to a new slot), then publish the update.
    admin.delete(f"{BASE}/hiring/draft/ops/{review['quarantine'][0]['id']}")
    assert admin.post(f"{BASE}/hiring/draft/publish").status_code == 200
    app = admin.get(f"{BASE}/hiring").json()
    assert app["template_version"] == "2.1.0" and app["upgrade_available"] is None
    fields = app["data_model"]["application"]["fields"]
    assert "salary_expectation" in fields and "Job fair" in fields["source"]["options"]


def test_the_guide_hands_an_agent_the_contract_and_the_draft(world, hiring):
    admin = _client(world["admin"])
    assert _client(world["editor"]).get(f"{BASE}/hiring/guide").status_code == 403
    admin.post(f"{BASE}/hiring/draft", {}, content_type=J)
    g = admin.get(f"{BASE}/hiring/guide").json()
    assert "add_field" in g["contract"] and g["draft"] and g["manifest"]["key"] == "hiring"


# --- the public door ---------------------------------------------------------------------


def test_the_public_door_is_shut_until_the_app_is_public(world, hiring):
    anon = _client()
    assert anon.get(PUB).status_code == 404
    assert anon.get(f"{BASE}/hiring").status_code == 401
    _client(world["admin"]).patch(f"{BASE}/hiring", {"public": True}, content_type=J)
    body = anon.get(PUB).json()
    assert {p["slug"] for p in body["pages"]} == {"careers", "job"}
    # The visitor's schema names only public fields and the public form's inputs.
    assert "hiring_manager" not in body["data_model"]["job"]["fields"]
    assert set(body["data_model"]["_actions"]) == {"apply"}
    assert "rejection_reason" not in body["data_model"]["application"]["fields"]
    _client(world["admin"]).patch(f"{BASE}/hiring", {"enabled": False}, content_type=J)
    assert anon.get(PUB).status_code == 404


def test_a_visitor_sees_only_open_jobs_and_only_public_fields(world, hiring):
    editor = _client(world["editor"])
    _job(editor, title="Open role", hiring_manager="boss@dimagi.com")
    _job(editor, title="Secret draft", status="draft")
    _client(world["admin"]).patch(f"{BASE}/hiring", {"public": True}, content_type=J)
    anon = _client()
    rows = anon.get(f"{PUB}/query?collection=job").json()
    assert [r["title"] for r in rows] == ["Open role"]
    assert "hiring_manager" not in rows[0] and "status" not in rows[0]
    assert anon.get(f"{PUB}/query?collection=job&where={json.dumps({'status': 'draft'})}").json() == []
    assert anon.get(f"{PUB}/query?collection=application").status_code == 404


def test_a_visitor_applies_with_a_resume_and_the_recruiter_downloads_it(world, hiring, settings):
    settings.DEBUG = True  # no bucket here: the dev fallback keeps the bytes in the row
    editor = _client(world["editor"])
    job = _job(editor)
    _client(world["admin"]).patch(f"{BASE}/hiring", {"public": True}, content_type=J)
    anon = _client()
    from django.core.files.uploadedfile import SimpleUploadedFile

    bad = anon.post(f"{PUB}/files", {"collection": "application", "field": "resume",
                                     "file": SimpleUploadedFile("cv.exe", b"MZ", content_type="application/x")})
    assert bad.status_code == 422
    assert anon.post(f"{PUB}/files", {"collection": "job", "field": "title",
                                      "file": SimpleUploadedFile("cv.pdf", b"%PDF")}).status_code == 422
    up = anon.post(f"{PUB}/files", {"collection": "application", "field": "resume",
                                    "file": SimpleUploadedFile("cv.pdf", b"%PDF-1.4 hello", content_type="application/pdf")})
    assert up.status_code == 201, up.content
    fid = up.json()["id"]
    r = _act(anon, "apply", {"job": job, "name": "Katherine", "email": "KJ@example.org", "resume": fid}, base=PUB)
    assert r.status_code == 200, r.content
    assert set(r.json()) == {"id"}
    # A file can be claimed once.
    assert _act(anon, "apply", {"job": job, "name": "Copycat", "email": "c@example.org", "resume": fid},
                base=PUB).status_code == 422
    got = editor.get(f"{BASE}/hiring/files/{fid}")
    assert got.status_code == 200 and got.content == b"%PDF-1.4 hello"
    assert _client(world["viewer"]).get(f"{BASE}/hiring/files/{fid}").status_code == 404
    rows = editor.get(f"{BASE}/hiring/query?collection=application").json()
    assert (rows[0]["name"], rows[0]["email"], rows[0]["stage"]) == ("Katherine", "kj@example.org", "Applied")


def test_public_actions_are_rate_limited_per_address_and_honeypotted(world, hiring):
    job = _job(_client(world["editor"]))
    _client(world["admin"]).patch(f"{BASE}/hiring", {"public": True}, content_type=J)
    anon = _client()
    app = {"job": job, "name": "Bot", "email": "bot@example.org"}
    assert _act(anon, "apply", {**app, "website": "spam.example"}, base=PUB).json() == {"id": 0}
    for _ in range(5):
        assert _act(anon, "apply", app, base=PUB).status_code == 200
    assert _act(anon, "apply", app, base=PUB).status_code == 429
    assert AppRecordEvent.objects.filter(action="apply").count() == 5


def test_records_never_cross_workspaces(world, hiring):
    _job(_client(world["editor"]))
    outsider = _client(world["outsider"])
    outsider.post("/api/workspace-apps/other/", {"template": "hiring"}, content_type=J)
    assert outsider.get("/api/workspace-apps/other/hiring/query?collection=job").json() == []
    assert outsider.get(f"{BASE}/hiring/query?collection=job").status_code == 404
