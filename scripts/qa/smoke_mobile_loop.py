"""Live smoke test of the canopy-mobile dispatch/continue loop, over the API.

Reports a synthetic open session to a runner YOU own, confirms it lists, dispatches
a Continue into it, confirms the server resolves that to reuse, then cleans up
(cancels the turn + clears the synthetic session). Safe against prod: it only ever
writes to your own runner and only `smoke-*` rows, and always cleans up.

Run:
    CANOPY_PAT=<raw-token> CANOPY_URL=https://labs.connect.dimagi.com/canopy \
      uv run python scripts/qa/smoke_mobile_loop.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

# What canopy records as the program behind anything this script creates
# (X-Canopy-Client, User-Agent, X-Canopy-Parent-* from the environment).
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import script_provenance  # noqa: E402

SCRIPT = "smoke_mobile_loop.py"
NONCE = os.urandom(4).hex()
URL = os.environ.get("CANOPY_URL", "").rstrip("/")
PAT = os.environ.get("CANOPY_PAT", "")
TASK = "smoke-loop"
PROJECT = "smoke"
THREAD = f"emdash:{TASK}"


def _req(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(f"{URL}{path}", data=data, method=method)
    r.add_header("Authorization", f"Bearer {PAT}")
    for k, v in script_provenance.headers(SCRIPT, NONCE).items():
        r.add_header(k, v)
    if data is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return e.code, raw[:200].decode(errors="replace")


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
    check("has a runner", bool(runner), "" if runner else f"no runner for this token (HTTP {st})")
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
        listed = isinstance(sessions, list) and any(s.get("emdash_task") == TASK for s in sessions)
        check("session lists", listed, "reported session not in GET /sessions")

        # 4. Dispatch a Continue into it (tenant-scoped path — the pin the composer uses).
        turn_path = f"/api/w/{ws}/harness/turns/" if ws else "/api/harness/turns/"
        st, turn = _req("POST", turn_path,
                        {"project": PROJECT, "origin": "manual",
                         "idempotency_key": f"smoke-{int(time.time())}",
                         "prompt": "smoke: continue this session",
                         "origin_ref": {"thread_key": THREAD,
                                        "e2e": {"script": SCRIPT, "nonce": NONCE}}})
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        check("dispatch continue", st in (200, 201) and bool(turn_id), f"HTTP {st} {turn}")

        # 5. The server resolves it to reuse of the exact task.
        st, plan = _req("POST", f"/api/harness/runners/{rid}/resolve-session",
                        {"project": PROJECT, "workspace": ws, "thread_key": THREAD})
        reuse = isinstance(plan, dict) and plan.get("reuse") and plan.get("session_key") == TASK
        check("resolves to reuse", bool(reuse), f"plan={plan}")
    finally:
        # 6. Cleanup — leave the server as found.
        if turn_id:
            _req("POST", f"/api/harness/turns/{turn_id}/cancel")
        _req("POST", f"/api/harness/runners/{rid}/sessions", {"sessions": []})

    return _report(steps)


def _report(steps: list[tuple[str, bool, str]]) -> int:
    print("\n=== mobile-loop smoke ===")
    for name, ok, detail in steps:
        suffix = f"  — {detail}" if detail and not ok else ""
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{suffix}")
    failed = [s for s in steps if not s[1]]
    print(f"{len(steps) - len(failed)}/{len(steps)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
