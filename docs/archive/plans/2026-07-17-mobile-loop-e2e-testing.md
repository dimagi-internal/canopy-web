# Mobile-loop end-to-end testing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Three layers that let us prove the phone → canopy-web → runner → emdash loop works end to end: an automated cross-component E2E test, a live deploy-smoke script, and a real-emdash runbook + one-shot dispatch helper.

**Architecture:** L1 is a pytest in the canopy-web suite that runs the REAL runner code (`drain_one`) against a real `live_server` over HTTP with a real Bearer PAT, stubbing only `cdp_control` (the Electron edge) with a recording fake. L2 and the L3 helper are stdlib Python scripts hitting a live server's API with a PAT. L3 is a runbook for the human-gated real-emdash check.

**Tech Stack:** Django 5 + Django Ninja + pytest + `pytest-django` (`live_server`); stdlib `urllib`; the `canopy_runner` package (stdlib-only).

**Spec:** `docs/superpowers/specs/2026-07-17-mobile-loop-e2e-testing-design.md`.

## Global Constraints

- **Verify like CI:** run `uv run pytest` with `.env` moved aside (`mv .env /tmp/.env.aside; …; mv /tmp/.env.aside .env`). A gitignored `.env` otherwise masks CI failures.
- **`canopy_runner` is NOT on the main suite's import path** — it is a separate package (`packages/canopy_runner`). The L1 test file adds it with a path insert at the top (a per-file insert, not a shared `tests/conftest.py` — there is none and we add none).
- **`live_server` requires transactional DB** — mark the L1 test `@pytest.mark.django_db(transaction=True)` so the server thread sees committed writes.
- **The runner read/CDP edge must be the ONLY thing stubbed** in L1 — everything from the HTTP claim through `execute_turn` is the real code path.
- **Scripts write only to a runner the caller OWNS and only synthetic `smoke-*` rows, and always attempt cleanup** — safe to run against prod, self-undoing.
- **`PersonalToken.create_for_user(user=…, label=…)` returns `(raw, obj)`** — the raw value is the runner's Bearer credential.
- **`Config` required fields:** `base_url, token, runner_id, emdash_db, automation_ids (dict), expected_migration_id (int)`; `executor` defaults to `"cdp"`.
- **Tests:** `pytestmark`/`@pytest.mark.django_db`, fixtures inline per file.

## File Structure

- `tests/test_mobile_loop_e2e.py` — L1 automated cross-component E2E (create).
- `scripts/qa/smoke_mobile_loop.py` — L2 live deploy-smoke (create).
- `scripts/qa/dispatch_one_continue.py` — L3 one-shot dispatch helper (create).
- `docs/runbooks/mobile-loop-real-emdash.md` — L3 runbook (create).

---

## Task 1: L1 — automated cross-component E2E (`tests/test_mobile_loop_e2e.py`)

**Files:**
- Create: `tests/test_mobile_loop_e2e.py`

**Interfaces:**
- Consumes: `canopy_runner.main.drain_one`, `canopy_runner.config.Config`, `canopy_runner.client.Client`, `canopy_runner.cdp_control`, `canopy_runner.emdash`; the harness ORM models + endpoints; `pytest-django`'s `live_server`.
- Produces: two passing E2E tests — the reuse (continue-into-existing) path and the create (fresh-thread) path.

