"""Hiring 2.1 through the framework: per-job access (scope), a compliance-only
reader, held-back emails, a then-step that links back, and bulk cancel."""
from __future__ import annotations

import datetime as dt

import pytest
from django.core import mail
from django.utils import timezone

from apps.workspace_apps import manifest as mf
from apps.workspace_apps import services
from apps.workspace_apps.models import AppRecord, AppRoleGrant, AppScheduledEmail
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.workspace_apps import BASE, J, _act, _client, _job, _user

pytestmark = pytest.mark.django_db
Q = f"{BASE}/hiring/query?collection="


def _cand(client, job, email="ada@example.org", name="Ada Lovelace"):
    r = _act(client, "add_candidate", {"job": job, "name": name, "email": email, "region": "US"})
    assert r.status_code == 200, r.content
    return r.json()["id"]


def _member(world, name, role=WorkspaceMembership.EDITOR):
    u = _user(name)
    WorkspaceMembership.objects.create(user=u, workspace=world["ws"], role=role)
    return u, _client(u)


def _ids(client, collection):
    r = client.get(Q + collection)
    assert r.status_code == 200, r.content
    return {row["id"] for row in r.json()}


def test_a_job_is_seen_by_its_team_and_a_confidential_one_by_its_team_alone(world, hiring):
    recruiter = _client(world["editor"])
    _, ivan = _member(world, "ivan")
    _, olga = _member(world, "olga")
    open_job = _job(recruiter, hiring_team="ivan@dimagi.com")
    secret = _job(recruiter, confidential=True, hiring_manager="olga@dimagi.com")
    a1, a2 = _cand(recruiter, open_job), _cand(recruiter, secret, email="bo@example.org")

    assert _ids(recruiter, "application") == {a1, a2}  # a recruiter sees every job
    assert _ids(ivan, "job") == {open_job}  # the confidential job is not even listed
    assert _ids(ivan, "application") == {a1}
    assert _ids(olga, "job") == {open_job, secret}
    assert _ids(olga, "application") == {a2}  # team-only: not the open job's candidates

    # Out of scope reads as missing, for history, actions and refs alike.
    assert ivan.get(f"{BASE}/hiring/records/{a2}/history").status_code == 404
    assert _act(ivan, "add_note", {"application": a2, "body": "hi"}).status_code == 422
    assert _act(ivan, "add_note", {"application": a1, "body": "hi"}).status_code == 200
    assert _act(olga, "update_candidate", {"tags": "x"}, record=a1).status_code in (403, 404)
    assert ivan.post(f"{BASE}/hiring/links/schedule/{a2}", content_type=J).status_code in (403, 404)

    # With team-only off, any interviewer sees a non-confidential job's candidates — never a confidential one's.
    services.manifest_for(hiring, "live")  # sanity: the setting exists
    v = services.start_draft(hiring, user=world["admin"])
    services.add_op(v, {"op": "set_setting", "key": "team_only_access", "value": False}, by="admin")
    services.publish(v, user=world["admin"], may_approve_risk=True)
    assert _ids(olga, "application") == {a1, a2}
    assert _ids(ivan, "application") == {a1}


def test_an_interviewer_sees_the_candidate_their_interview_names_and_no_more(world, hiring):
    recruiter = _client(world["editor"])
    _, ivan = _member(world, "ivan")
    job = _job(recruiter)
    mine, other = _cand(recruiter, job), _cand(recruiter, job, email="bo@example.org", name="Bo")
    assert _ids(ivan, "application") == set()
    iv = _act(recruiter, "schedule_interview", {"application": mine, "job": job, "kind": "Technical",
                                                "date": "2026-11-03", "time": "14:00", "interviewers": "ivan@dimagi.com"})
    assert iv.status_code == 200, iv.content
    assert _ids(ivan, "application") == {mine}
    assert _ids(ivan, "interview") == {iv.json()["id"]}
    assert job in _ids(ivan, "job")
    assert _act(ivan, "add_scorecard", {"application": mine, "interview": iv.json()["id"], "rating": "Yes"}).status_code == 200
    assert _act(ivan, "add_scorecard", {"application": other, "rating": "No"}).status_code == 422


def test_recruiter_is_for_admins_and_names_and_compliance_reads_applications(world, hiring):
    editor, admin = _client(world["editor"]), _client(world["admin"])
    job = _job(editor)
    cand = _cand(editor, job)
    clara, cc = _member(world, "clara", WorkspaceMembership.VIEWER)
    assert cc.get(Q + "application").status_code == 403
    AppRoleGrant.objects.create(installation=hiring, user=clara, role="compliance")
    assert _ids(cc, "application") == {cand}  # read_also, and compliance bypasses scope
    assert cc.get(Q + "stage_change").status_code == 200
    assert _act(cc, "move_stage", {"stage": "Screen"}, record=cand).status_code == 403
    assert _ids(admin, "application") == {cand}  # an admin holds recruiter by capability


