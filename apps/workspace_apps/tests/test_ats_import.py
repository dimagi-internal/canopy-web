"""The ATS importer: the export crawl (with HTTP faked — no network) and the load into Hiring."""
from __future__ import annotations

import hashlib
import json
import os
from io import StringIO
from unittest import mock

import pytest
import requests
from django.core.management import CommandError, call_command
from requests.structures import CaseInsensitiveDict

from apps.workspace_apps import ats_import, services
from apps.workspace_apps.models import AppFile, AppRecord, AppRecordEvent

pytestmark = pytest.mark.django_db

BASE = "https://ats.example/v3"
TOKEN_URL = "https://ats.example/token"


# --- a fake export API --------------------------------------------------------------------


def _resp(status=200, body=None, headers=None, content=None, url=""):
    r = requests.Response()
    r.status_code = status
    r._content = content if content is not None else json.dumps(body if body is not None else []).encode()
    r.headers = CaseInsensitiveDict(headers or {})
    r.url = url
    return r


class FakeSession:
    """Answers from a {(method, url): [responses…]} script; the last one repeats."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []
        token = _resp(body={"access_token": "tok", "expires_in": 3600})
        self.routes.setdefault(("POST", TOKEN_URL), [token])

    def request(self, method, url, params=None, headers=None, **kw):
        self.calls.append({"method": method, "url": url, "params": params, "headers": headers or {}, **kw})
        script = self.routes.get((method, url))
        if script is None:
            return _resp(404, {"error": "not found"}, url=url)
        r = script.pop(0) if len(script) > 1 else script[0]
        r.url = r.url or url
        return r

    def gets(self):
        return [c for c in self.calls if c["method"] == "GET"]


class Clock:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def sleep(self, s):
        self.slept.append(s)
        self.now += s

    def __call__(self):
        return self.now


def _client(session, clock=None, **kw):
    clock = clock or Clock()
    return ats_import.ExportClient(client_id="id", client_secret="secret", base_url=BASE, token_url=TOKEN_URL,
                                  session=session, sleep=clock.sleep, clock=clock, **kw)


# --- extract ---------------------------------------------------------------------------


def test_pagination_follows_the_next_link_exactly_and_a_rerun_skips_what_is_done(tmp_path):
    nxt = f"{BASE}/users?cursor=opaque%3D%3D"
    s = FakeSession({
        ("GET", f"{BASE}/users"): [_resp(body=[{"id": 1}, {"id": 2}], headers={"Link": f'<{nxt}>; rel="next"'})],
        ("GET", nxt): [_resp(body=[{"id": 3}])],
    })
    summary = ats_import.Extractor(_client(s), tmp_path, only=["users"], log=lambda *_: None).run()
    first, second = s.gets()
    assert first["params"] == ats_import.PAGE_PARAMS and first["headers"]["Authorization"] == "Bearer tok"
    assert second["url"] == nxt and second["params"] is None  # the cursor alone, or v3 answers 422
    assert summary["users"]["status"] == "complete" and summary["users"]["records"] == 3
    assert json.loads((tmp_path / "raw/users/page-00002.json").read_text()) == [{"id": 3}]
    assert [r["id"] for r in ats_import.Archive(tmp_path).records("users")] == [1, 2, 3]

    s2 = FakeSession({})
    ats_import.Extractor(_client(s2), tmp_path, only=["users"], log=lambda *_: None).run()
    assert s2.gets() == []


def test_an_interrupted_crawl_resumes_from_its_saved_cursor(tmp_path):
    nxt = f"{BASE}/jobs?cursor=c2"
    s = FakeSession({("GET", f"{BASE}/jobs"): [_resp(body=[{"id": 1}], headers={"Link": f'<{nxt}>; rel="next"'})],
                     ("GET", nxt): [_resp(400, {"error": "boom"})]})
    with pytest.raises(ats_import.ExportError):
        ats_import.Extractor(_client(s), tmp_path, only=["jobs"], log=lambda *_: None).run()
    s2 = FakeSession({("GET", nxt): [_resp(body=[{"id": 2}])]})
    summary = ats_import.Extractor(_client(s2), tmp_path, only=["jobs"], log=lambda *_: None).run()
    assert [c["url"] for c in s2.gets()] == [nxt]
    assert summary["jobs"]["records"] == 2 and summary["jobs"]["status"] == "complete"


def test_a_429_waits_out_retry_after_and_a_5xx_backs_off():
    clock = Clock()
    s = FakeSession({("GET", f"{BASE}/x"): [_resp(429, headers={"Retry-After": "7"}), _resp(503), _resp(502),
                                           _resp(body=[])]})
    r = _client(s, clock).get(f"{BASE}/x")
    assert r.status_code == 200
    assert clock.slept == [7.0, 2.0, 4.0]


def test_the_request_budget_is_spent_before_the_ats_has_to_say_429():
    clock = Clock()
    s = FakeSession({("GET", f"{BASE}/x"): [_resp(body=[])]})
    c = _client(s, clock, budget=2, window=30.0)
    for _ in range(3):
        c.get(f"{BASE}/x")
    assert clock.slept == [30.0]


def test_an_endpoint_the_ats_does_not_have_is_recorded_and_the_crawl_moves_on(tmp_path):
    s = FakeSession({("GET", f"{BASE}/departments"): [_resp(body=[{"id": 1, "name": "Eng"}])]})
    summary = ats_import.Extractor(_client(s), tmp_path, only=["offices", "departments"], log=lambda *_: None).run()
    assert summary["offices"]["status"] == "missing"
    assert summary["departments"]["status"] == "complete"


def test_attachments_are_downloaded_while_crawling_without_the_bearer(tmp_path):
    signed = "https://s3.amazonaws.com/gh-prod/cv.pdf?Signature=abc&Expires=123"
    pdf = b"%PDF-1.4 a resume"
    s = FakeSession({
        ("GET", f"{BASE}/candidates"): [_resp(body=[{"id": 2000, "attachments": [
            {"id": 77, "filename": "cv.pdf", "url": signed, "type": "resume",
             "created_at": "2025-01-01T00:00:00Z"}]}])],
        ("GET", signed): [_resp(content=pdf, headers={"Content-Type": "application/pdf"})],
    })
    ats_import.Extractor(_client(s), tmp_path, only=["candidates"], log=lambda *_: None).run()
    download = next(c for c in s.gets() if c["url"] == signed)
    assert "Authorization" not in download["headers"]
    entry = json.loads((tmp_path / "attachments.json").read_text())["a77"]
    assert entry["status"] == "ok" and entry["candidate_id"] == 2000 and entry["type"] == "resume"
    assert entry["sha256"] == hashlib.sha256(pdf).hexdigest()
    assert (tmp_path / entry["path"]).read_bytes() == pdf


def test_a_per_record_endpoint_is_crawled_once_per_parent(tmp_path):
    s = FakeSession({("GET", f"{BASE}/jobs"): [_resp(body=[{"id": 1}, {"id": 2}])],
                     ("GET", f"{BASE}/jobs/1/stages"): [_resp(body=[{"id": 10, "name": "Screen"}])],
                     ("GET", f"{BASE}/jobs/2/stages"): [_resp(body=[{"id": 20, "name": "Offer"}])]})
    eps = {"jobs": "/jobs", "job_stages": {"path": "/jobs/{id}/stages", "per": "jobs"}}
    summary = ats_import.Extractor(_client(s), tmp_path, endpoints=eps, log=lambda *_: None).run()
    assert summary["job_stages"] == {"status": "complete", "streams": 2, "records": 2, "pages": 2}
    assert {(r["id"], r["_parent"]) for r in ats_import.Archive(tmp_path).records("job_stages")} == {(10, "1"), (20, "2")}


def test_credentials_are_required():
    with pytest.raises(ats_import.ExportError, match="ATS_CLIENT_ID"):
        ats_import.ExportClient(client_id="", client_secret="")


# --- load ------------------------------------------------------------------------------

CV = b"%PDF-1.4 Ada's resume"
LETTER = b"%PDF-1.4 offer letter"


def _write(out, name, records):
    (out / "raw" / name).mkdir(parents=True, exist_ok=True)
    (out / "raw" / name / "page-00001.json").write_text(json.dumps(records))
    (out / "raw" / name / "_state.json").write_text(json.dumps({"status": "complete", "records": len(records)}))


def _archive(out, **overrides):
    data = {
        "users": [{"id": 1, "name": "Rita", "primary_email_address": "rita@dimagi.com"},
                  {"id": 2, "name": "Hank", "primary_email_address": "Hank@dimagi.com"}],
        "departments": [{"id": 10, "name": "Engineering"}],
        "sources": [{"id": 30, "public_name": "Hacker News"}],
        "jobs": [{"id": 100, "name": "Senior Engineer", "status": "open", "requisition_id": "ENG-7",
                  "departments": [{"id": 10, "name": "Engineering"}],
                  "offices": [{"id": 20, "name": "Cambridge", "location": {"name": "Cambridge, MA"}}],
                  "opened_at": "2025-01-05T10:00:00Z", "created_at": "2025-01-04T10:00:00Z",
                  "openings": [{"id": 500, "opening_id": "ENG-7-1", "status": "closed", "application_id": 3000,
                                "close_reason": {"id": 1, "name": "Hired"}, "opened_at": "2025-01-05T10:00:00Z",
                                "closed_at": "2025-03-01T10:00:00Z"}],
                  "hiring_team": {"hiring_managers": [{"id": 2}], "recruiters": [{"id": 1, "responsible": True}]},
                  "keyed_custom_fields": {"salary_range": {"type": "currency_range", "value": {
                      "min_value": 120000, "max_value": 150000, "unit": "USD"}}}}],
        "job_posts": [{"id": 600, "job_id": 100, "live": True, "external": True,
                       "content": "<p>Build &amp; ship.</p>", "location": {"name": "Remote - US"}}],
        "job_stages": [{"id": 700, "job_id": 100, "name": "Technical Interview", "priority": 2, "interviews": [
            {"id": 710, "name": "Coding exercise", "estimated_minutes": 60, "default_interviewer_users": [{"id": 2}],
             "interview_kit": {"content": "<p>Be kind</p>",
                               "questions": [{"question": "Walk me through a design"}]}}]}],
        "candidates": [
            {"id": 2000, "first_name": "Ada", "last_name": "Lovelace", "can_email": True, "tags": ["python"],
             "email_addresses": [{"value": "ada@example.com", "type": "personal"}],
             "phone_numbers": [{"value": "+44 20 1234", "type": "mobile"}],
             "addresses": [{"value": "12 Baker St, London, United Kingdom", "type": "home"}],
             "social_media_addresses": [{"value": "https://www.linkedin.com/in/ada"}]},
            {"id": 2001, "first_name": "Bo", "last_name": "Prospect", "email_addresses": [{"value": "bo@example.com"}],
             "addresses": [{"value": "Nairobi, Kenya"}], "can_email": False},
            {"id": 2002, "first_name": "No", "last_name": "Email", "email_addresses": []},
        ],
        "applications": [
            {"id": 3000, "candidate_id": 2000, "status": "hired", "applied_at": "2025-02-01T09:00:00Z",
             "last_activity_at": "2025-03-01T00:00:00Z", "jobs": [{"id": 100}],
             "current_stage": {"id": 1, "name": "Offer"}, "source": {"id": 30, "public_name": "Hacker News"},
             "credited_to": {"id": 1}},
            {"id": 3001, "candidate_id": 2000, "status": "rejected", "applied_at": "2024-05-01T09:00:00Z",
             "rejected_at": "2024-06-01T00:00:00Z", "jobs": [{"id": 100}],
             "rejection_reason": {"id": 40, "name": "Culture add concerns"},
             "source": {"public_name": "LinkedIn (Prospecting)"}},
            {"id": 3002, "candidate_id": 2001, "prospect": True, "status": "active", "jobs": [],
             "prospect_detail": {"prospect_pool": {"id": 90, "name": "Future engineers"}},
             "source": {"public_name": "Referral"}, "rejection_reason": {"name": "Withdrew"}},
            {"id": 3003, "candidate_id": 2002, "status": "active", "jobs": [{"id": 100}]},
        ],
        "eeoc": [{"application_id": 3000, "gender": {"id": 1, "description": "Female"},
                  "race": {"id": 2, "description": "Two or More Races"},
                  "veteran_status": {"id": 1, "description": "I am not a protected veteran"},
                  "disability_status": {"id": 9, "description": "Something new"}}],
        "stage_history": [{"id": 800, "application_id": 3000, "stage": {"id": 1, "name": "Technical Interview"},
                           "entered_at": "2025-02-10T12:00:00Z", "user": {"id": 1}}],
        "scheduled_interviews": [{"id": 900, "application_id": 3000,
                                  "interview": {"id": 710, "name": "Coding exercise"},
                                  "start": {"date_time": "2025-02-11T15:00:00Z"},
                                  "end": {"date_time": "2025-02-11T16:00:00Z"}, "status": "complete",
                                  "video_conferencing_url": "https://meet.google.com/abc",
                                  "interviewers": [{"id": 2, "scorecard_id": 950}]}],
        "scorecards": [{"id": 950, "application_id": 3000, "interviewer": {"id": 2},
                        "overall_recommendation": "strong_yes", "submitted_at": "2025-02-12T00:00:00Z",
                        "attributes": [{"name": "Communication", "rating": "yes", "note": "Clear"},
                                       {"name": "Python", "rating": "definitely_not"},
                                       {"name": "Go", "rating": "no_decision"}],
                        "questions": [{"id": None, "question": "Key Take-Aways", "answer": "<p>Hire</p>"},
                                      {"id": None, "question": "Private Notes", "answer": "salary ask high"},
                                      {"id": 5, "question": "Design?", "answer": "Good"}]}],
        "offers": [{"id": 990, "application_id": 3000, "job_id": 100, "status": "accepted", "opening": {"id": 500},
                    "sent_at": "2025-02-20T00:00:00Z", "resolved_at": "2025-02-21T00:00:00Z", "starts_at": "2025-03-15",
                    "keyed_custom_fields": {"salary": {"type": "currency",
                                                       "value": {"amount": 140000, "unit": "USD"}}}},
                   {"id": 991, "application_id": 3000, "job_id": 100, "status": "deprecated"}],
        "notes": [{"id": 1100, "candidate_id": 2000, "body": "Great call", "user": {"id": 1}, "visibility": "public"},
                  {"id": 1101, "candidate_id": 2000, "body": "Comp details", "user": {"id": 1},
                   "visibility": "admin_only"}],
        "email_templates": [{"id": 1200, "name": "Rejection", "type": "candidate_rejection",
                             "subject": "Your application",
                             "body": "Hi {{CANDIDATE_FIRST_NAME}}, thanks for applying to {{ JOB_NAME }}."}],
    }
    data.update(overrides)
    for name, recs in data.items():
        _write(out, name, recs)
    (out / "files").mkdir(exist_ok=True)
    (out / "files/a77.pdf").write_bytes(CV)
    (out / "files/a78.pdf").write_bytes(LETTER)
    (out / "attachments.json").write_text(json.dumps({
        "a77": {"filename": "ada_cv.pdf", "type": "resume", "candidate_id": 2000, "status": "ok",
                "path": "files/a77.pdf", "sha256": hashlib.sha256(CV).hexdigest(), "created_at": "2025-02-01"},
        "a78": {"filename": "offer.pdf", "type": "offer_letter", "offer_id": 990, "application_id": 3000,
                "status": "ok", "path": "files/a78.pdf", "sha256": hashlib.sha256(LETTER).hexdigest()},
        "a79": {"filename": "cover.pdf", "type": "cover_letter", "candidate_id": 2000, "status": "failed: HTTP 403"},
    }))
    return data


@pytest.fixture()
def hiring(world, settings):  # the conftest's `world`; its own install, with settings
    settings.DEBUG = True  # no bucket here: the dev fallback keeps file bytes in the row
    settings.CANOPY_ATTACHMENTS_BUCKET = ""
    services.sync_bundled()
    return services.install(workspace_id="dimagi", user=world["admin"], template="hiring")


def _recs(inst, coll):
    return list(AppRecord.objects.filter(installation=inst, collection=coll).order_by("id"))


def _one(inst, coll, **where):
    return next(r for r in _recs(inst, coll) if all(r.data.get(k) == v for k, v in where.items()))


def _load(inst, out, **kw):
    return ats_import.Loader(inst, out, log=lambda *_: None, **kw).run()


def test_an_ats_archive_loads_into_the_hiring_app(hiring, tmp_path):
    _archive(tmp_path)
    report = _load(hiring, tmp_path)

    job = _one(hiring, "job", requisition_id="ENG-7").data
    assert job["title"] == "Senior Engineer" and job["status"] == "open" and job["post_status"] == "live"
    assert (job["department"], job["office"], job["location"]) == ("Engineering", "Cambridge", "Remote - US")
    assert job["description"] == "Build & ship."
    assert (job["pay_min"], job["pay_max"], job["pay_currency"]) == (120000, 150000, "USD")
    assert (job["hiring_manager"], job["recruiter"]) == ("hank@dimagi.com", "rita@dimagi.com")
    assert job["hiring_team"] == "hank@dimagi.com, rita@dimagi.com" and job["openings"] == 1
    assert "workplace" not in job  # the old ATS never said the job was remote

    ada = _one(hiring, "application", applied_on="2025-02-01")
    a = ada.data
    assert (a["name"], a["email"], a["region"], a["country"]) == ("Ada Lovelace", "ada@example.com", "EU/UK",
                                                                  "United Kingdom")
    assert (a["status"], a["stage"], a["kind"]) == ("hired", "Hired", "applicant")
    assert (a["source"], a["source_tag"]) == ("Other", "Hacker News")
    assert a["linkedin"] == "https://www.linkedin.com/in/ada" and a["credited_to"] == "rita@dimagi.com"
    assert ada.created_at.year == 2025  # backdated: retention counts from the old ATS's dates, not the import's
    cv = AppFile.objects.get(pk=a["resume"])
    assert cv.sha256 == hashlib.sha256(CV).hexdigest() and cv.claimed and services.file_bytes(cv) == CV

    old = _one(hiring, "application", applied_on="2024-05-01").data
    assert (old["status"], old["stage"], old["rejection_reason"]) == ("rejected", "Rejected", "Other")
    assert "Previous ATS reason: Culture add concerns" in old["rejection_note"]
    assert (old["source"], old["source_tag"]) == ("LinkedIn", "LinkedIn (Prospecting)")

    bo = _one(hiring, "application", kind="prospect").data
    assert (bo["region"], bo["country"], bo["source"], bo["do_not_email"]) == ("Kenya", "Kenya", "Referral", True)
    assert bo["pool"] == _one(hiring, "pool", name="Future engineers").pk and "job" not in bo
    assert bo["rejection_reason"] == "Withdrew" and "rejection_note" not in bo

    assert report["skipped"]["application"] == {"missing email": 1}
    opening = _one(hiring, "opening", opening_id="ENG-7-1").data
    assert (opening["status"], opening["close_reason"], opening["hired_application"]) == ("closed", "Hired", ada.pk)

    step = _one(hiring, "interview_step", name="Coding exercise").data
    assert (step["stage"], step["duration"], step["prep"], step["interviewers"]) == (
        "Technical Interview", 60, "Be kind", "hank@dimagi.com")
    change = _one(hiring, "stage_change", application=ada.pk).data
    assert (change["to_stage"], change["day"], change["by"]) == ("Technical Interview", "2025-02-10", "rita@dimagi.com")
    iv = _one(hiring, "interview", application=ada.pk)
    assert (iv.data["kind"], iv.data["date"], iv.data["time"], iv.data["duration"], iv.data["status"]) == (
        "Technical", "2025-02-11", "15:00", 60, "Done")
    assert iv.data["step"] == _one(hiring, "interview_step", name="Coding exercise").pk

    sc = _one(hiring, "scorecard", application=ada.pk).data
    assert (sc["rating"], sc["interviewer"], sc["interview"]) == ("Strong yes", "hank@dimagi.com", iv.pk)
    assert sc["attribute_ratings"] == "Communication: Yes\nPython: Strong no"
    assert (sc["key_takeaways"], sc["private_notes"]) == ("Hire", "salary ask high")
    assert "Communication — Clear" in sc["notes"] and "Design?\nGood" in sc["notes"]

    offer = _one(hiring, "offer", status="hired").data
    assert (offer["salary"], offer["currency"], offer["start_date"], offer["title"]) == (
        140000, "USD", "2025-03-15", "Senior Engineer")
    assert offer["opening"] == _one(hiring, "opening", opening_id="ENG-7-1").pk
    assert services.file_bytes(AppFile.objects.get(pk=offer["letter"])) == LETTER
    assert _one(hiring, "offer", status="withdrawn")

    public, private = _one(hiring, "note", body="Great call"), _one(hiring, "note", private_body="Comp details")
    assert public.data["application"] == ada.pk == private.data["application"]
    tpl = _one(hiring, "email_template", name="Rejection").data
    assert tpl["kind"] == "Rejection" and tpl["body"] == "Hi {{name}}, thanks for applying to {{job.title}}."

    recs = AppRecord.objects.filter(installation=hiring)
    events = AppRecordEvent.objects.filter(installation=hiring)
    assert {e.action for e in events} == {ats_import.ACTION} and events.count() == recs.count()
    assert set(events.values_list("record_id", flat=True)) == set(recs.values_list("id", flat=True))

    row = next(r for r in report["reconcile"] if r["endpoint"] == "applications")
    assert (row["in_archive"], row["written"], row["skipped"]) == (4, 3, 1)
    assert report["attachments"]["listed"] == 3 and report["attachments"]["downloaded"] == 2
    # Ada's one résumé is on both her applications; the letter is on offer 990 only, not the superseded 991.
    assert (report["attachments"]["attached"], report["attachments"]["stored"]) == (3, 2)
    assert _one(hiring, "application", applied_on="2024-05-01").data["resume"] == a["resume"]
    assert "letter" not in _one(hiring, "offer", status="withdrawn").data
    assert "Hacker News" in report["unmapped"]["source"]
    assert "Reconciliation" in ats_import.format_report(report)


def test_eeo_answers_map_onto_the_manifest_options_or_stay_empty(hiring, tmp_path):
    _archive(tmp_path)
    _load(hiring, tmp_path)
    a = _one(hiring, "application", applied_on="2025-02-01").data
    assert (a["eeo_gender"], a["eeo_race"], a["eeo_veteran"]) == ("Female", "Two or more races",
                                                                  "I am not a protected veteran")
    assert "eeo_disability" not in a  # "Something new" is not an answer the app knows
    assert ats_import.eeo_answers({"gender": "Decline To Self Identify", "veteran_status": {
        "description": "I identify as one or more of the classifications of protected veteran"},
        "disability_status": {"description": "No, I don't have a disability"}}) == {
        "eeo_gender": "Decline to answer", "eeo_veteran": "I am a protected veteran", "eeo_disability": "No"}
    assert ats_import.eeo_answers(None) == {}


def test_an_unknown_rejection_reason_becomes_other_and_a_workspace_reason_is_kept(hiring, tmp_path):
    _archive(tmp_path)
    loader = ats_import.Loader(hiring, tmp_path, log=lambda *_: None)
    loader._lookups()
    assert loader._rejection({"rejection_reason": {"name": "Ghosted us"}, "status": "rejected"}) == (
        "Other", "Previous ATS reason: Ghosted us")
    assert loader._rejection({"rejection_reason": {"name": "position filled"}}) == ("Position filled", None)
    assert loader._rejection({"rejection_reason_id": 40, "status": "rejected"})[0] == "Other"
    loader.m["collections"]["application"]["fields"]["rejection_reason"]["options"].append("Ghosted us")
    assert loader._rejection({"rejection_reason": {"name": "Ghosted us"}}) == ("Ghosted us", None)


@pytest.mark.parametrize("address,expected", [
    ("12 Baker St, London, United Kingdom", ("EU/UK", "United Kingdom")),
    ("Cambridge, MA 02139", ("US", "United States")),
    ("Berlin, Germany", ("EU/UK", "Germany")),
    ("Bengaluru, Karnataka, India", ("India", "India")),
    ("Cape Town, South Africa", ("South Africa", "South Africa")),
    ("Lima, Peru", ("Other", "Peru")),
    ("", ("Other", "")),
])
def test_region_comes_from_the_address(address, expected):
    assert ats_import.region_of(address) == expected


def test_a_reload_is_idempotent_updates_what_changed_and_keeps_canopy_edits(hiring, tmp_path):
    data = _archive(tmp_path)
    _load(hiring, tmp_path)
    n_recs, n_events = AppRecord.objects.count(), AppRecordEvent.objects.count()
    n_files = AppFile.objects.count()

    again = _load(hiring, tmp_path)
    assert (AppRecord.objects.count(), AppRecordEvent.objects.count(), AppFile.objects.count()) == (
        n_recs, n_events, n_files)
    assert all(set(c) == {"unchanged"} for c in again["collections"].values()), again["collections"]

    bo = _one(hiring, "application", kind="prospect")
    bo.data = {**bo.data, "tags": "edited in canopy"}
    bo.save()
    data["jobs"][0]["status"] = "closed"
    data["candidates"][1]["tags"] = ["from the old ats"]
    _write(tmp_path, "jobs", data["jobs"])
    _write(tmp_path, "candidates", data["candidates"])
    third = _load(hiring, tmp_path)
    assert third["collections"]["job"] == {"updated": 1}
    assert third["collections"]["application"]["kept (changed in canopy)"] == 1
    assert _one(hiring, "job", requisition_id="ENG-7").data["status"] == "closed"
    bo.refresh_from_db()
    assert bo.data["tags"] == "edited in canopy"
    update = AppRecordEvent.objects.get(op="update")
    assert update.action == ats_import.ACTION and update.changes["status"] == ["open", "closed"]
    assert AppRecord.objects.count() == n_recs


def test_a_dry_run_reports_counts_and_writes_nothing(hiring, tmp_path):
    _archive(tmp_path)
    report = _load(hiring, tmp_path, dry_run=True)
    assert report["dry_run"] and report["collections"]["application"] == {"created": 3}
    assert report["collections"]["job"] == {"created": 1}
    assert AppRecord.objects.count() == AppRecordEvent.objects.count() == AppFile.objects.count() == 0
    assert not (tmp_path / "idmap.json").exists()
    assert report["attachments"]["would attach"] == 3


def test_an_idmap_from_another_installation_is_refused(hiring, tmp_path):
    _archive(tmp_path)
    (tmp_path / "idmap.json").write_text(json.dumps({"installation": hiring.pk + 99, "maps": {}}))
    with pytest.raises(ValueError, match="belongs to installation"):
        ats_import.Loader(hiring, tmp_path)


def test_the_command_dry_runs_and_never_takes_credentials_from_argv(hiring, tmp_path):
    _archive(tmp_path)
    out = StringIO()
    call_command("import_ats", "--workspace", "dimagi", "--app", "hiring", "--out", str(tmp_path),
                 "--dry-run", stdout=out)
    assert "DRY RUN" in out.getvalue() and "application" in out.getvalue()
    assert AppRecord.objects.count() == 0
    with mock.patch.dict(os.environ, {"ATS_CLIENT_ID": "", "ATS_CLIENT_SECRET": ""}):
        with pytest.raises(CommandError, match="ATS_CLIENT_ID"):
            call_command("import_ats", "--workspace", "dimagi", "--out", str(tmp_path), "--extract")
    with pytest.raises(CommandError, match="no app"):
        call_command("import_ats", "--workspace", "dimagi", "--app", "nope", "--out", str(tmp_path), "--load")
    with pytest.raises(CommandError, match="unrecognized arguments"):
        call_command("import_ats", "--workspace", "dimagi", "--out", str(tmp_path), "--client-secret", "x")
