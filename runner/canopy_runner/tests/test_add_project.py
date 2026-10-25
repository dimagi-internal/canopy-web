"""The laptop half of "add a repo to this runner" (add_project.py): clone it under
this account, open it in emdash's Add Project dialog (the CDP sidecar, mocked
here), confirm emdash's DB holds it, report back — and never mark the box
not-ready over it."""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from canopy_runner import add_project, cdp_control, desktop, readiness
from canopy_runner.client import ClientError

REPO = "https://github.com/marshellis/bailey"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    add_project._reset_for_tests()
    monkeypatch.setattr(desktop, "_current", desktop.EMDASH)
    yield
    add_project._reset_for_tests()


@pytest.fixture
def cfg(tmp_path):
    db = tmp_path / "emdash4.db"
    db.write_text("")   # only its presence is read; list_projects is faked
    return SimpleNamespace(emdash_db=str(db), cdp_port=9222, runner_id="r-1")


class FakeClient:
    def __init__(self, fail_with=None):
        self.reports = []
        self.fail_with = fail_with

    def report_project_result(self, runner_id, request_id, ok, detail=""):
        if self.fail_with is not None:
            raise self.fail_with
        self.reports.append((runner_id, request_id, ok, detail))
        return {}


class FakeEmdash:
    """emdash's projects table, and the sidecar that may add to it."""

    def __init__(self, projects=(), adds=True, error=None):
        self.projects = list(projects)
        self.adds = adds
        self.error = error
        self.calls = []

    def list_projects(self, db):
        return list(self.projects)

    def add(self, name, path, *, port):
        self.calls.append((name, path, port))
        if self.error:
            raise cdp_control.CDPError(self.error)
        if self.adds:
            self.projects.append(name)
        return {"ok": True, "action": "submitted"}


def git_ok(dest_holder=None, origin=REPO):
    """A fake `subprocess.run` for git: clone makes the folder, get-url answers `origin`."""
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        if argv[:2] == ["git", "clone"]:
            Path(argv[-1]).mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "get-url" in argv:
            return subprocess.CompletedProcess(argv, 0, origin + "\n", "")
        raise AssertionError(argv)
    run.calls = calls
    return run


def ask(**kw):
    return {"add_projects": [{"id": "a-1", "name": "bailey", "repo_url": REPO,
                              "requested_at": "2026-10-24T20:00:00Z", **kw}]}


def _observe(me, cfg, client, em, run, root, **kw):
    add_project.observe(me, cfg, client, run=run, add=em.add, list_projects=em.list_projects,
                        root=root, sleep=lambda s: None, background=False, **kw)


def test_clones_adds_in_emdash_verifies_and_reports(cfg, tmp_path):
    em, client, run = FakeEmdash(["ada"]), FakeClient(), git_ok()
    root = tmp_path / "repos"
    _observe(ask(), cfg, client, em, run, root)
    assert run.calls[0] == ["git", "clone", "--", REPO, str(root / "bailey")]
    assert em.calls == [("bailey", str(root / "bailey"), 9222)]
    assert client.reports == [("r-1", "a-1", True, f"cloned to {root / 'bailey'} and added to emdash as 'bailey'")]
    # Answered once: the same ask on the next beat is not redone.
    _observe(ask(), cfg, client, em, run, root)
    assert len(em.calls) == 1 and len(client.reports) == 1


def test_a_project_emdash_already_holds_is_reported_without_touching_anything(cfg, tmp_path):
    em, client, run = FakeEmdash(["bailey"]), FakeClient(), git_ok()
    _observe(ask(), cfg, client, em, run, tmp_path)
    assert run.calls == [] and em.calls == []
    assert client.reports[0][2] is True


def test_a_failed_clone_reports_gits_words_and_never_marks_the_box(cfg, tmp_path, monkeypatch):
    marked = []
    monkeypatch.setattr(readiness, "mark_failed", lambda *a, **k: marked.append(a))

    def run(argv, **kw):
        assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"   # never waits on a hidden prompt
        return subprocess.CompletedProcess(argv, 128, "", "fatal: repository 'x' not found")

    em, client = FakeEmdash(), FakeClient()
    _observe(ask(), cfg, client, em, run, tmp_path)
    (_, _, ok, detail), = client.reports
    assert ok is False and "not found" in detail
    assert em.calls == [] and marked == []


def test_the_dialog_accepting_it_is_not_enough_emdash_must_hold_it(cfg, tmp_path):
    em, client = FakeEmdash(adds=False), FakeClient()
    clock = iter(range(0, 1000, 5))
    add_project.observe(ask(), cfg, client, run=git_ok(), add=em.add, list_projects=em.list_projects,
                        root=tmp_path, sleep=lambda s: None, background=False, now=lambda: next(clock))
    (_, _, ok, detail), = client.reports
    assert ok is False and "no project named 'bailey'" in detail


def test_a_sidecar_failure_is_reported_with_its_reason(cfg, tmp_path):
    em, client = FakeEmdash(error="emdash would not accept /x as a project: Not a git repository"), FakeClient()
    _observe(ask(), cfg, client, em, git_ok(), tmp_path)
    (_, _, ok, detail), = client.reports
    assert ok is False and "Not a git repository" in detail


