"""Importing a repo-authored storyboard.yaml. Idempotent per (workspace, slug)."""
from __future__ import annotations

import pytest
import yaml
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.storyboards.models import Act, Entry, Storyboard
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


@pytest.fixture()
def ws():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    return Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)


def _write(tmp_path, raw):
    p = tmp_path / "storyboard.yaml"
    p.write_text(yaml.dump(raw))
    return str(p)


BOARD = {
    "slug": "ecf-supply",
    "title": "What the money bought",
    "lede": "From the first purchase order to the child who recovered.",
    "capability": "comment",
    "acts": [
        {
            "title": "Six weeks to a supply base",
            "prose": "Procurement integrity you can show, not assert.",
            "entries": ["procurement-eoi", "supplier-registry"],
        },
        {"title": "Where the RUTF is", "entries": ["command-centre"]},
    ],
}


def test_import_creates_the_arc(tmp_path, ws):
    call_command("import_storyboard", _write(tmp_path, BOARD), workspace="dimagi")

    board = Storyboard.objects.get(slug="ecf-supply")
    assert board.title == "What the money bought"
    assert board.capability == "comment"
    assert [a.title for a in board.acts.all()] == [
        "Six weeks to a supply base",
        "Where the RUTF is",
    ]
    assert [e.narrative_slug for e in board.acts.first().entries.all()] == [
        "procurement-eoi",
        "supplier-registry",
    ]


def test_re_importing_updates_rather_than_duplicating(tmp_path, ws):
    path = _write(tmp_path, BOARD)
    call_command("import_storyboard", path, workspace="dimagi")

    revised = {**BOARD, "title": "Renamed", "acts": [{"title": "Only act", "entries": ["a"]}]}
    call_command("import_storyboard", _write(tmp_path, revised), workspace="dimagi")

    assert Storyboard.objects.count() == 1
    board = Storyboard.objects.get()
    assert board.title == "Renamed"
    assert [a.title for a in board.acts.all()] == ["Only act"]
    assert Entry.objects.filter(act__storyboard=board).count() == 1


def test_re_import_preserves_the_share_token(tmp_path, ws):
    """A re-import must not invalidate links already sent — the arc changed,
    not who may see it."""
    path = _write(tmp_path, BOARD)
    call_command("import_storyboard", path, workspace="dimagi")
    token = Storyboard.objects.get().ensure_share_token()

    call_command("import_storyboard", path, workspace="dimagi")
    assert Storyboard.objects.get().share_token == token


def test_a_mapping_entry_can_pin_a_run(tmp_path, ws):
    raw = {
        **BOARD,
        "acts": [
            {
                "title": "Act",
                "entries": [{"narrative_slug": "held", "pinned_run_id": "held-2026-07-26-001"}],
            }
        ],
    }
    call_command("import_storyboard", _write(tmp_path, raw), workspace="dimagi")
    entry = Entry.objects.get()
    assert entry.narrative_slug == "held"
    assert entry.pinned_run_id == "held-2026-07-26-001"


def test_a_missing_slug_is_a_loud_error(tmp_path, ws):
    with pytest.raises(CommandError, match="slug"):
        call_command("import_storyboard", _write(tmp_path, {"title": "No slug"}), workspace="dimagi")


def test_an_entry_without_a_narrative_slug_is_a_loud_error(tmp_path, ws):
    raw = {**BOARD, "acts": [{"title": "Act", "entries": [{"pinned_run_id": "x"}]}]}
    with pytest.raises(CommandError, match="narrative_slug"):
        call_command("import_storyboard", _write(tmp_path, raw), workspace="dimagi")


def test_a_missing_file_is_a_loud_error(ws):
    with pytest.raises(CommandError, match="no such file"):
        call_command("import_storyboard", "/nope/storyboard.yaml", workspace="dimagi")


def test_re_importing_keeps_the_same_act_anchors(tmp_path, ws):
    """The import deletes and recreates every act, so an anchor tied to the row
    id would orphan every act note on each push. Keys must reproduce."""
    raw = {**BOARD, "acts": [{"title": "Six weeks", "entries": ["a"]}, {"title": "Where it is", "entries": ["b"]}]}
    path = _write(tmp_path, raw)

    call_command("import_storyboard", path, workspace="dimagi")
    before = list(Act.objects.order_by("position").values_list("key", flat=True))
    pks = set(Act.objects.values_list("pk", flat=True))

    call_command("import_storyboard", path, workspace="dimagi")
    assert list(Act.objects.order_by("position").values_list("key", flat=True)) == before
    assert not (pks & set(Act.objects.values_list("pk", flat=True))), "rows were replaced"


def test_a_declared_key_survives_a_retitle_in_the_file(tmp_path, ws):
    first = {**BOARD, "acts": [{"key": "supply-base", "title": "Six weeks", "entries": ["a"]}]}
    call_command("import_storyboard", _write(tmp_path, first), workspace="dimagi")
    second = {**BOARD, "acts": [{"key": "supply-base", "title": "What six weeks bought", "entries": ["a"]}]}
    call_command("import_storyboard", _write(tmp_path, second), workspace="dimagi")

    act = Act.objects.get()
    assert (act.key, act.title) == ("supply-base", "What six weeks bought")


def test_layout_and_entry_overrides_come_through_the_file(tmp_path, ws):
    """A reel is authored, not configured in a UI — the file has to carry both
    the layout and the flat per-video statement it exists to show."""
    raw = {
        **BOARD,
        "layout": "reel",
        "acts": [
            {
                "title": "Watch in order",
                "entries": [
                    {
                        "narrative_slug": "verified-monitoring",
                        "title": "Survey quality review",
                        "blurb": "Six rounds of survey data; one surveyor flagged.",
                    }
                ],
            }
        ],
    }
    call_command("import_storyboard", _write(tmp_path, raw), workspace="dimagi")

    board = Storyboard.objects.get(slug="ecf-supply")
    assert board.layout == Storyboard.LAYOUT_REEL
    entry = Entry.objects.get(act__storyboard=board)
    assert entry.title == "Survey quality review"
    assert entry.blurb == "Six rounds of survey data; one surveyor flagged."


def test_a_board_that_says_nothing_about_layout_stays_a_review_board(tmp_path, ws):
    call_command("import_storyboard", _write(tmp_path, BOARD), workspace="dimagi")
    assert Storyboard.objects.get(slug="ecf-supply").layout == Storyboard.LAYOUT_REVIEW
    assert Entry.objects.filter(title="", blurb="").count() == 3
