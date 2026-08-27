"""`opp.yaml` is read once per store, not once per run.

`_run_header_fields` runs once PER RUN and called `_opp_display_name()`, which
did a `list_folder` + `get_content` of the same `opp.yaml` every time.
Profiled against a real 12-run opp: 12 redundant reads costing 8.7s of a 19s
call — the single largest item, dwarfing the per-run state reads that the
batching work had targeted.

The batching made `list_runs` issue 2 Drive calls instead of 25 and the
endpoint got no faster, because this loop was still there. That is the lesson
worth keeping: optimise what you MEASURED, not what you assumed.
"""
import pytest

from canopy_agent_runs.drive.store import DriveRunStore, SkillMeta
from tests.fixtures.fake_drive import FakeDriveClient

AGENT = "goofy-geese"
REGISTRY = [SkillMeta("idea-to-pdd", "1-design", 1)]
STATE = "phases:\n  1-design:\n    status: done\n"


class _Counting:
    def __init__(self, inner):
        self._inner = inner
        self.content_reads = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_content(self, file_id, mime_type):
        self.content_reads += 1
        return self._inner.get_content(file_id, mime_type)


def _tree(runs, *, with_opp_yaml=True):
    node = {"runs": {r: {"run_state.yaml": STATE} for r in runs}}
    if with_opp_yaml:
        node["opp.yaml"] = "display_name: Goofy Geese\n"
    return {"goofy-geese": node}


def _store(client, root):
    return DriveRunStore(client=client, root_folder_id=root, skill_registry=REGISTRY)


def test_opp_yaml_is_read_once_regardless_of_run_count():
    runs = [f"2026010{i}-0{i}00" for i in range(1, 6)]
    inner = FakeDriveClient.from_tree(_tree(runs))
    c = _Counting(inner)
    store = _store(c, inner.folder_id("goofy-geese"))
    store.list_runs(AGENT)
    # 5 run_state bodies + exactly ONE opp.yaml, not one per run.
    assert c.content_reads == len(runs) + 1


def test_the_name_still_reaches_every_run():
    runs = ["20260101-0100", "20260102-0200"]
    inner = FakeDriveClient.from_tree(_tree(runs))
    out = _store(inner, inner.folder_id("goofy-geese")).list_runs(AGENT)
    assert {r.label for r in out} == {"Goofy Geese"}


def test_a_missing_opp_yaml_is_cached_too():
    """None is a legitimate answer. If it weren't cached, the opps that lack
    the file would keep paying full price — the exact case the memo exists
    for."""
    runs = ["20260101-0100", "20260102-0200", "20260103-0300"]
    inner = FakeDriveClient.from_tree(_tree(runs, with_opp_yaml=False))
    c = _Counting(inner)
    store = _store(c, inner.folder_id("goofy-geese"))
    out = store.list_runs(AGENT)
    assert c.content_reads == len(runs), "no opp.yaml means no body reads for it"
    # Falls back to the agent slug rather than inventing a name.
    assert {r.label for r in out} == {AGENT}
