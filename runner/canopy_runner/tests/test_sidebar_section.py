"""The sidecar finds a task under ITS project's sidebar section, not by name alone.

Turn 22662f53 (2026-10-04): ada and eva each had an emdash task called "editing".
The sidebar then held two buttons labelled "Open task editing", the sidecar clicked
the first (ada's), and eva's chat message was typed into ada's session four times.

`sidebar_section.mjs` is the pure rule the sidecar now applies (a task row belongs
to the nearest "New task for <project>" header above it). It runs under node, so
these tests drive it through node; the label lists are the real sidebar order read
from a live emdash on 2026-10-04.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "canopy_runner" / "cdp" / "sidebar_section.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

# Measured on the live emdash that misdelivered turn 22662f53.
LIVE_SIDEBAR = [
    "New task for muse", "New task for ace-web", "New task for connect-labs",
    "Open task supply-ddd", "New task for echo", "New task for hal", "Open task audit-ddd",
    "New task for ada", "Open task editing", "Open task audit",
    "New task for eva", "Open task editing", "Open task idm-talk",
    "New task for ace", "Open task spark", "Open task c-kmc-workflows-bedc",
    "Open task c-interviews-9a2b", "New task for canopy-web",
    "Open task c-make-settings-topology-the-one-69d4", "New task for canopy",
]


def pick(labels, project, task, carried=False):
    script = (
        f"const m = await import({json.dumps(MODULE.as_uri())});"
        f"process.stdout.write(JSON.stringify(m.pickInSection("
        f"{json.dumps(labels)}, {json.dumps(project)}, {json.dumps(task)}, {json.dumps(carried)})));"
    )
    out = subprocess.run(["node", "--input-type=module", "-e", script],
                         capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_each_project_gets_its_own_same_named_row():
    assert pick(LIVE_SIDEBAR, "ada", "editing") == {"state": "found", "index": 8}
    assert pick(LIVE_SIDEBAR, "eva", "editing") == {"state": "found", "index": 11}
    # The old lookup: the first row with the label, whichever project asked.
    assert LIVE_SIDEBAR.index("Open task editing") == 8


def test_a_project_without_the_task_never_borrows_anothers():
    assert pick(LIVE_SIDEBAR, "hal", "editing") == {"state": "ended"}
    assert pick(LIVE_SIDEBAR, "eva", "spark") == {"state": "ended"}


def test_an_unrendered_header_is_not_reached():
    assert pick(LIVE_SIDEBAR, "nosuch", "editing") == {"state": "before"}


def test_a_section_running_past_the_rendered_rows_asks_to_scroll():
    assert pick(LIVE_SIDEBAR[:12], "eva", "idm-talk") == {"state": "more"}


def test_after_scrolling_past_the_header_the_carried_section_still_owns_its_rows():
    # The header has scrolled out of the DOM; rows above the next header are eva's.
    scrolled = ["Open task idm-talk", "Open task late-task", "New task for ace", "Open task spark"]
    assert pick(scrolled, "eva", "late-task", carried=True) == {"state": "found", "index": 1}
    assert pick(scrolled, "eva", "spark", carried=True) == {"state": "ended"}


def test_rows_above_a_visible_header_are_never_the_projects_even_when_carried():
    assert pick(LIVE_SIDEBAR, "eva", "audit", carried=True) == {"state": "ended"}