def test_a_person_using_emdash_defers_the_dialog_instead_of_failing(cfg, tmp_path):
    em, client = FakeEmdash(error="BUSY: a dialog is already open in emdash"), FakeClient()
    t = [0.0]
    kw = dict(run=git_ok(), add=em.add, list_projects=em.list_projects, root=tmp_path,
              sleep=lambda s: None, background=False, now=lambda: t[0])
    add_project.observe(ask(), cfg, client, **kw)
    assert client.reports == [] and len(em.calls) == 1
    add_project.observe(ask(), cfg, client, **kw)   # too soon: not retried
    assert len(em.calls) == 1
    em.error = None
    t[0] = add_project.BUSY_RETRY_SECONDS + 1
    add_project.observe(ask(), cfg, client, **kw)
    assert len(em.calls) == 2 and client.reports[0][2] is True


def test_the_emdash_step_waits_for_a_healthy_cdp(cfg, tmp_path):
    em, client = FakeEmdash(), FakeClient()
    _observe(ask(), cfg, client, em, git_ok(), tmp_path, healthy=False)
    assert em.calls == [] and client.reports == []
    _observe(ask(), cfg, client, em, git_ok(), tmp_path, healthy=True)
    assert len(em.calls) == 1 and client.reports[0][2] is True


def test_an_existing_checkout_of_the_same_repo_is_used(cfg, tmp_path):
    (tmp_path / "bailey").mkdir()
    em, client, run = FakeEmdash(), FakeClient(), git_ok(origin="git@github.com:marshellis/bailey.git")
    _observe(ask(), cfg, client, em, run, tmp_path)
    assert not any(c[:2] == ["git", "clone"] for c in run.calls)
    assert client.reports[0][2] is True


def test_an_existing_folder_of_another_repo_is_refused(cfg, tmp_path):
    (tmp_path / "bailey").mkdir()
    em, client = FakeEmdash(), FakeClient()
    _observe(ask(), cfg, client, em, git_ok(origin="https://github.com/someone/else"), tmp_path)
    (_, _, ok, detail), = client.reports
    assert ok is False and "someone/else" in detail and em.calls == []


@pytest.mark.parametrize("bad", [{"name": "../x"}, {"name": "-rf"}, {"repo_url": "--upload-pack=x"},
                                 {"repo_url": "file:///etc"}])
def test_unsafe_asks_are_refused_here_too(cfg, tmp_path, bad):
    em, client, run = FakeEmdash(), FakeClient(), git_ok()
    _observe(ask(**bad), cfg, client, em, run, tmp_path)
    assert run.calls == [] and em.calls == [] and client.reports[0][2] is False


def test_a_claude_cli_runner_says_it_does_not_use_emdash(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "_current", desktop.CLAUDE_CLI)
    em, client = FakeEmdash(), FakeClient()
    _observe(ask(), cfg, client, em, git_ok(), tmp_path)
    assert client.reports[0][2] is False and "claude-cli" in client.reports[0][3]


def test_a_report_that_does_not_land_is_sent_again_without_redoing_the_work(cfg, tmp_path):
    em, run = FakeEmdash(), git_ok()
    down = FakeClient(fail_with=ClientError("POST -> 503", transient=True, status=503))
    _observe(ask(), cfg, down, em, run, tmp_path)
    up = FakeClient()
    _observe(ask(), cfg, up, em, run, tmp_path)
    assert len(em.calls) == 1 and up.reports[0][2] is True


def test_a_re_armed_ask_is_a_new_attempt(cfg, tmp_path):
    em, client = FakeEmdash(error="emdash would not accept it"), FakeClient()
    _observe(ask(), cfg, client, em, git_ok(), tmp_path)
    em.error = None
    _observe(ask(requested_at="2026-10-24T21:00:00Z"), cfg, client, em, git_ok(), tmp_path)
    assert [r[2] for r in client.reports] == [False, True]


def test_nothing_asked_is_a_no_op(cfg):
    add_project.observe({"add_projects": []}, cfg, FakeClient())
    add_project.observe({}, cfg, FakeClient())
    add_project.observe(None, cfg, FakeClient())


def test_repos_root_prefers_the_folder_this_mac_already_uses(tmp_path):
    assert add_project.repos_root(tmp_path) == tmp_path / "emdash" / "repositories"
    (tmp_path / "emdash-projects").mkdir()
    assert add_project.repos_root(tmp_path) == tmp_path / "emdash-projects"
    (tmp_path / "emdash" / "repositories").mkdir(parents=True)
    assert add_project.repos_root(tmp_path) == tmp_path / "emdash" / "repositories"


@pytest.mark.parametrize("a,b", [
    ("https://github.com/marshellis/bailey", "git@github.com:marshellis/bailey.git"),
    ("https://github.com/Marshellis/Bailey.git", "ssh://git@github.com/marshellis/bailey"),
])
def test_remote_forms_of_one_repo_match(a, b):
    assert add_project._norm_remote(a) == add_project._norm_remote(b)


def test_the_sidecar_wrapper_sends_the_add_project_command(monkeypatch):
    seen = {}

    def fake_run(command, args, **kw):
        seen.update(command=command, args=args)
        return {"ok": True, "action": "submitted"}
    monkeypatch.setattr(cdp_control, "_run", fake_run)
    assert cdp_control.add_project("bailey", "/r/bailey", port=9333)["action"] == "submitted"
    assert seen == {"command": "add-project", "args": {"port": 9333, "name": "bailey", "path": "/r/bailey"}}
