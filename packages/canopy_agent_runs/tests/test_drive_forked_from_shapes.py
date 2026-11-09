"""``forked_from`` on Drive is not always a bare run-id string.

ace-web's opp_forker writes it as a dict block
(``{run_id, phase, forked_at}``) in some run_state.yaml files. The framework
read model reads it (``RunSummary.forked_from: str | None``), so a dict would
raise pydantic ValidationError inside ``list_runs`` — which 500s every
uncached read of the whole opp, not just the forked run.

The rows live in Google Docs holding live run state, so rewriting them in
place is riskier than reading them tolerantly. Every sibling header field is
coerced (``str(...)``, ``_coerce_dt(...)``), and ``forked_from`` is coerced the
same way.
"""

import yaml

from canopy_agent_runs.drive.store import DriveRunStore, SkillMeta
from canopy_agent_runs.schemas import RunSummary
from tests.fixtures.fake_drive import FakeDriveClient

AGENT = "goofy-geese"
RUN_ID = "20260101-1000"
SOURCE_RUN = "20251231-0900"

REGISTRY = [SkillMeta("idea-to-pdd", "1-design", 1)]

_BASE_STATE = {
    "mode": "review",
    "started_at": "2026-01-01T10:00:00Z",
    "current_step": "idea-to-pdd",
    "phases": {"1-design": {"status": "complete",
                            "steps": {"idea-to-pdd": {"status": "done"}}}},
}


def _store(forked_from) -> DriveRunStore:
    state = dict(_BASE_STATE)
    if forked_from is not None:
        state["forked_from"] = forked_from
    tree = {"agents": {AGENT: {
        "opp.yaml": "display_name: Goofy Geese Demo\nslug: goofy-geese\n",
        "runs": {RUN_ID: {"run_state.yaml": yaml.safe_dump(state)}},
    }}}
    client = FakeDriveClient.from_tree(tree)
    return DriveRunStore(client, client.folder_id(f"agents/{AGENT}"),
                         agent_slug=AGENT, manifest=[], skill_registry=REGISTRY)


def _only(summaries) -> RunSummary:
    assert len(summaries) == 1
    return summaries[0]


def test_string_forked_from_is_preserved():
    assert _only(_store(SOURCE_RUN).list_runs(AGENT)).forked_from == SOURCE_RUN


def test_dict_forked_from_is_reduced_to_the_source_run_id():
    """The shape that was 500ing prod (ace-web opp_forker's lineage block)."""
    block = {"run_id": SOURCE_RUN, "phase": "commcare-setup",
             "forked_at": "2026-07-27T18:50:43Z"}
    assert _only(_store(block).list_runs(AGENT)).forked_from == SOURCE_RUN


def test_absent_forked_from_stays_none():
    assert _only(_store(None).list_runs(AGENT)).forked_from is None


def test_unusable_forked_from_degrades_to_none_rather_than_raising():
    """A shape we can't read must not take down the whole opp's listing."""
    assert _only(_store({"phase": "commcare-setup"}).list_runs(AGENT)).forked_from is None
    assert _only(_store([SOURCE_RUN]).list_runs(AGENT)).forked_from is None


def test_get_run_agrees_with_list_runs():
    block = {"run_id": SOURCE_RUN, "phase": "commcare-setup"}
    store = _store(block)
    assert store.get_run(AGENT, RUN_ID).forked_from == SOURCE_RUN
