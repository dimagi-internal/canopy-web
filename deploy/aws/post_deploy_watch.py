"""Watch canopy-web for a few minutes after a rollout and say whether to roll back.

The deploy workflow runs this once the service is at steady state on the new task
definition. It polls three things:

- the service's log group, counting the lines a broken release produces (see
  CATEGORIES): DB pool exhaustion, unhandled ASGI exceptions (HTTP and websocket),
  tracebacks, websocket/session-socket failures, and django.request ERRORs;
- the ALB target group: a registered target that stays `unhealthy`;
- `/health/` on the public URL.

Exit codes: 0 clean; 3 tripped (the workflow rolls the service back to the task
definition it recorded before deploying); 2 the watch itself could not run (AWS
errors). 2 never rolls back: a guard that cannot see must not take prod down.

Thresholds are per WINDOW (the whole watch, ~5 minutes) and were set by scanning
real history in 5-minute slices: a clean day's worst slice stays well under every
limit, and the PoolTimeout outage crosses several of them inside its first minute.
Re-run that calibration with `--start/--end` (see main()) before changing them.

The scan is stdlib-only and talks to AWS through the `aws` CLI, so it runs the
same on a laptop (`AWS_PROFILE=labs`) as in the workflow.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

LOG_GROUP = "/ecs/labs-jj-canopy-web"
TARGET_GROUP = "labs-jj-canopy-web-tg"
HEALTH_URL = "https://canopy.dimagi.com/health/"

# A CloudWatch filter pattern that admits every line any category below can match,
# so a scan pulls hundreds of lines rather than the whole log.
FILTER_PATTERN = (
    '?PoolTimeout ?Traceback ?"Exception in ASGI application" '
    '?websockets_sansio_impl ?"socket connect failed" ?"django.request"'
)

_DJANGO_REQUEST_ERROR = re.compile(r'"severity":\s*"ERROR",\s*"logger":\s*"django\.request"')

# name -> (predicate over one log line, a human label). Each traceback is several
# log events (uvicorn writes one per line), so a category matches ONE line of it:
# the exception line, not its frames.
CATEGORIES: dict[str, tuple] = {
    "pool_timeout": (
        lambda m: "psycopg_pool.PoolTimeout:" in m or ("PoolTimeout" in m and '"severity": "ERROR"' in m),
        "psycopg_pool.PoolTimeout (DB pool exhausted)",
    ),
    "asgi_exception": (
        lambda m: "Exception in ASGI application" in m,
        "Exception in ASGI application (unhandled, HTTP or websocket)",
    ),
    "traceback": (
        lambda m: m.startswith("Traceback (most recent call last)"),
        "Traceback",
    ),
    # A websocket handler that raised (uvicorn's websocket protocol frame in the
    # traceback), or a session socket that could not connect (canopy_sessions.consumers).
    "socket_failure": (
        lambda m: ("websockets_sansio_impl.py" in m and "run_asgi" in m) or "session socket connect failed" in m,
        "websocket / session-socket failure",
    ),
    "django_request_error": (
        lambda m: bool(_DJANGO_REQUEST_ERROR.search(m)),
        "django.request ERROR (5xx)",
    ),
}

# Trip when a category's count over the window is AT LEAST this. Calibrated on a
# week of 5-minute slices, about twenty deploys among them. The worst clean slice
# was pool_timeout 0, asgi_exception 7, traceback 7, socket_failure 4,
# django_request_error 2; the PoolTimeout outage's first slice was 1246 / 396 /
# 792 / 68 / 493. Each limit sits at about twice the clean worst (PoolTimeout,
# never seen clean, at 3) and an order of magnitude under the outage.
THRESHOLDS: dict[str, int] = {
    "pool_timeout": 3,
    "asgi_exception": 15,
    "traceback": 20,
    "socket_failure": 10,
    "django_request_error": 10,
}

# Consecutive bad polls before the target group or /health/ trips the watch.
UNHEALTHY_POLLS = 3
HEALTH_FAIL_POLLS = 3

SAMPLE_LINES = 5


class WatchError(Exception):
    """The watch could not read what it needs (exit 2, no rollback)."""


def classify(message: str) -> list[str]:
    return [name for name, (match, _) in CATEGORIES.items() if match(message)]


@dataclass
class Tally:
    counts: dict[str, int] = field(default_factory=lambda: {k: 0 for k in CATEGORIES})
    samples: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in CATEGORIES})

    def add(self, message: str) -> None:
        for name in classify(message):
            self.counts[name] += 1
            if len(self.samples[name]) < SAMPLE_LINES:
                self.samples[name].append(message.strip()[:400])

    def tripped(self) -> list[str]:
        return [k for k, limit in THRESHOLDS.items() if self.counts[k] >= limit]


def _aws(*args: str) -> dict:
    out = subprocess.run(["aws", *args, "--output", "json"], capture_output=True, text=True)
    if out.returncode:
        raise WatchError(f"aws {' '.join(args[:2])} failed: {out.stderr.strip()[:400]}")
    return json.loads(out.stdout or "{}")


def fetch_messages(start: datetime, end: datetime, log_group: str = LOG_GROUP) -> list[tuple[int, str]]:
    """(timestamp ms, line) for every line in [start, end) that any category could match (the CLI paginates)."""
    data = _aws(
        "logs",
        "filter-log-events",
        "--log-group-name",
        log_group,
        "--start-time",
        str(int(start.timestamp() * 1000)),
        "--end-time",
        str(int(end.timestamp() * 1000)),
        "--filter-pattern",
        FILTER_PATTERN,
    )
    return [(e.get("timestamp", 0), e["message"]) for e in data.get("events", [])]


def target_states(target_group: str = TARGET_GROUP) -> list[str]:
    tg = _aws("elbv2", "describe-target-groups", "--names", target_group)
    arn = tg["TargetGroups"][0]["TargetGroupArn"]
    health = _aws("elbv2", "describe-target-health", "--target-group-arn", arn)
    return [d["TargetHealth"]["State"] for d in health.get("TargetHealthDescriptions", [])]


def health_ok(url: str = HEALTH_URL) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status == 200, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001 - any failure to answer is the signal
        return False, type(e).__name__


@dataclass
class Verdict:
    tally: Tally
    reasons: list[str]

    @property
    def tripped(self) -> bool:
        return bool(self.reasons)


def scan_window(start: datetime, end: datetime) -> Verdict:
    tally = Tally()
    for _, m in fetch_messages(start, end):
        tally.add(m)
    reasons = [f"{CATEGORIES[k][1]}: {tally.counts[k]} (limit {THRESHOLDS[k]})" for k in tally.tripped()]
    return Verdict(tally, reasons)


def watch(
    seconds: int, interval: int, since: datetime, check_targets: bool = True, check_health: bool = True
) -> Verdict:
    """Poll until `seconds` pass or something trips; logs are counted from `since`."""
    deadline = time.monotonic() + seconds
    unhealthy_run = health_run = 0
    while True:
        now = datetime.now(UTC)
        verdict = scan_window(since, now)
        reasons = list(verdict.reasons)
        if check_targets:
            states = target_states()
            # draining/initial are a rollout's normal states; only `unhealthy` counts.
            unhealthy_run = unhealthy_run + 1 if "unhealthy" in states else 0
            if unhealthy_run >= UNHEALTHY_POLLS:
                reasons.append(
                    f"target group {TARGET_GROUP}: a target unhealthy on "
                    f"{unhealthy_run} polls in a row (states {states})"
                )
        if check_health:
            ok, why = health_ok()
            health_run = 0 if ok else health_run + 1
            if health_run >= HEALTH_FAIL_POLLS:
                reasons.append(f"{HEALTH_URL} failed {health_run} polls in a row ({why})")
        counts = " ".join(f"{k}={v}" for k, v in verdict.tally.counts.items())
        print(f"{now:%H:%M:%S} {counts} unhealthy_run={unhealthy_run} health_fail_run={health_run}", flush=True)
        if reasons or time.monotonic() >= deadline:
            return Verdict(verdict.tally, reasons)
        time.sleep(interval)


def summary_markdown(v: Verdict, since: datetime, until: datetime) -> str:
    rows = "\n".join(f"| {CATEGORIES[k][1]} | {v.tally.counts[k]} | {THRESHOLDS[k]} |" for k in CATEGORIES)
    head = "TRIPPED" if v.tripped else "clean"
    out = [
        f"### Post-deploy watch: {head}",
        "",
        f"Window {since:%H:%M:%S}–{until:%H:%M:%S} UTC on `{LOG_GROUP}`.",
        "",
        "| Signal | Count | Trips at |",
        "|---|---|---|",
        rows,
        "",
    ]
    if v.reasons:
        out += ["**Tripped on:**", ""] + [f"- {r}" for r in v.reasons] + [""]
        for k in CATEGORIES:
            if v.tally.samples[k] and v.tally.counts[k]:
                out += [f"<details><summary>Sample lines: {CATEGORIES[k][1]}</summary>", "", "```"]
                out += v.tally.samples[k] + ["```", "</details>", ""]
    return "\n".join(out)


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def calibrate(start: datetime, end: datetime, slice_minutes: int) -> int:
    """Scan [start, end) in fixed slices and print each slice's counts and verdict."""
    events = fetch_messages(start, end)
    step = timedelta(minutes=slice_minutes)
    t = start
    worst = {k: 0 for k in CATEGORIES}
    trips = 0
    while t < end:
        lo, hi = int(t.timestamp() * 1000), int((t + step).timestamp() * 1000)
        tally = Tally()
        for ts, m in events:
            if lo <= ts < hi:
                tally.add(m)
        for k in CATEGORIES:
            worst[k] = max(worst[k], tally.counts[k])
        hit = tally.tripped()
        trips += bool(hit)
        if any(tally.counts.values()) or hit:
            counts = " ".join(f"{k}={v}" for k, v in tally.counts.items())
            print(f"{t:%m-%d %H:%M} {counts}{'  TRIP ' + ','.join(hit) if hit else ''}")
        t += step
    print(f"worst slice: {worst}")
    print(f"thresholds:  {THRESHOLDS}")
    print(f"slices tripped: {trips}")
    return 3 if trips else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--seconds", type=int, default=300, help="how long to watch")
    p.add_argument("--interval", type=int, default=30, help="seconds between polls")
    p.add_argument("--since", help="count log lines from this ISO time (default: now)")
    p.add_argument("--start", help="calibrate: scan history from this ISO time")
    p.add_argument("--end", help="calibrate: scan history up to this ISO time")
    p.add_argument("--slice-minutes", type=int, default=5)
    a = p.parse_args(argv)
    try:
        if a.start:
            end = _parse_ts(a.end) if a.end else datetime.now(UTC)
            return calibrate(_parse_ts(a.start), end, a.slice_minutes)
        since = _parse_ts(a.since) if a.since else datetime.now(UTC)
        v = watch(a.seconds, a.interval, since)
    except WatchError as e:
        print(f"::warning::Post-deploy watch could not run: {e}")
        return 2
    md = summary_markdown(v, since, datetime.now(UTC))
    print(md)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(md + "\n")
    return 3 if v.tripped else 0


if __name__ == "__main__":
    sys.exit(main())
