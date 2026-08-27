"""`list_runs` fast paths: one query for the state files, concurrent reads.

The shape this replaces cost 1 + 2N Drive round-trips SEQUENTIALLY — for each
run, list its folder, then download run_state.yaml. Measured on a real opp
(12 runs): ~25 calls, 30-50s of wall clock, behind a 30s content cache that a
50s load can never populate, so every page view paid full price.

Both fast paths are negotiated off the client (`find_in_folders`,
`get_contents`), so a client offering neither still works. These tests pin
three things: the fast path is USED when offered, the fallback still works
when it isn't, and both produce the SAME read model.
"""
import pytest

from canopy_agent_runs.drive.store import DriveRunStore, SkillMeta
from tests.fixtures.fake_drive import FakeDriveClient

AGENT = "goofy-geese"
REGISTRY = [SkillMeta("idea-to-pdd", "1-design", 1)]

STATE = """
opportunity: goofy-geese
mode: default
phases:
  1-design:
    status: done
    steps:
      idea-to-pdd:
        status: done
"""


def _tree(run_ids: list[str]) -> dict:
    return {
        "goofy-geese": {
            "runs": {rid: {"run_state.yaml": STATE} for rid in run_ids},
        }
    }


class _CountingClient:
    """Wraps FakeDriveClient and counts calls, so a test can assert the fast
    path actually eliminated the per-run work rather than merely producing the
    same answer more slowly."""

    def __init__(self, inner, *, offer_find=False, offer_bulk=False):
        self._inner = inner
        self.list_folder_calls = 0
        self.get_content_calls = 0
        self.find_calls = 0
        self.bulk_calls = 0
        if offer_find:
            self.find_in_folders = self._find_in_folders
        if offer_bulk:
            self.get_contents = self._get_contents

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def list_folder(self, folder_id):
        self.list_folder_calls += 1
        return self._inner.list_folder(folder_id)

    def get_content(self, file_id, mime_type):
        self.get_content_calls += 1
        return self._inner.get_content(file_id, mime_type)

    def _find_in_folders(self, parent_ids, name):
        self.find_calls += 1
        out = {}
        for pid in parent_ids:
            match = next(
                (f for f in self._inner.list_folder(pid) if f.name == name), None
            )
            out[pid] = match
        return out

    def _get_contents(self, specs):
        self.bulk_calls += 1
        return {
            fid: self._inner.get_content(fid, mime).content for fid, mime in specs
        }


def _store(client, root):
    return DriveRunStore(client=client, root_folder_id=root, skill_registry=REGISTRY)


RUN_IDS = ["20260101-0100", "20260102-0200", "20260103-0300"]


@pytest.fixture
def seeded():
    inner = FakeDriveClient.from_tree(_tree(RUN_IDS))
    return inner, inner.folder_id("goofy-geese")


def test_fallback_client_still_works(seeded):
    inner, root = seeded
    c = _CountingClient(inner)
    runs = _store(c, root).list_runs(AGENT)
    assert sorted(r.id for r in runs) == RUN_IDS
    # 1 root + 1 runs/ + one per run
    assert c.list_folder_calls >= 3
    assert c.get_content_calls == 3


def test_fast_paths_are_used_when_offered(seeded):
    inner, root = seeded
    c = _CountingClient(inner, offer_find=True, offer_bulk=True)
    runs = _store(c, root).list_runs(AGENT)
    assert sorted(r.id for r in runs) == RUN_IDS
    assert c.find_calls == 1, "the per-run folder listing should collapse to one query"
    assert c.bulk_calls == 1, "the per-run downloads should collapse to one bulk call"
    assert c.get_content_calls == 0, "no sequential per-run download should remain"


def test_both_paths_produce_the_same_read_model(seeded):
    inner, root = seeded
    slow = _store(_CountingClient(inner), root).list_runs(AGENT)
    fast = _store(
        _CountingClient(inner, offer_find=True, offer_bulk=True), root
    ).list_runs(AGENT)
    assert [r.model_dump() for r in slow] == [r.model_dump() for r in fast]


def test_a_half_initialised_run_folder_is_skipped_on_both_paths():
    tree = _tree(RUN_IDS)
    tree["goofy-geese"]["runs"]["20260104-0400"] = {}  # no run_state.yaml
    inner = FakeDriveClient.from_tree(tree)
    root = inner.folder_id("goofy-geese")
    for c in (
        _CountingClient(inner),
        _CountingClient(inner, offer_find=True, offer_bulk=True),
    ):
        ids = [r.id for r in _store(c, root).list_runs(AGENT)]
        assert "20260104-0400" not in ids
        assert len(ids) == 3


def test_an_opp_with_no_runs_folder_returns_empty():
    inner = FakeDriveClient.from_tree({"empty-opp": {}})
    root = inner.folder_id("empty-opp")
    c = _CountingClient(inner, offer_find=True, offer_bulk=True)
    assert _store(c, root).list_runs(AGENT) == []
    assert c.find_calls == 0, "no runs means no query at all"