- [ ] **Step 1: Write the test file** (this is a test-authoring task; the "RED" is that `canopy_runner`'s real execute path must drive the recording fake — write the whole file, then run)

```python
"""End-to-end: a dispatched Continue, claimed and executed by the REAL runner code
(drain_one), lands in the exact emdash session — with only the Electron/CDP edge
stubbed. Real HTTP (live_server), real Bearer PAT, real claim + execute_turn.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client as DjangoClient
from django.utils import timezone

# canopy_runner is a separate stdlib-only package, not on the suite's path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "canopy_runner"))
from canopy_runner import cdp_control, emdash  # noqa: E402
from canopy_runner.client import Client  # noqa: E402
from canopy_runner.config import Config  # noqa: E402
from canopy_runner.main import drain_one  # noqa: E402

from apps.harness.models import Runner, SessionLink, Turn  # noqa: E402
from apps.tokens.models import PersonalToken  # noqa: E402
from apps.workspaces.models import Workspace, WorkspaceMembership  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

HOST = "jj@test-mac"


def _seed():
    """A dimagi user (single membership → flat routing resolves), a paired runner
    that can drive canopy-web, and a raw PAT for the runner to authenticate with."""
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    raw, _ = PersonalToken.create_for_user(user=user, label="e2e-runner")
    runner = Runner.objects.create(
        name="test-mbp", kind=Runner.EMDASH, host=HOST, paired_by=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        capabilities={"projects": ["canopy-web"]},
    )
    return user, ws, raw, runner


def _cfg(base_url: str, raw: str, runner_id: str, tmp_path) -> Config:
    return Config(
        base_url=base_url, token=raw, runner_id=str(runner_id),
        emdash_db=str(tmp_path / "emdash.db"), automation_ids={},
        expected_migration_id=0, executor="cdp", state_path=str(tmp_path / "state.json"),
    )


def _record_cdp(monkeypatch):
    """Swap the Electron edge for a recorder. Returns the calls list."""
    calls = {"open_and_send": [], "create_task": []}
    monkeypatch.setattr(cdp_control, "open_and_send",
                        lambda task, text, **kw: calls["open_and_send"].append((task, text)) or {"ok": True})
    monkeypatch.setattr(cdp_control, "create_task",
                        lambda project, prompt, **kw: calls["create_task"].append((project, prompt)) or {"task": f"{project}-new"})
    monkeypatch.setattr(cdp_control, "host_id", lambda: HOST)
    # The reuse path asks sqlite "is this task live?" — say yes without a real DB.
    monkeypatch.setattr(emdash, "task_state", lambda db, name: "live")
    return calls


def test_continue_reuses_the_exact_session_end_to_end(live_server, monkeypatch, tmp_path):
    user, ws, raw, runner = _seed()
    dc = DjangoClient()
    dc.force_login(user)

    # The runner reported this open session (creates the display row + the continue SessionLink).
    r = dc.post(f"/api/harness/runners/{runner.id}/sessions",
                {"sessions": [{"emdash_task": "cloud-runner", "project": "canopy-web",
                               "status": "in_progress"}]},
                content_type="application/json")
    assert r.status_code == 200, r.content
    assert SessionLink.objects.filter(project="canopy-web", thread_key="emdash:cloud-runner").exists()

    # The phone dispatched a Continue into that session.
    d = dc.post("/api/harness/turns/",
                {"project": "canopy-web", "origin": "manual", "idempotency_key": "e2e-reuse",
                 "prompt": "rerun the failing test",
                 "origin_ref": {"thread_key": "emdash:cloud-runner"}},
                content_type="application/json")
    assert d.status_code == 201, d.content
    turn_id = d.json()["id"]

    calls = _record_cdp(monkeypatch)
    result = drain_one(_cfg(live_server.url, raw, runner.id, tmp_path), Client(live_server.url, raw))

    # The prompt reached the EXACT task via reuse — not a fresh create.
    assert calls["open_and_send"] == [("cloud-runner", "rerun the failing test")]
    assert calls["create_task"] == []
    assert result.startswith("reused:")
    Turn.objects.get(id=turn_id).refresh_from_db()
    assert Turn.objects.get(id=turn_id).status == Turn.DONE


def test_continue_with_no_prior_session_creates_end_to_end(live_server, monkeypatch, tmp_path):
    user, ws, raw, runner = _seed()
    dc = DjangoClient()
    dc.force_login(user)

    # No report → no SessionLink for this thread. Dispatch a fresh Continue.
    d = dc.post("/api/harness/turns/",
                {"project": "canopy-web", "origin": "manual", "idempotency_key": "e2e-create",
                 "prompt": "start a fresh thread",
                 "origin_ref": {"thread_key": "emdash:brand-new"}},
                content_type="application/json")
    assert d.status_code == 201, d.content
    turn_id = d.json()["id"]

    calls = _record_cdp(monkeypatch)
    result = drain_one(_cfg(live_server.url, raw, runner.id, tmp_path), Client(live_server.url, raw))

    # A brand-new thread creates a session, not a reuse.
    assert len(calls["create_task"]) == 1
    assert calls["create_task"][0][0] == "canopy-web"
    assert "start a fresh thread" in calls["create_task"][0][1]
    assert calls["open_and_send"] == []
    assert result.startswith("created:")
    assert Turn.objects.get(id=turn_id).status == Turn.DONE
```

- [ ] **Step 2: Run it — the assertions verify the real chain**

Run: `mv .env /tmp/.env.aside; uv run pytest tests/test_mobile_loop_e2e.py -q; mv /tmp/.env.aside .env`
Expected: PASS (2). If it fails, read the failure — likely candidates and their fixes:
- Import of `canopy_runner` fails → the path insert line is wrong (check `packages/canopy_runner` is two levels up from `tests/`).
- `create_for_user` signature differs → open `apps/tokens/models.py` and match it.
- `drain_one`'s result string differs from `reused:`/`created:` → open `execute.py`'s return values and match the assertion to the real prefixes.
- `_paused_agents(cfg)` errors on the tmp `state_path` → point `state_path` at an existing tmp file or check what it reads.
- The turn isn't claimed (stays QUEUED) → confirm the runner's `capabilities.projects` includes `canopy-web` and the workspace/tenant matches (the dimagi single-membership user).

Fix the harness (not the assertions' intent) until green.

- [ ] **Step 3: Commit**

```bash
git add tests/test_mobile_loop_e2e.py
git commit -m "test(e2e): the mobile loop end to end — real runner drain_one over HTTP, recording fake emdash"
```

---

## Task 2: L2 — live deploy-smoke script (`scripts/qa/smoke_mobile_loop.py`)

**Files:**
- Create: `scripts/qa/smoke_mobile_loop.py`

**Interfaces:**
- Consumes: a live server URL + a PAT (env), the harness API.
- Produces: a runnable script printing a per-step PASS/FAIL summary, exit non-zero on failure, self-cleaning.

- [ ] **Step 1: Write the script** (mirror `scripts/qa/smoke_deployed.py`'s env convention: `CANOPY_URL` + a PAT env; use stdlib `urllib`, not Playwright — this is the API loop)

```python
"""Live smoke test of the canopy-mobile dispatch/continue loop, over the API.

Reports a synthetic open session to a runner YOU own, confirms it lists, dispatches
a Continue into it, confirms the server resolves that to reuse, then cleans up
(cancels the turn + clears the synthetic session). Safe against prod: it only ever
writes to your own runner and only `smoke-*` rows, and always cleans up.

Run:
    CANOPY_PAT=<raw-token> CANOPY_URL=https://labs.connect.dimagi.com/canopy \\
      uv run python scripts/qa/smoke_mobile_loop.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("CANOPY_URL", "").rstrip("/")
PAT = os.environ.get("CANOPY_PAT", "")
TASK = "smoke-loop"
PROJECT = "smoke"
THREAD = f"emdash:{TASK}"


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{URL}{path}", data=data, method=method)
    r.add_header("Authorization", f"Bearer {PAT}")
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def main() -> int:
    if not URL or not PAT:
        print("Set CANOPY_URL and CANOPY_PAT.", file=sys.stderr)
        return 2

    steps: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        steps.append((name, ok, detail))

    # 1. Find a runner the caller owns.
    st, runners = _req("GET", "/api/harness/runners/")
    runner = (runners or [None])[0] if isinstance(runners, list) else None
    check("has a runner", bool(runner), "" if runner else "no runner paired for this token")
    if not runner:
        return _report(steps)
    rid, ws = runner["id"], runner.get("workspace")

    turn_id = None
    try:
        # 2. Report a synthetic open session.
        st, _ = _req("POST", f"/api/harness/runners/{rid}/sessions",
                     {"sessions": [{"emdash_task": TASK, "project": PROJECT, "status": "in_progress"}]})
        check("report session", st == 200, f"HTTP {st}")

        # 3. It lists.
        st, sessions = _req("GET", "/api/harness/sessions")
        listed = any(s.get("emdash_task") == TASK for s in (sessions or []))
        check("session lists", listed, "reported session not in GET /sessions")

        # 4. Dispatch a Continue into it (tenant-scoped path — the pin the composer uses).
        st, turn = _req("POST", f"/api/w/{ws}/harness/turns/",
                        {"project": PROJECT, "origin": "manual",
                         "idempotency_key": f"smoke-{int(time.time())}",
                         "prompt": "smoke: continue this session",
                         "origin_ref": {"thread_key": THREAD}})
        turn_id = (turn or {}).get("id")
        check("dispatch continue", st in (200, 201) and bool(turn_id), f"HTTP {st}")

        # 5. The server resolves it to reuse of the exact task.
        st, plan = _req("POST", f"/api/harness/runners/{rid}/resolve-session",
                        {"project": PROJECT, "workspace": ws, "thread_key": THREAD})
        reuse = bool(plan and plan.get("reuse") and plan.get("emdash_task_id") == TASK)
        check("resolves to reuse", reuse, f"plan={plan}")
    finally:
        # 6. Cleanup — leave prod as found.
        if turn_id:
            _req("POST", f"/api/harness/turns/{turn_id}/cancel")
        _req("POST", f"/api/harness/runners/{rid}/sessions", {"sessions": []})

    return _report(steps)


def _report(steps: list[tuple[str, bool, str]]) -> int:
    print("\n=== mobile-loop smoke ===")
    for name, ok, detail in steps:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  — ' + detail) if detail and not ok else ''}")
    failed = [s for s in steps if not s[1]]
    print(f"{len(steps) - len(failed)}/{len(steps)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Smoke-check it against the local e2e backend**

In one shell: `cd frontend && bash e2e/backend.sh` (boots the seeded server on :8000 — it sets `REQUIRE_AUTH=False`). In another:
```bash
# REQUIRE_AUTH=False means any Bearer is accepted as the seeded user; a placeholder works.
CANOPY_URL=http://127.0.0.1:8000 CANOPY_PAT=placeholder uv run python scripts/qa/smoke_mobile_loop.py
```
Expected: prints the step summary. NOTE: the e2e seed's runner (`e2e-mbp`) has `capabilities.projects=["canopy-web"]` and workspace `dimagi`, but the synthetic `smoke` project + `resolve` still exercise steps 1–5 against real endpoints. If a step legitimately can't pass on the seed (e.g. the seeded runner isn't the token's first runner), adjust the seed check or document the expected local result in a comment — the script's real target is prod. Stop the backend afterward.

- [ ] **Step 3: Commit**

```bash
git add scripts/qa/smoke_mobile_loop.py
git commit -m "test(qa): live smoke script for the mobile dispatch/continue loop (self-cleaning)"
```

---

## Task 3: L3 — real-emdash runbook + one-shot dispatch helper

**Files:**
- Create: `scripts/qa/dispatch_one_continue.py`
- Create: `docs/runbooks/mobile-loop-real-emdash.md`

**Interfaces:**
- `dispatch_one_continue.py`: args `--project/-​-agent`, `--thread`, `--prompt`, optional `--workspace`; dispatches ONE turn and prints its id. Stops at the dispatch (no emdash automation).

- [ ] **Step 1: Write the helper**

```python
"""Dispatch exactly one Continue/turn against a live server and print its id.

The human then runs `python -m canopy_runner.main --drain-one --config …` and
eyeballs emdash. This helper never touches emdash — it stops at the dispatch.

Run:
    CANOPY_PAT=<raw> CANOPY_URL=<url> uv run python scripts/qa/dispatch_one_continue.py \\
      --project canopy-web --thread emdash:cloud-runner --prompt "add a comment to the header" \\
      [--workspace dimagi]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("CANOPY_URL", "").rstrip("/")
PAT = os.environ.get("CANOPY_PAT", "")


def _post(path: str, body: dict) -> tuple[int, object]:
    r = urllib.request.Request(f"{URL}{path}", data=json.dumps(body).encode(), method="POST")
    r.add_header("Authorization", f"Bearer {PAT}")
    r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


def main() -> int:
    if not URL or not PAT:
        print("Set CANOPY_URL and CANOPY_PAT.", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--project")
    g.add_argument("--agent")
    ap.add_argument("--thread", required=True, help="thread_key, e.g. emdash:cloud-runner")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--workspace", default="", help="required for a --project turn on a multi-workspace user")
    args = ap.parse_args()

    body = {
        "origin": "manual", "idempotency_key": f"dispatch1-{int(time.time())}",
        "prompt": args.prompt, "origin_ref": {"thread_key": args.thread},
    }
    if args.agent:
        body["agent_slug"] = args.agent
        path = "/api/harness/turns/"
    else:
        body["project"] = args.project
        path = f"/api/w/{args.workspace}/harness/turns/" if args.workspace else "/api/harness/turns/"

    st, turn = _post(path, body)
    if st in (200, 201) and turn:
        print(f"dispatched turn {turn['id']} (status={turn.get('status')}). "
              f"Now run: python -m canopy_runner.main --drain-one --config ~/.canopy/runner.json")
        return 0
    print(f"dispatch failed: HTTP {st} {turn}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write the runbook** — create `docs/runbooks/mobile-loop-real-emdash.md`:

```markdown
# Runbook: prove a phone Continue lands in a real emdash session

The final link of the mobile loop types into real emdash, so it is human-gated (not a
CI test). This proves it with a single turn, without restarting the fleet daemon.

## Preconditions
- The daemon checkout (`~/emdash-projects/canopy-web`) is updated to a build that has the
  repo-target runner code: `git -C ~/emdash-projects/canopy-web checkout main && git -C ~/emdash-projects/canopy-web pull`.
- emdash is running with the CDP port open (`--remote-debugging-port=9222`, the runner's
  `cdp_port`).
- The runner declares the target project. Add it in place (no re-pair) via the API:
  `PATCH /api/harness/runners/{id}` with `{"capabilities": {"agents": [...], "projects": ["canopy-web"]}}`.
- A **scratch** emdash task exists for the target project (don't test against a real work
  session first). Note its exact task name.

## Steps
1. Report is automatic once the updated daemon ticks (or use the smoke script's report), so
   the session shows on the phone's Supervisor → Sessions.
2. Dispatch ONE Continue into the scratch session — from the phone (Sessions → type → Continue),
   or:
   ```
   CANOPY_PAT=<raw> CANOPY_URL=https://labs.connect.dimagi.com/canopy \
     uv run python scripts/qa/dispatch_one_continue.py \
       --project canopy-web --workspace dimagi \
       --thread emdash:<scratch-task-name> --prompt "QA: add a one-line comment"
   ```
3. Take exactly that one turn (the pause sentinel does NOT block this; the fleet stays off):
   ```
   python -m canopy_runner.main --drain-one --config ~/.canopy/runner.json
   ```
4. Confirm the prompt appears in the scratch emdash session and the model acts on it.

## Rollback
- The turn is one-shot; nothing recurring is started. If you dispatched but don't want to
  run it, cancel it: `POST /api/harness/turns/{id}/cancel`.
- Remove `projects` from the runner's capabilities (`PATCH …`) to stop it claiming repo
  turns again until you're ready.
```

- [ ] **Step 3: Commit**

```bash
git add scripts/qa/dispatch_one_continue.py docs/runbooks/mobile-loop-real-emdash.md
git commit -m "docs(qa): real-emdash validation runbook + one-shot dispatch helper"
```

---

## Final verification (before PR)

- [ ] `mv .env /tmp/.env.aside; uv run pytest tests/test_mobile_loop_e2e.py -q; mv /tmp/.env.aside .env` — 2 passed.
- [ ] `mv .env /tmp/.env.aside; uv run pytest -q; mv /tmp/.env.aside .env` — full suite still green (the new L1 test collected + passing, nothing else disturbed).
- [ ] The L2 script + L3 helper `python -c "import ast; ast.parse(open(p).read())"` parse-clean, and L2 ran once against the local backend (or prod PAT) with a printed summary.
- [ ] PR, CI green, merge. No deploy needed (tests + scripts + docs only; no app/runtime change).

## Self-review notes (coverage against the spec)

- L1 (spec §Layer 1): both the reuse and create cases, real HTTP `live_server` + real `drain_one` + recording fake `cdp_control`/`task_state`; asserts the exact `(task, prompt)`, the turn `done`, reuse-vs-create. → Task 1.
- L2 (spec §Layer 2): report → list → dispatch → resolve → cleanup, PAT + `urllib`, self-cleaning, prod-safe. → Task 2.
- L3 (spec §Layer 3): the runbook + the stop-at-dispatch helper (no emdash automation). → Task 3.
- Non-goals honored: no automated emdash drive; the physical edge is stubbed (L1) or human-eyeballed (L3).
