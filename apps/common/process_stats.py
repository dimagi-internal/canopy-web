"""One log line a minute saying what this web process is doing, from inside it.

canopy-web serves every request, WebSocket and background thread from one
process, so when it pins a CPU or climbs toward the memory limit, nothing at the
request level names the cause: a killed process never logs its own request, and
work started from the off-request clock (a knowledge-base build, a vector sync)
has no request at all. ECS metrics say THAT the task is busy; this line says
WHAT: memory as the container's limit sees it, CPU over the interval, the live
threads grouped by name, the event loop's task count — and, while CPU is high,
the busiest threads with the Python stack each one is in right now.

It runs in-process (no profiler to install, no ptrace capability), on the
off-request clock with `dedupe=False`, because every web task must report on
itself. Logger `canopy.process_stats`; the message starts `PROCESS_STATS` so a
Logs Insights `filter @message like /PROCESS_STATS/` finds it. Linux-only parts
(`/proc`, cgroup files) are skipped where absent; the job never raises.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

from django.conf import settings

from . import ticks

logger = logging.getLogger("canopy.process_stats")

#: Threads using at least this share of one core over the interval get a stack.
HOT_THREAD_PCT = 20.0
#: How many hot threads to report, and how many innermost frames of each.
HOT_THREADS = 3
STACK_FRAMES = 12

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_last: dict = {}


def interval_s() -> float:
    return float(getattr(settings, "PROCESS_STATS_S", 60) or 0)


def run() -> None:
    logger.info("PROCESS_STATS %s", json.dumps(snapshot(), separators=(",", ":"), default=str))


def snapshot() -> dict:
    now = time.monotonic()
    cpu = _process_cpu_s()
    per_thread = _thread_cpu_ticks()
    prev = dict(_last)
    _last.update(at=now, cpu=cpu, threads=per_thread)

    out: dict = {"pid": os.getpid(), **_memory()}
    out["threads"] = _thread_names()
    out["asyncio_tasks"] = _asyncio_task_count()
    if "at" not in prev:
        return out  # the first sample has nothing to compare against
    elapsed = max(now - prev["at"], 1e-6)
    out["interval_s"] = round(elapsed, 1)
    out["cpu_pct"] = round(100 * (cpu - prev["cpu"]) / elapsed, 1)
    hot = _hot_threads(prev.get("threads", {}), per_thread, elapsed)
    if hot:
        out["hot_threads"] = hot
    return out


# -- memory ---------------------------------------------------------------

def _memory() -> dict:
    out = {}
    rss = _proc_status_kb("VmRSS")
    if rss is not None:
        out["rss_mb"] = round(rss / 1024, 1)
    used, limit = _cgroup_memory()
    if used is not None:
        out["cgroup_mb"] = round(used / 2**20, 1)
    if used is not None and limit:
        out["cgroup_pct"] = round(100 * used / limit, 1)
    return out


def _proc_status_kb(field: str) -> int | None:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _cgroup_memory() -> tuple[int | None, int | None]:
    """(bytes used, limit) for this container: cgroup v2, else v1."""
    for used_f, limit_f in (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
                            ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
                             "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        used = _read_int(used_f)
        if used is not None:
            limit = _read_int(limit_f)
            # v1 reports "no limit" as a huge number; v2 as "max" (not an int).
            return used, limit if limit and limit < 2**60 else None
    return None, None


def _read_int(path: str) -> int | None:
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


# -- CPU ------------------------------------------------------------------

def _process_cpu_s() -> float:
    t = os.times()
    return t.user + t.system


def _thread_cpu_ticks() -> dict[int, int]:
    """Native thread id -> utime+stime clock ticks, from /proc (Linux only)."""
    out = {}
    try:
        tids = os.listdir("/proc/self/task")
    except OSError:
        return out
    for tid in tids:
        try:
            stat = Path(f"/proc/self/task/{tid}/stat").read_text()
            # Fields after the ")" that closes the command name; utime/stime are 14/15.
            fields = stat[stat.rindex(")") + 2:].split()
            out[int(tid)] = int(fields[11]) + int(fields[12])
        except (OSError, ValueError, IndexError):
            continue
    return out


def _hot_threads(before: dict[int, int], after: dict[int, int], elapsed: float) -> list[dict]:
    by_native = {t.native_id: t for t in threading.enumerate() if t.native_id is not None}
    frames = sys._current_frames()  # noqa: SLF001 - the whole point: where each thread is now
    rows = []
    for tid, ticks_now in after.items():
        # A thread born during the interval spent all its CPU inside it, so it counts from 0.
        pct = 100 * (ticks_now - before.get(tid, 0)) / _CLK_TCK / elapsed
        if pct < HOT_THREAD_PCT:
            continue
        thread = by_native.get(tid)
        row = {"name": thread.name if thread else f"native-{tid}", "cpu_pct": round(pct, 1)}
        frame = frames.get(thread.ident) if thread else None
        if frame is not None:
            row["stack"] = [f"{f.filename.rsplit('site-packages/', 1)[-1]}:{f.lineno} {f.name}"
                            for f in traceback.extract_stack(frame)[-STACK_FRAMES:]]
        rows.append(row)
    rows.sort(key=lambda r: -r["cpu_pct"])
    return rows[:HOT_THREADS]


# -- threads and the event loop -------------------------------------------

_NUMBERED = re.compile(r"[-_ ]?\d+(_\d+)?$")


def _thread_names() -> dict[str, int]:
    """Live threads by name, numbered pool members folded together
    ("ThreadPoolExecutor-3_7" -> "ThreadPoolExecutor")."""
    return dict(Counter(_NUMBERED.sub("", t.name) or t.name for t in threading.enumerate()))


def _asyncio_task_count() -> int | None:
    loop = ticks.loop()
    if loop is None:
        return None
    for _ in range(3):  # the loop's task set can change under us; just try again
        try:
            return len(asyncio.all_tasks(loop))
        except RuntimeError:
            continue
    return None
