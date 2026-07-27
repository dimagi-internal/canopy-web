"""End-to-end on REAL narrative shapes: import the arc, resolve it, read it
anonymously with a token, and leave a suggestion — exactly what Sophie does."""
import json

import pytest
import yaml
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from apps.feedback.models import Feedback
from apps.reviews.models import ReviewRequest
from apps.storyboards.models import Storyboard
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db

ARC = {
    "slug": "rf-surveys",
    "title": "Proving a programme works",
    "lede": "Three acts: design the measurement, commission it, check the checkers.",
    "capability": "suggest",
    "acts": [
        {"title": "Design the comparison", "entries": ["microplans-study-groups"]},
        {"title": "Commission someone independent", "entries": ["create-survey-solicitation"]},
        {"title": "Check the checkers", "entries": ["verified-monitoring"]},
    ],
}

REAL = {
    "microplans-study-groups": (14, "Design the comparison"),
    "create-survey-solicitation": (12, "Commission"),
    "verified-monitoring": (17, "Check the checkers"),
}


def test_the_rf_surveys_arc_end_to_end(tmp_path):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)

    # Real narrative shapes: several versions each, newest carrying the story.
    for slug, (version, _) in REAL.items():
        for v in (version - 1, version):
            ReviewRequest.objects.create(
                run_id=f"{slug}-2026-07-23-{v:03d}",
                request_json={
                    "gate": "concept_change",
                    "narrative_slug": slug,
                    "narrative": f"{slug} story at v{v}.",
                    "narration": [
                        {"id": "the-goal", "title": "The goal", "text": f"Opening beat, v{v}."},
                        {"id": "the-proof", "title": "The proof", "text": f"Second beat, v{v}."},
                    ],
                },
                gate="concept_change",
                visibility="link",
                workspace_id="dimagi",
                version=v,
            )

    board_yaml = tmp_path / "storyboard.yaml"
    board_yaml.write_text(yaml.dump(ARC))
    call_command("import_storyboard", str(board_yaml), workspace="dimagi")
    board = Storyboard.objects.get(slug="rf-surveys")
    token = board.ensure_share_token()

    # --- Sophie opens the link, logged out.
    anon = Client()
    r = anon.get(f"/api/storyboards/rf-surveys?t={token}")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["capability"] == "suggest"
    assert [a["title"] for a in body["acts"]] == [
        "Design the comparison", "Commission someone independent", "Check the checkers",
    ]
    got = {e["narrative_slug"]: e for a in body["acts"] for e in a["entries"]}
    for slug, (version, _) in REAL.items():
        assert got[slug]["published"] is True, slug
        assert got[slug]["version"] == version, (slug, got[slug]["version"])

    # --- she opens one narrative and sees the before/after
    r = anon.get(f"/api/storyboards/rf-surveys/narratives/verified-monitoring?t={token}")
    assert r.status_code == 200, r.content
    n = r.json()
    assert n["version"] == 17 and n["previous_version"] == 16
    assert n["narration"][0]["text"] == "Opening beat, v17."
    assert n["previous_narration"][0]["text"] == "Opening beat, v16."

    # --- she suggests wording on scene 1
    r = anon.post(
        f"/api/storyboards/rf-surveys/feedback?t={token}",
        json.dumps({
            "kind": "suggestion",
            "narrative_slug": "verified-monitoring",
            "target_version": 17,
            "anchor_id": "the-goal",
            "suggested_text": "'Back-check' is the term of art here.",
            "author_name": "Sophie",
        }),
        content_type="application/json",
    )
    assert r.status_code == 200, r.content

    fb = Feedback.objects.get()
    assert fb.author_name == "Sophie"
    assert fb.submitted_by is None
    assert (fb.target_kind, fb.target_ref) == ("narrative", "verified-monitoring")
    assert (fb.target_version, fb.anchor_id) == (17, "the-goal")
    assert fb.kind == "suggestion"

    # --- and the wrong link gets nothing
    assert Client().get("/api/storyboards/rf-surveys?t=wrong").status_code == 404
