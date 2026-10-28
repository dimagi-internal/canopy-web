"""The app framework's own rules, through Hiring 2.0: guards on record state and
on who a record names, then-steps, params, role-only fields, private links,
bulk actions, feeds, retention, and a data-subject export."""
from __future__ import annotations

import datetime as dt

import pytest
from django.core import mail, signing
from django.test import Client, override_settings
from django.utils import timezone

from apps.workspace_apps import services
from apps.workspace_apps.models import AppFile, AppRecord
from tests.fixtures.workspace_apps import BASE, PUB, J, _act, _client, _job

pytestmark = pytest.mark.django_db


def _cand(client, job, **kw):
    r = _act(client, "add_candidate", {"job": job, "name": "Ada Lovelace", "email": "ada@example.org",
                                       "region": "EU/UK", **kw})
    assert r.status_code == 200, r.content
    return r.json()["id"]


def test_a_guarded_action_is_refused_when_the_record_does_not_match(world, hiring):
    editor = _client(world["editor"])
    job = _job(editor, status="draft")
    cand = _cand(editor, job)
    assert _act(editor, "reject", {"rejection_reason": "Other"}, record=cand).status_code == 200
    again = _act(editor, "reject", {"rejection_reason": "Other"}, record=cand)
    assert again.status_code == 409 and "does not apply" in again.json()["detail"]
    # The rejection was logged by the template's then-step, as the app.
    changes = services.query(hiring, roles={"interviewer"}, collection="stage_change", where={"application": cand})
    assert [c["to_stage"] for c in changes] == ["Rejected", "Applied"]


def test_only_the_approver_a_record_names_may_approve_and_the_last_approval_opens_the_job(world, hiring):
    editor, admin = _client(world["editor"]), _client(world["admin"])
    job = _job(editor, status="draft", pay_min=1, pay_max=2)
    a1 = _act(editor, "add_approver", {"kind": "Job", "job": job, "approver": "admin@dimagi.com"}).json()["id"]
    a2 = _act(editor, "add_approver", {"kind": "Job", "job": job, "approver": "editor@dimagi.com"}).json()["id"]
    assert _act(editor, "submit_job", {}, record=job).status_code == 200
    refused = _act(editor, "approve", {}, record=a1)
    assert refused.status_code == 403 and "Approver" in refused.json()["detail"]
    assert _act(admin, "approve", {"note": "ok"}, record=a1).status_code == 200
    assert AppRecord.objects.get(pk=job).data["status"] == "pending_approval"
    assert _act(editor, "approve", {}, record=a2).status_code == 200
    assert AppRecord.objects.get(pk=job).data["status"] == "open"


def test_a_field_only_one_role_reads_is_absent_for_everyone_else(world, hiring):
    """Voluntary self-ID answers reach the compliance role only — not the
    recruiter who decides, not the record's history, not an email."""
    editor = _client(world["editor"])
    job = _job(editor)
    anon = Client()
    r = anon.post(f"{PUB}/actions/apply", {"input": {"job": job, "name": "Grace", "email": "grace@example.org",
                                                     "eeo_gender": "Female", "region": "US"}}, content_type=J)
    # The app is not public yet: the public door is shut.
    assert r.status_code == 404
    _client(world["admin"]).patch(f"{BASE}/hiring", {"public": True}, content_type=J)
    r = anon.post(f"{PUB}/actions/apply", {"input": {"job": job, "name": "Grace", "email": "grace@example.org",
                                                     "eeo_gender": "Female", "region": "US"}}, content_type=J)
    assert r.status_code == 200, r.content
    rid = r.json()["id"]
    row = editor.get(f"{BASE}/hiring/query?collection=application").json()[0]
    assert row["name"] == "Grace" and "eeo_gender" not in row
    assert all("eeo_gender" not in e["changes"] for e in editor.get(f"{BASE}/hiring/records/{rid}/history").json())
    bad = editor.get(f"{BASE}/hiring/query?collection=application&where=" + '{"eeo_gender":"Female"}')
    assert bad.status_code == 422
    assert services.query(hiring, roles={"interviewer", "compliance"}, collection="application")[0]["eeo_gender"] == "Female"


