"""The post-deploy watch trips on a broken release, stays quiet on normal noise, and the
deploy workflow wires it between the rollout and the held-back drops."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import yaml

from deploy.aws import post_deploy_watch as w

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/deploy-labs.yml"

# Real lines from the log group, as CloudWatch stores them (one event per line).
POOL = "psycopg_pool.PoolTimeout: couldn't get a connection after 10.00 sec"
RAISE = "    raise PoolTimeout("
ASGI = "ERROR:    Exception in ASGI application"
TB = "Traceback (most recent call last):"
WS_FRAME = (
    '  File "/usr/local/lib/python3.13/site-packages/uvicorn/protocols/websockets/'
    'websockets_sansio_impl.py", line 409, in run_asgi'
)
SESSION_SOCKET = (
    '{"timestamp": "2026-11-24T16:13:27.264+00:00", "severity": "ERROR", '
    '"logger": "apps.canopy_sessions.consumers", "message": "session socket connect failed: session=x"}'
)
DJANGO_5XX = (
    '{"timestamp": "2026-11-24T15:45:41.759+00:00", "severity": "ERROR", '
    '"logger": "django.request", "message": "Internal Server Error: /api/harness/x"}'
)
ACCEPTED = 'INFO:     - - "WebSocket /ws/runner/abc/" [accepted]'
DJANGO_404 = (
    '{"timestamp": "2026-11-24T15:45:41.759+00:00", "severity": "WARNING", '
    '"logger": "django.request", "message": "Not Found: /x"}'
)


def test_each_line_lands_in_its_category():
    assert w.classify(POOL) == ["pool_timeout"]
    assert w.classify(ASGI) == ["asgi_exception"]
    assert w.classify(TB) == ["traceback"]
    assert w.classify(WS_FRAME) == ["socket_failure"]
    assert w.classify(SESSION_SOCKET) == ["socket_failure"]
    assert w.classify(DJANGO_5XX) == ["django_request_error"]


def test_noise_and_traceback_frames_count_for_nothing():
    for line in (RAISE, ACCEPTED, DJANGO_404, "INFO: GET /health/ 200"):
        assert w.classify(line) == [], line


def tally(*lines_and_counts):
    t = w.Tally()
    for line, n in lines_and_counts:
        for _ in range(n):
            t.add(line)
    return t


def test_a_normal_five_minutes_does_not_trip():
    # The worst clean slice seen in calibration: a handful of websocket disconnects
    # and one-off exceptions.
    t = tally((ASGI, 7), (TB, 7), (WS_FRAME, 4), (DJANGO_5XX, 2))
    assert t.tripped() == []


def test_the_pool_outage_trips_on_its_first_minute():
    # A fraction of one minute of the PoolTimeout outage.
    t = tally((POOL, 40), (ASGI, 15), (TB, 30), (WS_FRAME, 10), (DJANGO_5XX, 30))
    assert set(t.tripped()) == set(w.THRESHOLDS)


def test_one_pool_timeout_alone_is_not_an_outage():
    assert tally((POOL, 1)).tripped() == []


def test_samples_are_capped():
    t = tally((POOL, 50))
    assert len(t.samples["pool_timeout"]) == w.SAMPLE_LINES


def test_the_filter_pattern_admits_every_category():
    # A category the CloudWatch filter drops would silently never count.
    for needle in (
        "PoolTimeout",
        "Traceback",
        "Exception in ASGI application",
        "websockets_sansio_impl",
        "socket connect failed",
        "django.request",
    ):
        assert needle in w.FILTER_PATTERN


def test_summary_names_what_tripped_with_sample_lines():
    from datetime import datetime

    t = tally((POOL, 5))
    v = w.Verdict(t, [f"pool: {t.counts['pool_timeout']}"])
    now = datetime.now(UTC)
    md = w.summary_markdown(v, now, now)
    assert "TRIPPED" in md and POOL in md


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def test_dispatch_requires_a_reason_and_a_session_and_names_the_run():
    wf = _workflow()
    on = wf.get("on") or wf.get(True)
    inputs = on["workflow_dispatch"]["inputs"]
    for name in ("reason", "session"):
        assert inputs[name]["required"] is True
        assert f"inputs.{name}" in wf["run-name"]


def test_watch_runs_after_the_rollout_and_before_the_drops():
    steps = [s.get("name", "") for s in _workflow()["jobs"]["deploy"]["steps"]]
    rec = steps.index("Record the rollback target")
    migrate = steps.index("Run database migrations")
    rollout = steps.index("Deploy (CloudFormation owns the task definition + service)")
    watch = steps.index("Post-deploy watch")
    back = steps.index("Roll back to the recorded task definition")
    drops = steps.index("Apply the drops held back from before the rollout")
    assert rec < migrate < rollout < watch < back < drops


def test_rollback_uses_the_recorded_task_definition():
    steps = {s.get("name"): s for s in _workflow()["jobs"]["deploy"]["steps"]}
    back = steps["Roll back to the recorded task definition"]
    assert back["if"] == "${{ steps.watch.outputs.tripped == 'true' }}"
    assert back["env"]["PREV"] == "${{ steps.prev.outputs.task_def }}"
    assert '--task-definition "$PREV"' in back["run"]
    assert "GITHUB_STEP_SUMMARY" in back["run"]
    assert "post_deploy_watch" in steps["Post-deploy watch"]["run"]
