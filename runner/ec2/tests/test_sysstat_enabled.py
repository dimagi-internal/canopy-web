"""`--setup-runners` turns sysstat collection on, so a box's memory/disk/load history exists.

Ubuntu ships sysstat with ENABLED="false" and HISTORY=7, and CloudWatch has no memory
metric for an EC2 box, so peak memory — the number a runner is sized by — was recorded
nowhere. `enable_sysstat` is idempotent: an unchanged box runs no systemctl at all.
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def files(cloud_runner, tmp_path, monkeypatch):
    default, conf = tmp_path / "default-sysstat", tmp_path / "sysstat"
    default.write_text('# comment\nENABLED="false"\n')
    conf.write_text('HISTORY=7\nSADC_OPTIONS="-S DISK"\n')
    monkeypatch.setattr(cloud_runner, "SYSSTAT_DEFAULT", default)
    monkeypatch.setattr(cloud_runner, "SYSSTAT_CONF", conf)
    return default, conf


def test_turns_collection_on_and_keeps_a_month(cloud_runner, files):
    default, conf = files
    calls = []
    assert cloud_runner.enable_sysstat(run=lambda cmd, **kw: calls.append(cmd)) is True
    assert 'ENABLED="true"' in default.read_text() and "# comment" in default.read_text()
    assert "HISTORY=31" in conf.read_text() and 'SADC_OPTIONS="-S DISK"' in conf.read_text()
    assert ["systemctl", "restart", "sysstat.service"] in calls


def test_a_second_run_changes_nothing(cloud_runner, files):
    cloud_runner.enable_sysstat(run=lambda cmd, **kw: None)
    calls = []
    assert cloud_runner.enable_sysstat(run=lambda cmd, **kw: calls.append(cmd)) is True
    assert calls == []


def test_a_box_without_sysstat_is_left_alone(cloud_runner, tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_runner, "SYSSTAT_DEFAULT", tmp_path / "missing")
    calls = []
    assert cloud_runner.enable_sysstat(run=lambda cmd, **kw: calls.append(cmd)) is False
    assert calls == []


def test_setup_runners_calls_it(cloud_runner):
    import inspect
    src = inspect.getsource(cloud_runner._runners_cli)
    assert "enable_sysstat()" in src.split('"--setup-runners"', 1)[1]


def test_new_boxes_install_the_package():
    import pathlib
    cfn = (pathlib.Path(__file__).resolve().parent.parent / "runner.cfn.yaml").read_text()
    assert "python3-yaml sysstat" in cfn