def test_params_word_one_email_and_bulk_runs_each_record_alone(world, hiring, settings):
    settings.WORKSPACE_APPS_SEND_EMAIL = True
    editor = _client(world["editor"])
    job = _job(editor)
    ids = [_cand(editor, job, email=f"c{i}@example.org", name=f"Cand {i}") for i in range(3)]
    _act(editor, "reject", {"rejection_reason": "Other"}, record=ids[0])
    r = editor.post(f"{BASE}/hiring/actions/reject_and_email/bulk",
                    {"records": ids, "input": {"rejection_reason": "Position filled"},
                     "params": {"subject": "About {{job.title}}", "body": "Hi {{name}}, it is filled."}},
                    content_type=J)
    assert r.status_code == 200, r.content
    out = r.json()
    assert out["done"] == ids[1:] and [f["id"] for f in out["failed"]] == [ids[0]]
    assert sorted(m.to[0] for m in mail.outbox) == ["c1@example.org", "c2@example.org"]
    assert {m.body for m in mail.outbox} == {"Hi Cand 1, it is filled.", "Hi Cand 2, it is filled."}
    assert mail.outbox[0].subject == "About Senior Engineer"
    # Only an action the template marked bulk runs in bulk.
    assert editor.post(f"{BASE}/hiring/actions/add_note/bulk", {"records": ids}, content_type=J).status_code == 404


def test_an_interview_invite_carries_a_calendar_file(world, hiring, settings):
    settings.WORKSPACE_APPS_SEND_EMAIL = True
    editor = _client(world["editor"])
    job = _job(editor)
    cand = _cand(editor, job)
    r = _act(editor, "schedule_interview", {"application": cand, "job": job, "kind": "Technical", "date": "2026-11-03",
                                            "time": "14:00", "duration": 60, "interviewers": "ivan@example.org"})
    assert r.status_code == 200, r.content
    assert sorted(m.to[0] for m in mail.outbox) == ["ada@example.org", "ivan@example.org"]
    name, body, kind = mail.outbox[0].attachments[0]
    assert name == "invite.ics" and "DTSTART:20261103T140000" in body and "DTEND:20261103T150000" in body


def test_a_private_link_opens_one_record_and_runs_only_its_actions(world, hiring, settings):
    settings.WORKSPACE_APPS_SEND_EMAIL = True
    editor = _client(world["editor"])
    job = _job(editor)
    cand = _cand(editor, job)
    iv = _act(editor, "request_availability", {"application": cand, "job": job, "kind": "Phone screen", "duration": 30,
                                               "slot_options": "2026-11-04 10:00\n2026-11-04 15:00"}).json()["id"]
    # The email carried the link; the recruiter can also make one to send by hand.
    assert "/w/dimagi/app/hiring/l/" in mail.outbox[0].body
    made = editor.post(f"{BASE}/hiring/links/schedule/{iv}").json()
    token = made["token"]
    anon = Client()
    page = anon.get(f"{PUB}/link/{token}").json()
    assert page["record"]["application"] == {"name": "Ada Lovelace"} and page["page"]["slug"] == "pick-a-time"
    assert "email" not in str(page["record"])  # only the link's declared fields
    # A link action other than the link's own, or an input it does not take, is refused.
    assert anon.post(f"{PUB}/link/{token}/actions/accept_offer", {"input": {}}, content_type=J).status_code == 404
    assert anon.post(f"{PUB}/link/{token}/actions/pick_slot", {"input": {"status": "Done"}},
                     content_type=J).status_code == 422
    ok = anon.post(f"{PUB}/link/{token}/actions/pick_slot", {"input": {"date": "2026-11-04", "time": "15:00"}},
                   content_type=J)
    assert ok.status_code == 200 and ok.json() == {"id": iv}
    assert AppRecord.objects.get(pk=iv).data["status"] == "Scheduled"
    assert anon.post(f"{PUB}/link/{token}/actions/pick_slot", {"input": {"date": "2026-11-04", "time": "10:00"}},
                     content_type=J).status_code == 409
    # Tampered, expired, other-app and switched-off links are the same 404.
    assert anon.get(f"{PUB}/link/{token}x").status_code == 404
    other = signing.dumps({"i": hiring.pk + 99, "l": "schedule", "r": iv, "s": "live"}, salt="workspace_apps.link",
                          compress=True)
    assert anon.get(f"{PUB}/link/{other}").status_code == 404
    with override_settings(USE_TZ=True):
        later = timezone.now() + dt.timedelta(days=22)
        from unittest import mock  # noqa: PLC0415 — test-only

        with mock.patch("django.core.signing.time.time", return_value=later.timestamp()):
            assert anon.get(f"{PUB}/link/{token}").status_code == 404
    _client(world["admin"]).patch(f"{BASE}/hiring", {"enabled": False}, content_type=J)
    assert anon.get(f"{PUB}/link/{token}").status_code == 404


