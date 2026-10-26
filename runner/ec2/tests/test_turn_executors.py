"""One executor interface: how a turn's agent CLI is driven is a `TurnExecutor`.

RUNNER_EXECUTOR is read in one place (`_executor()`), and every behaviour that
depends on it — running a turn, a stop, bootstrap installs, the package health
check, sensitive-data mode — asks the instance. A new way of driving a CLI (Codex's
app-server) is a new subclass, not another `if RUNNER_EXECUTOR == ...` branch.
"""
from __future__ import annotations

import inspect
import pathlib
import re

import pytest

TID = "0c1d2e3f-0000-4000-8000-000000000001"
SOURCE = pathlib.Path(__file__).resolve().parent.parent / "cloud_runner.py"
TEMPLATE = pathlib.Path(__file__).resolve().parent.parent / "runner.cfn.yaml"


@pytest.mark.parametrize("value,name", [
    (None, "cli"), ("cli", "cli"), ("acp", "acp"), (" ACP ", "acp"), ("bogus", "cli"),
])
def test_runner_executor_resolves_to_one_instance(load_cloud_runner, monkeypatch, value, name):
    monkeypatch.delenv("RUNNER_DATA_MODE", raising=False)
    if value is None:
        monkeypatch.delenv("RUNNER_EXECUTOR", raising=False)
    else:
        monkeypatch.setenv("RUNNER_EXECUTOR", value)
    cr = load_cloud_runner()
    ex = cr._executor()
    assert isinstance(ex, cr.TurnExecutor)
    assert ex.name == name
    assert ex is cr._EXECUTORS[name]


def test_every_executor_drives_a_cli_canopy_knows(cloud_runner):
    clis_py = (SOURCE.parents[2] / "apps" / "harness" / "clis.py").read_text()
    for ex in cloud_runner._EXECUTORS.values():
        assert f'"{ex.cli}"' in clis_py, f"{ex.name} drives {ex.cli!r}, not a CLI in clis.py"


def test_the_template_offers_exactly_the_registered_executors(cloud_runner):
    allowed = re.search(r"^  RunnerExecutor:\n.*?AllowedValues: \[(.*?)\]", TEMPLATE.read_text(),
                        re.S | re.M).group(1)
    offered = {v.strip().strip("'\"") for v in allowed.split(",")}
    assert offered == set(cloud_runner._EXECUTORS)


def test_no_behaviour_branches_on_the_executor_name():
    """`_executor()` is the only reader that turns RUNNER_EXECUTOR into behaviour."""
    code = [ln for ln in SOURCE.read_text().splitlines() if not ln.lstrip().startswith("#")]
    branches = [ln.strip() for ln in code
                if re.search(r"RUNNER_EXECUTOR\s*(==|!=)|(==|!=)\s*RUNNER_EXECUTOR", ln)]
    assert branches == [], f"branch on the executor's interface instead: {branches}"


@pytest.mark.parametrize("executor,ran", [("cli", "run_claude"), ("acp", "run_acp")])
def test_a_turn_runs_on_the_selected_executor(cloud_runner, monkeypatch, executor, ran):
    calls = []
    for fn in ("run_claude", "run_acp"):
        monkeypatch.setattr(cloud_runner, fn,
                            lambda *a, _fn=fn, **k: (calls.append((_fn, a, k)), (True, "ok", "s"))[1])
    monkeypatch.setattr(cloud_runner, "RUNNER_EXECUTOR", executor)
    out = cloud_runner._execute_once("p", TID, print, cwd="/w", agent_slug="ada",
                                     resume_session_id="r")
    assert out == (True, "ok", "s")
    assert calls == [(ran, ("p", TID, print),
                      {"cwd": "/w", "agent_slug": "ada", "resume_session_id": "r"})]


def test_only_an_executor_that_carries_settings_runs_sensitive_data(load_cloud_runner, monkeypatch):
    monkeypatch.setenv("RUNNER_NAME", "cloud-sensitive-2")
    monkeypatch.setenv("RUNNER_DATA_MODE", "sensitive_data")
    monkeypatch.setenv("RUNNER_EXECUTOR", "acp")
    cr = load_cloud_runner()
    assert cr._executor().carries_settings
    assert not cr._EXECUTORS["acp"].carries_settings


def test_a_stop_reaches_whichever_executor_holds_the_turn(cloud_runner, monkeypatch):
    """ACP without canopy_acp runs its turn as `claude -p`: the stop must find it."""
    cr = cloud_runner

    class Proc:
        def __init__(self):
            self.signals = []

        def poll(self):
            return None

        def send_signal(self, sig):
            self.signals.append("INT" if sig == cr.signal.SIGINT else str(sig))

        def terminate(self):
            self.signals.append("TERM")

        def kill(self):
            self.signals.append("KILL")

    monkeypatch.setattr(cr, "RUNNER_EXECUTOR", "acp")
    with cr._TURNS_LOCK:
        cr._IN_FLIGHT[TID] = None
    try:
        proc = Proc()
        cr._register_cli_proc(TID, proc)
        assert cr.request_stop(TID) is True
        assert proc.signals == ["INT"]
    finally:
        with cr._TURNS_LOCK:
            cr._IN_FLIGHT.pop(TID, None)
        cr._clear_stop(TID)


def test_bootstrap_prepares_only_the_selected_executor(cloud_runner, monkeypatch, tmp_path):
    prepared = []
    for name, ex in cloud_runner._EXECUTORS.items():
        monkeypatch.setattr(ex, "prepare", lambda _n=name: prepared.append(_n))
    monkeypatch.setattr(cloud_runner, "_expose_repo_packages", lambda repo: None)
    monkeypatch.setattr(cloud_runner, "RUNNER_EXECUTOR", "acp")
    cloud_runner._install_transcript_core(tmp_path)
    assert prepared == ["acp"]


def test_the_interface_is_what_a_new_executor_must_implement(cloud_runner):
    abstract = cloud_runner.TurnExecutor.__abstractmethods__
    assert abstract == {"run_turn", "live", "interrupt"}
    params = list(inspect.signature(cloud_runner.TurnExecutor.run_turn).parameters)
    assert params == ["self", "prompt", "turn_id", "emit", "cwd", "agent_slug",
                      "resume_session_id"]
