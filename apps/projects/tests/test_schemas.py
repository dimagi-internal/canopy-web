import pytest

from apps.projects.schemas import (
    BatchContextIn,
    ProjectCreateIn,
    ProjectListOut,
    ProjectPatchIn,
    ProjectSlugOut,
)


def test_project_list_round_trip():
    raw = {
        "id": 1,
        "name": "canopy-web",
        "slug": "canopy-web",
        "repo_url": "https://github.com/dimagi/canopy",
        "deploy_url": "",
        "visibility": "public",
        "status": "active",
        "skills": [{"name": "discovery-call", "description": "X"}],
        "latest_context": {
            "current_work": {
                "content": "API modernization",
                "source": "session-review",
                "created_at": "2026-05-26T09:00:00Z",
            }
        },
        "latest_actions": {
            "session-review": {
                "status": "completed",
                "started_at": "2026-05-25T09:00:00Z",
                "completed_at": "2026-05-25T09:10:00Z",
            }
        },
        "walkthrough_count": 2,
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-05-26T09:00:00Z",
    }
    parsed = ProjectListOut.model_validate(raw)
    assert parsed.slug == "canopy-web"
    assert parsed.walkthrough_count == 2
    assert "current_work" in parsed.latest_context


def test_project_list_walkthrough_count_default_zero():
    """walkthrough_count defaults to 0 if not in payload."""
    raw = {
        "id": 1, "name": "x", "slug": "x", "repo_url": "", "deploy_url": "",
        "visibility": "public", "status": "active", "skills": [],
        "latest_context": {}, "latest_actions": {},
        "created_at": "2026-04-01T00:00:00Z",
        "updated_at": "2026-05-26T09:00:00Z",
    }
    parsed = ProjectListOut.model_validate(raw)
    assert parsed.walkthrough_count == 0


def test_project_create_slug_validation():
    obj = ProjectCreateIn(name="X", slug="canopy-web")
    assert obj.slug == "canopy-web"
    with pytest.raises(ValueError):
        ProjectCreateIn(name="X", slug="UPPERCASE")
    with pytest.raises(ValueError):
        ProjectCreateIn(name="X", slug="has spaces")


def test_project_patch_partial():
    obj = ProjectPatchIn(status="archived")
    dumped = obj.model_dump(exclude_unset=True)
    assert dumped == {"status": "archived"}


def test_batch_context_in_shape():
    obj = BatchContextIn.model_validate({
        "updates": {
            "canopy-web": [
                {"context_type": "current_work", "content": "x", "source": "y"}
            ]
        }
    })
    assert list(obj.updates.keys()) == ["canopy-web"]


def test_project_slug_out():
    parsed = ProjectSlugOut.model_validate({
        "slug": "canopy-web",
        "name": "canopy-web",
        "status": "active",
        "visibility": "public",
        "workspace": None,
    })
    assert parsed.slug == "canopy-web"
