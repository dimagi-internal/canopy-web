"""`PR fast checks` repeats three of `Backend pytest (1/3)`'s steps; they must stay the same.

ci.yml's fast job runs the lint, migration-sync and system-check commands early so a PR fails
in a minute instead of at the end of the shard run. If the two copies drift, the fast job
passes what the real job rejects (or the reverse), and the point of it is lost. The comment
above the copy says "keep them in step"; this is that, enforced.
"""
from __future__ import annotations

import yaml

from tests._source import ROOT

CI = ROOT / ".github" / "workflows" / "ci.yml"

#: The steps both jobs carry, by the command they run.
SHARED = (
    "ruff check",
    "makemigrations --check",
    "manage.py check",
)


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]


def _job_named(prefix: str) -> dict:
    (job,) = [j for j in _jobs().values() if str(j.get("name", "")).startswith(prefix)]
    return job


def _step_for(job: dict, needle: str) -> dict:
    (step,) = [s for s in job["steps"] if needle in str(s.get("run", ""))]
    return step


def _normalised(step: dict) -> dict:
    """What a step does: its command and the environment it runs it in. The `if:` gate
    (the shard job runs these on shard 1 only) and the step `name` are about where it runs."""
    return {"run": " ".join(str(step["run"]).split()), "env": step.get("env") or {}}


def test_the_fast_job_runs_the_same_checks_as_the_backend_shards():
    shards = _job_named("Backend pytest (${{ matrix.shard }}")
    fast = _job_named("PR fast checks")
    for needle in SHARED:
        assert _normalised(_step_for(fast, needle)) == _normalised(_step_for(shards, needle)), (
            f"ci.yml: the `{needle}` step differs between `Backend pytest` and `PR fast checks`; "
            "change both together")


def test_each_shared_step_runs_once_in_a_shard_not_in_all_of_them():
    # The shard job is a matrix; the checks are repo-wide, so they run on shard 1 only.
    shards = _job_named("Backend pytest (${{ matrix.shard }}")
    for needle in SHARED:
        assert _step_for(shards, needle).get("if") == "matrix.shard == 1", needle