def test_the_job_feed_carries_only_public_posts_with_their_pay_range(world, hiring):
    editor, admin = _client(world["editor"]), _client(world["admin"])
    _job(editor, title="Open role", pay_min=100, pay_max=200, pay_currency="USD", pay_period="YEAR",
         workplace="Remote", country="United States, Kenya")
    _job(editor, title="Secret", confidential=True)
    _job(editor, title="Draft role", status="draft")
    admin.patch(f"{BASE}/hiring", {"public": True}, content_type=J)
    feed = Client().get(f"{PUB}/feed/job.json").json()
    assert [e["title"] for e in feed] == ["Open role"]
    e = feed[0]
    assert e["@type"] == "JobPosting" and e["hiringOrganization"]["name"] == "Dimagi"
    assert e["baseSalary"] == {"@type": "MonetaryAmount", "currency": "USD",
                               "value": {"@type": "QuantitativeValue", "unitText": "YEAR", "minValue": 100,
                                         "maxValue": 200}}
    assert e["jobLocationType"] == "TELECOMMUTE"
    assert [c["name"] for c in e["applicantLocationRequirements"]] == ["United States", "Kenya"]
    assert e["url"].endswith(f"/w/dimagi/app/hiring/job?id={e['identifier']['value']}")
    xml = Client().get(f"{PUB}/feed/job.xml")
    assert xml.status_code == 200 and b"<title>Open role</title>" in xml.content and b"Secret" not in xml.content
    assert Client().get(f"{PUB}/feed/application.json").status_code == 404


def test_retention_anonymizes_by_region_after_its_days_and_respects_a_legal_hold(world, hiring):
    editor = _client(world["editor"])
    job = _job(editor)
    eu, us, held = (_cand(editor, job, email=e, region=r) for e, r in
                    (("eu@example.org", "EU/UK"), ("us@example.org", "US"), ("held@example.org", "EU/UK")))
    for c in (eu, us, held):
        _act(editor, "reject", {"rejection_reason": "Other"}, record=c)
        _act(editor, "add_note", {"application": c, "body": "Said something personal"})
    _act(editor, "set_hold", {"legal_hold": True}, record=held)
    AppRecord.objects.filter(pk__in=[eu, us, held]).update(updated_at=timezone.now() - dt.timedelta(days=200))
    assert editor.get(f"{BASE}/hiring/retention").status_code == 403
    preview = {r["name"]: r["count"] for r in _client(world["admin"]).get(f"{BASE}/hiring/retention").json()}
    assert preview == {"rejected_eu": 1, "rejected_us": 0, "rejected_other": 0, "prospects": 0}
    assert _client(world["editor"]).post(f"{BASE}/hiring/retention").status_code == 403
    counts = _client(world["admin"]).post(f"{BASE}/hiring/retention").json()
    assert counts["rejected_eu"] == 1
    gone = AppRecord.objects.get(pk=eu).data
    assert gone["name"] == "(removed)" and gone["email"] == "(removed)" and gone["status"] == "rejected"
    assert not AppRecord.objects.filter(collection="note", data__application=eu).exists()
    assert AppRecord.objects.filter(collection="note", data__application=held).exists()
    assert AppRecord.objects.get(pk=held).data["email"] == "held@example.org"
    # Run again: nothing more is due.
    assert services.retention_apply(hiring)["rejected_eu"] == 0


def test_a_subject_export_gathers_what_the_app_holds_about_one_person(world, hiring):
    editor = _client(world["editor"])
    job = _job(editor)
    cand = _cand(editor, job)
    _act(editor, "add_note", {"application": cand, "body": "Strong portfolio"})
    assert editor.get(f"{BASE}/hiring/subject-export?email=ada@example.org").status_code == 403
    out = _client(world["admin"]).get(f"{BASE}/hiring/subject-export?email=ADA@example.org").json()
    assert [r["id"] for r in out["records"]["application"]] == [cand]
    assert out["records"]["note"][0]["body"] == "Strong portfolio"


def test_a_workspace_cannot_add_then_steps_or_link_actions(world, hiring):
    admin = _client(world["admin"])
    admin.post(f"{BASE}/hiring/draft", {}, content_type=J)
    r = admin.post(f"{BASE}/hiring/draft/ops", {"op": "add_action", "key": "x_sneaky", "action": {
        "role": "member", "op": "create", "collection": "note", "input": ["application", "body"],
        "then": [{"op": "update", "ref": "application", "set": {"status": "hired"}}]}}, content_type=J)
    assert r.status_code == 422 and "then" in r.json()["detail"]


def test_files_of_an_anonymized_candidate_are_deleted(world, hiring, settings):
    settings.DEBUG = True  # no bucket here: the dev fallback keeps the bytes in the row
    editor = _client(world["editor"])
    job = _job(editor)
    f = services.upload(hiring, space="live", name="cv.pdf", content_type="application/pdf", body=b"%PDF-1.4 x",
                        label="t", client_ip=None, accept=[".pdf"], max_mb=5)
    cand = _cand(editor, job, resume=f.pk)
    _act(editor, "reject", {"rejection_reason": "Other"}, record=cand)
    AppRecord.objects.filter(pk=cand).update(updated_at=timezone.now() - dt.timedelta(days=400))
    services.retention_apply(hiring)
    assert not AppFile.objects.filter(pk=f.pk).exists()
    assert "resume" not in AppRecord.objects.get(pk=cand).data
