"""Moving a narrative's artifacts between workspaces. Dry run by default."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.reviews.models import ReviewRequest
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def workspaces():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    Workspace.objects.create(slug="connect", display_name="Connect", created_by=owner)
    return owner


def _review(slug: str, version: int, workspace: str) -> ReviewRequest:
    return ReviewRequest.objects.create(
        run_id=f"{slug}-2026-07-26-{version:03d}",
        request_json={
            "gate": "concept_change",
            "narrative_slug": slug,
            "narration": [{"id": "the-goal", "title": "T", "text": f"v{version}"}],
        },
        gate="concept_change",
        visibility="link",
        workspace_id=workspace,
        version=version,
    )


def _split_lineage():
    """The real labs shape: one narrative's versions straddling two tenants."""
    _review("create-survey-solicitation", 7, "dimagi")
    _review("create-survey-solicitation", 11, "connect")
    _review("create-survey-solicitation", 12, "dimagi")
    _review("untouched-narrative", 1, "dimagi")


def test_dry_run_moves_nothing(workspaces, capsys):
    _split_lineage()
    call_command("move_narrative_workspace", "--to", "connect", "--slug", "create-survey-solicitation")

    assert "DRY RUN" in capsys.readouterr().out
    assert ReviewRequest.objects.filter(workspace_id="dimagi").count() == 3


def test_apply_consolidates_the_lineage(workspaces):
    _split_lineage()
    call_command(
        "move_narrative_workspace", "--to", "connect",
        "--slug", "create-survey-solicitation", "--apply",
    )

    moved = ReviewRequest.objects.filter(run_id__startswith="create-survey-solicitation")
    assert {r.workspace_id for r in moved} == {"connect"}
    assert [r.version for r in moved.order_by("version")] == [7, 11, 12]


def test_an_unnamed_narrative_is_untouched(workspaces):
    _split_lineage()
    call_command(
        "move_narrative_workspace", "--to", "connect",
        "--slug", "create-survey-solicitation", "--apply",
    )
    other = ReviewRequest.objects.get(run_id__startswith="untouched-narrative")
    assert other.workspace_id == "dimagi"


def test_rows_already_in_the_target_are_left_alone(workspaces, capsys):
    _review("verified-monitoring", 17, "connect")
    call_command("move_narrative_workspace", "--to", "connect", "--slug", "verified-monitoring")
    assert "Would move 0 review(s)" in capsys.readouterr().out


def test_it_is_idempotent(workspaces):
    _split_lineage()
    for _ in range(2):
        call_command(
            "move_narrative_workspace", "--to", "connect",
            "--slug", "create-survey-solicitation", "--apply",
        )
    moved = ReviewRequest.objects.filter(run_id__startswith="create-survey-solicitation")
    assert {r.workspace_id for r in moved} == {"connect"}
    assert moved.count() == 3


def test_an_unknown_target_workspace_is_a_loud_error(workspaces):
    with pytest.raises(CommandError, match="no such workspace"):
        call_command("move_narrative_workspace", "--to", "nope", "--slug", "x")


def test_several_narratives_move_together(workspaces):
    _review("a-narrative", 1, "dimagi")
    _review("b-narrative", 1, "dimagi")
    call_command(
        "move_narrative_workspace", "--to", "connect",
        "--slug", "a-narrative", "--slug", "b-narrative", "--apply",
    )
    assert ReviewRequest.objects.filter(workspace_id="connect").count() == 2
