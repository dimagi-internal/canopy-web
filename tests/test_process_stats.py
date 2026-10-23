"""The PROCESS_STATS line (apps/common/process_stats.py): what a web process is
doing, from inside it, once a minute."""
from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest

from apps.common import process_stats, ticks

linux_only = pytest.mark.skipif(not os.path.isdir("/proc/self/task"), reason="reads /proc")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(process_stats, "_last", {})


def test_it_is_registered_per_process_not_fleet_wide():
    job = ticks.jobs()["common.process_stats"]
    assert job.dedupe is False
    assert job.interval() == 60


def test_threads_are_counted_by_name_with_pool_numbers_folded():
    stop = threading.Event()
    workers = [threading.Thread(target=stop.wait, name=f"ThreadPoolExecutor-3_{i}") for i in range(2)]
    named = threading.Thread(target=stop.wait, name="kb-index")
    for t in [*workers, named]:
        t.start()
    try:
        names = process_stats._thread_names()
    finally:
        stop.set()
    assert names["kb-index"] == 1
    assert names["ThreadPoolExecutor"] >= 2


def test_the_first_sample_has_no_rates_and_the_second_does():
    first = process_stats.snapshot()
    assert "cpu_pct" not in first and first["pid"] == os.getpid()
    second = process_stats.snapshot()
    assert "cpu_pct" in second and second["interval_s"] >= 0


def _spin(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(range(1000))


@linux_only
def test_a_thread_burning_cpu_is_named_with_the_stack_it_is_in():
    stop = threading.Event()
    hot = threading.Thread(target=_spin, args=(stop,), name="kb-index", daemon=True)
    process_stats.snapshot()
    hot.start()
    try:
        time.sleep(1.0)
        out = process_stats.snapshot()
    finally:
        stop.set()
        hot.join()
    rows = [r for r in out.get("hot_threads", []) if r["name"] == "kb-index"]
    assert rows, out
    assert any("_spin" in frame for frame in rows[0]["stack"])


@linux_only
def test_memory_reports_rss():
    assert process_stats._memory()["rss_mb"] > 0


def test_run_logs_one_parseable_line(caplog):
    with caplog.at_level(logging.INFO, logger="canopy.process_stats"):
        process_stats.run()
    [record] = [r for r in caplog.records if r.name == "canopy.process_stats"]
    prefix, payload = record.getMessage().split(" ", 1)
    assert prefix == "PROCESS_STATS"
    assert json.loads(payload)["pid"] == os.getpid()


def test_cgroup_v1_no_limit_is_reported_as_no_limit(monkeypatch):
    values = {"/sys/fs/cgroup/memory/memory.usage_in_bytes": 5 * 2**20,
              "/sys/fs/cgroup/memory/memory.limit_in_bytes": 2**63 - 4096}
    monkeypatch.setattr(process_stats, "_read_int", values.get)
    assert process_stats._cgroup_memory() == (5 * 2**20, None)
