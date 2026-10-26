"""A laptop chat turn held on a site's page leaves a marker its agent's MCP helper finds
(`page_site.py`): the runner cannot set an emdash session's environment."""
from __future__ import annotations

import json

from canopy_runner import page_site

PAGE = {"page_site": {"site": "connect-labs", "withheld": ["LABS_MCP_TOKEN", "bad name"]}}


def test_a_page_turn_leaves_a_private_marker_by_task(tmp_path):
    path = page_site.write(PAGE, task="ace-chat-1", root=tmp_path)
    assert path == tmp_path / "task" / "ace-chat-1.json"
    assert json.loads(path.read_text()) == {"site": "connect-labs", "withheld": ["LABS_MCP_TOKEN"]}
    assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "task").stat().st_mode & 0o777 == 0o700


def test_a_turn_off_any_page_clears_the_tasks_marker(tmp_path):
    page_site.write(PAGE, task="ace-chat-1", root=tmp_path)
    assert page_site.write({}, task="ace-chat-1", root=tmp_path) is None
    assert not (tmp_path / "task" / "ace-chat-1.json").exists()


def test_nothing_odd_is_joined_onto_a_path(tmp_path):
    assert page_site.write(PAGE, task="../escape", root=tmp_path) is None
    assert page_site.of({"page_site": {"site": "a/b"}}) is None
