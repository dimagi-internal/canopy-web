"""Which verdict a step shows when its skill wrote several.

The un-suffixed file is the skill's FULL evaluation; `-shallow` / `-quick`
are one-dimension smoke passes beside it. The full file used to rank 0 —
below both — so ACE's app-screenshot-capture step showed its shallow smoke
("25") over its 4-dimension 9.2 on spark-facilitator/20260925-1536
(dimagi-internal/ace-web#838 fixed ace-web's own copy of this ranking).
"""
from canopy_agent_runs.drive.store import _load_verdicts
from tests.fixtures.fake_drive import FakeDriveClient

FULL = "ran_at: 2026-09-26T03:40:19Z\noverall_score: 9.2\nverdict: pass\n"
SHALLOW = "mode: shallow\nran_at: 2026-09-26T03:40:19Z\noverall_score: 2.5\nverdict: pass\n"
DEEP = "mode: deep\nran_at: 2026-09-26T03:40:19Z\noverall_score: 7.0\nverdict: pass\n"


def _load(files: dict):
    drive = FakeDriveClient.from_tree({"RUN": {"6-qa-and-training": files}})
    run = drive.folder_id("RUN")
    return _load_verdicts(drive, drive.list_files(run, recursive=True),
                          registered_skills={"app-screenshot-capture"})["app-screenshot-capture"]


def test_the_full_verdict_outranks_its_shallow_smoke():
    v = _load({"app-screenshot-capture_verdict.yaml": FULL,
               "app-screenshot-capture_verdict-shallow.yaml": SHALLOW})
    assert v.score == 9.2


def test_a_deep_verdict_still_outranks_the_full_one():
    v = _load({"app-screenshot-capture_verdict.yaml": FULL,
               "app-screenshot-capture_verdict-deep.yaml": DEEP})
    assert v.score == 7.0


def test_a_lone_shallow_verdict_is_still_shown():
    assert _load({"app-screenshot-capture_verdict-shallow.yaml": SHALLOW}).score == 2.5
