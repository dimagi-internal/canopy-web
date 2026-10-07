"""A review link is often someone's first canopy-web page (canopy-web#1266-#1271).

Pins the server half of the first-use fixes: the narrative has a name, the page
learns whether THIS caller may decide, a member can save edits without
deciding, and a suggestion reaches the review's owner.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import Client

from apps.reviews.models import ReviewRequest
from apps.reviews.titles import narrative_title, phase_words
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.services import ensure_member
from apps.workspaces.testing import a_workspace

User = get_user_model()
BASE = "/api/reviews"

LONG_STORY = (
    "This is a quick overview of how we register waterpoints. A field worker registers "
    "each one on their phone, with its name, village, a GPS point and a photo."
)
REQ = {
    "schema_version": 3,
    "run_id": "chlorine-dispenser-walkthroughs-2026-10-07-002",
    "gate": "concept_change",
    "decisions": [{"id": "narrative-verdict", "prompt": "Approve?", "options": ["approve", "redraft"],
                   "recommended": "approve", "class": "concept_change"}],
    "narrative": LONG_STORY,
    "narration": [{"scene": 1, "id": "s1", "text": "Register waterpoints."}],
    "autonomous_audit": [],
}
EDIT = {"edited_scenes": [{"id": "s1", "narration": "Register every waterpoint."}]}


def _user(email, role=None):
    u = User.objects.create_user(username=email, email=email)
    if role:
        ensure_member(a_workspace(), u, role)
    return u


def _client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def owner(db):
    return _user("ace@dimagi-ai.com", WorkspaceMembership.EDITOR)


@pytest.fixture
def review(owner):
    r = ReviewRequest.objects.create(
        owner=owner, run_id=REQ["run_id"], gate="concept_change", request_json=REQ,
        visibility="link", workspace=a_workspace(), narrative_slug="chlorine-dispenser-walkthroughs",
    )
    r.ensure_share_token()
    return r


# --- #1271: a narrative has a name -----------------------------------------------------


def test_long_first_line_is_not_the_title():
    assert narrative_title(REQ, "chlorine-dispenser-walkthroughs") == "Chlorine Dispenser Walkthroughs"


def test_explicit_title_wins():
    assert narrative_title({**REQ, "title": "Chlorine dispensers"}, "x") == "Chlorine dispensers"


def test_short_first_line_is_still_the_title():
    assert narrative_title({"narrative": "Maya runs the study."}, "slug") == "Maya runs the study."


def test_no_slug_clips_at_a_word_never_mid_word():
    t = narrative_title(REQ, None)
    assert t.endswith("…") and len(t) <= 71
    assert not t[:-1].endswith("poi")
    assert LONG_STORY.startswith(t[:-1])


def test_phase_is_plain_words():
    assert phase_words("concept_change", "pending") == "Story review · awaiting a decision"


@pytest.mark.django_db
def test_detail_carries_the_title(review):
    body = Client().get(f"{BASE}/{review.id}/").json()
    assert body["title"] == "Chlorine Dispenser Walkthroughs"


# --- #1268: the page learns whether THIS caller may decide ------------------------------


@pytest.mark.django_db
def test_can_decide_for_an_editor_only(review):
    assert _client(review.owner).get(f"{BASE}/{review.id}/").json()["can_decide"] is True
    viewer = _user("viewer@dimagi.com", WorkspaceMembership.VIEWER)
    assert _client(viewer).get(f"{BASE}/{review.id}/").json()["can_decide"] is False
    # Signed in, but not a member — the case a Dimagi-domain guest reviewer is in.
    stranger = _user("sagar@dimagi.com")
    assert _client(stranger).get(f"{BASE}/{review.id}/?t={review.share_token}").json()["can_decide"] is False
    assert Client().get(f"{BASE}/{review.id}/").json()["can_decide"] is False


@pytest.mark.django_db
def test_signed_in_non_member_can_still_suggest_with_the_token(review):
    stranger = _user("sagar@dimagi.com")
    resp = _client(stranger).post(
        f"{BASE}/{review.id}/suggest/?t={review.share_token}",
        data={"response_json": EDIT}, content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    review.refresh_from_db()
    assert review.suggestions_json[0]["name"] == "sagar@dimagi.com"


# --- #1266: a member saves edits without deciding ---------------------------------------


@pytest.mark.django_db
def test_member_saves_edits_without_resolving(review):
    member = _user("jj@dimagi.com", WorkspaceMembership.EDITOR)
    resp = _client(member).post(  # no ?t= — a session, not the share link
        f"{BASE}/{review.id}/suggest/", data={"response_json": EDIT}, content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    review.refresh_from_db()
    assert review.status == "pending" and review.response_json is None
    assert review.suggestions_json[0]["name"] == "jj@dimagi.com"


@pytest.mark.django_db
def test_member_save_is_csrf_checked(review):
    member = _user("jj@dimagi.com", WorkspaceMembership.EDITOR)
    c = Client(enforce_csrf_checks=True)
    c.force_login(member)
    resp = c.post(f"{BASE}/{review.id}/suggest/", data={"response_json": EDIT},
                  content_type="application/json")
    assert resp.status_code == 403


@pytest.mark.django_db
def test_non_member_without_token_still_refused(review):
    resp = _client(_user("x@example.com")).post(
        f"{BASE}/{review.id}/suggest/", data={"response_json": EDIT}, content_type="application/json",
    )
    assert resp.status_code == 403


# --- #1269: a suggestion reaches the owner ----------------------------------------------


@pytest.mark.django_db
def test_suggestion_emails_the_owner(review):
    mail.outbox.clear()
    resp = Client().post(
        f"{BASE}/{review.id}/suggest/?t={review.share_token}",
        data={"response_json": EDIT, "name": "Sagar"}, content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    assert len(mail.outbox) == 1
    m = mail.outbox[0]
    assert m.to == ["ace@dimagi-ai.com"]
    assert "Sagar" in m.subject and "Chlorine Dispenser Walkthroughs" in m.subject
    assert f"/review/{review.id}/" in m.body


@pytest.mark.django_db
def test_a_mail_failure_never_loses_the_suggestion(review, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("SES down")

    monkeypatch.setattr("apps.workspaces.services._send", boom)
    resp = Client().post(
        f"{BASE}/{review.id}/suggest/?t={review.share_token}",
        data={"response_json": EDIT}, content_type="application/json",
    )
    assert resp.status_code == 200
    review.refresh_from_db()
    assert len(review.suggestions_json) == 1