def test_a_held_back_rejection_is_sent_when_due_and_cancelled_if_the_candidate_is_back(world, hiring, settings):
    settings.WORKSPACE_APPS_SEND_EMAIL = True
    editor = _client(world["editor"])
    job = _job(editor)
    a, b = _cand(editor, job), _cand(editor, job, email="bo@example.org", name="Bo")
    for rid in (a, b):
        r = editor.post(f"{BASE}/hiring/actions/reject_and_email",
                        {"record": rid, "input": {"rejection_reason": "Other"}, "params": {"delay_hours": 24}},
                        content_type=J)
        assert r.status_code == 200, r.content
        assert r.json()["_effects"][0]["status"].startswith("scheduled for ")
    assert mail.outbox == [] and AppScheduledEmail.objects.count() == 2
    assert _act(editor, "unreject", {"stage": "Applied"}, record=b).status_code == 200

    assert services.send_scheduled() == {"sent": 0, "cancelled": 0, "failed": 0}  # not due yet
    out = services.send_scheduled(now=timezone.now() + dt.timedelta(hours=25))
    assert out == {"sent": 1, "cancelled": 1, "failed": 0}
    assert [m.to for m in mail.outbox] == [["ada@example.org"]]
    assert AppScheduledEmail.objects.get(record_id=b).status == "cancelled"
    assert any(e["op"] == "email" for e in editor.get(f"{BASE}/hiring/records/{a}/history").json())


def test_a_referral_links_to_the_application_it_became_and_interviews_cancel_in_bulk(world, hiring):
    editor = _client(world["editor"])
    _, sam = _member(world, "sam", WorkspaceMembership.VIEWER)
    job = _job(editor)
    ref = _act(sam, "refer", {"job": job, "name": "Cy", "email": "cy@example.org"})
    assert ref.status_code == 200, ref.content
    linked = AppRecord.objects.get(pk=ref.json()["id"]).data["application"]
    assert AppRecord.objects.get(pk=linked).data["email"] == "cy@example.org"

    cand = _cand(editor, job)
    ivs = [_act(editor, "schedule_interview", {"application": cand, "job": job, "kind": "Technical",
                                               "date": "2026-11-0" + str(d), "time": "10:00"}).json()["id"]
           for d in (3, 4)]
    r = editor.post(f"{BASE}/hiring/actions/cancel_interview/bulk", {"records": ivs, "input": {}}, content_type=J)
    assert r.status_code == 200 and sorted(r.json()["done"]) == sorted(ivs)
    assert {AppRecord.objects.get(pk=i).data["status"] for i in ivs} == {"Cancelled"}


def test_a_scope_must_lead_to_a_scoped_root():
    base = {"key": "x-app", "name": "X", "roles": {"boss": {}},
            "collections": {"a": {"fields": {"t": {"type": "text"}}},
                            "b": {"fields": {"a": {"type": "ref", "collection": "a"}}, "scope": {"via": "a"}}},
            "actions": {}, "pages": [{"slug": "home", "code": "function View(){return <Empty>x</Empty>}"}]}
    with pytest.raises(ValueError, match="scoped root"):
        mf.validate(base)
    base["collections"]["a"]["scope"] = {"bypass": ["boss"], "team": ["t"]}
    assert mf.validate(base)["collections"]["b"]["scope"]["via"] == "a"
    base["collections"]["b"]["scope"]["bypass"] = ["boss"]
    with pytest.raises(ValueError, match="bypass from its root"):
        mf.validate(base)


def test_the_store_shows_an_app_in_full_before_it_is_installed(world):
    viewer, outsider = _client(world["viewer"]), _client(world["outsider"])
    card = next(a for a in viewer.get("/api/workspace-app-store/dimagi/").json() if a["key"] == "hiring")
    assert card["tagline"] and card["cover"].startswith("screenshots/")
    d = viewer.get("/api/workspace-app-store/dimagi/detail?template=hiring").json()
    assert d["installed"] is False and len(d["listing"]["screenshots"]) >= 4 and d["listing"]["highlights"]
    assert {r["name"] for r in d["roles"]} >= {"maintainer", "recruiter", "interviewer", "compliance"}
    assert "Careers" in d["public_pages"] and any(r["scoped"] for r in d["records"])
    shot = viewer.get(f"/api/workspace-app-store/dimagi/screenshot?template=hiring&file={card['cover']}")
    assert shot.status_code == 200 and shot["Content-Type"].startswith("image/")
    # Only a file the listing names, and only for a member.
    assert viewer.get("/api/workspace-app-store/dimagi/screenshot?template=hiring&file=manifest.json").status_code == 404
    assert viewer.get("/api/workspace-app-store/dimagi/screenshot?template=hiring&file=../hiring/manifest.json"
                      ).status_code == 404
    assert outsider.get(f"/api/workspace-app-store/dimagi/screenshot?template=hiring&file={card['cover']}"
                        ).status_code in (403, 404)
    assert viewer.get("/api/workspace-app-store/dimagi/detail?template=nope").status_code == 404


def test_sample_records_seed_in_any_collection_order(world, hiring):
    """Postgres stores the manifest as jsonb, which reorders its keys: an offer's
    fixture can come before the opening it names."""
    m = services.manifest_for(hiring, "live")
    m = {**m, "fixtures": dict(reversed(list(m["fixtures"].items())))}
    saved = services.seed(hiring, "draft-x", m)
    assert saved and AppRecord.objects.filter(installation=hiring, space="draft-x").count() == sum(
        len(rows) for rows in m["fixtures"].values())
    with pytest.raises(ValueError, match="@nobody"):
        services.seed(hiring, "draft-y", {**m, "fixtures": {"note": [{"application": "@nobody", "body": "x"}]}})
