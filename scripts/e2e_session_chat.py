#!/usr/bin/env python3
"""Live end-to-end check of session chat, against a real deployment.

**Why this is a script and not a pytest.** Everything under `tests/*_e2e.py`
runs in-process with the two ends faked — a captured screen in, a keystroke list
out. That is the right shape for seam coverage and it is exactly what passed,
repeatedly, while the live chain was broken: on 2026-08-01 four separate fixes
shipped green and were still wrong in production (a hook map scoped to watched
sessions, a dropped answer outcome, a stale server row, an idle prompt counted as
blocked). Each was found by driving the real thing.

So this drives the real thing: real canopy-web, real runner, real agent, real
tokens. It spawns an actual session and burns an agent turn per step.

    uv run python scripts/e2e_session_chat.py                     # everything
    uv run python scripts/e2e_session_chat.py --steps preflight   # cheap, no agent
    uv run python scripts/e2e_session_chat.py --keep              # leave the session

Exit code is 0 only if every selected step passed.

**What it covers, and why each step is here rather than in a unit test:**

  preflight  a session-capable runner is online — otherwise every later failure
             is really this one, reported in a confusing place
  create     a session exists and is addressable
  send       a message becomes a turn, gets claimed, and the agent's reply comes
             back. The core loop; nothing else matters if this is broken
  history    the tail is ordered and pages backwards. `turn_index` is both sort
             order and paging cursor, and it changed scheme once already
  reload     reset-from-transcript rebuilds the SAME conversation. These rows are
             a cache of a file on the runner's disk, so "reload" has to be a pure
             re-derivation — this is the step that catches the cache lying
  menu       an AskUserQuestion is captured, shown with the right options,
             ANSWERED, and the agent acts on the answer. The whole 2026-08-01
             incident lives in this one step
  stale_tap  tapping a dialog that is already gone CLEARS it. The reconciliation
             rule — a tap always reconciles the cache to the terminal — and the
             step that catches a refusal which keeps a disproven menu. Needs to
             run on the same box as the runner (it uses the loopback listener)
  close      the session retires and stays retired

Deliberately not covered: the WebSocket surface (this speaks REST, which is the
same reader — `serializers.pending_menu` — so a divergence there is a different
test), and push notifications.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_BASE = "https://labs.connect.dimagi.com/canopy"
DEFAULT_TOKEN_FILE = Path.home() / ".claude" / "canopy" / "workbench-token"

ALL_STEPS = ("preflight", "create", "send", "history", "reload", "menu",
             "stale_tap", "close")

# An agent turn is minutes, not seconds: a cold session starts a worktree and a
# fresh `claude`. Generous by default because a timeout here is indistinguishable
# from a real failure, and a false red on an e2e check is worse than a slow one.
TURN_TIMEOUT = 300
POLL_EVERY = 5


class Failure(Exception):
    """A step's assertion did not hold. Carries what was seen, not just what was
    expected — the point of an e2e run is to leave you able to debug."""


class Client:
    def __init__(self, base: str, token: str, verbose: bool = False):
        self.base = base.rstrip("/")
        self.token = token
        self.verbose = verbose

    def __call__(self, method: str, path: str, body=None, timeout: int = 30):
        url = f"{self.base}/api{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, method=method, data=data, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise Failure(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
        except Exception as exc:  # noqa: BLE001
            raise Failure(f"{method} {path} -> {type(exc).__name__}: {exc}")
        if self.verbose:
            print(f"    {method} {path} -> {raw[:160]}")
        return json.loads(raw) if raw.strip() else None


def wait_for(what: str, predicate, *, timeout: int, every: int = POLL_EVERY):
    """Poll `predicate` until it returns a truthy value. Returns it.

    The last observed value is carried into the failure, because "timed out" on
    its own tells you nothing about which hop stalled.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(every)
    raise Failure(f"timed out after {timeout}s waiting for {what}; last saw: {str(last)[:300]}")


def assistant_texts(session: dict) -> list[str]:
    out = []
    for m in session.get("messages") or []:
        if m.get("role") != "assistant":
            continue
        content = m.get("content") or {}
        text = content.get("text") if isinstance(content, dict) else None
        out.append(text or m.get("plaintext") or "")
    return out


# --- steps -----------------------------------------------------------------

def step_preflight(ctx):
    """A session-capable runner must be online. Without this every later step
    fails as a timeout, which reads as "chat is broken" rather than "no runner"."""
    runners = ctx["api"]("GET", "/harness/runners/")
    # `status` is the SERVED liveness (RunnerOut derives it from live_status);
    # `ready` and `sessions` are the two things a chat turn additionally needs.
    live = [r for r in runners
            if not r.get("retired")
            and str(r.get("status", "")).lower() == "online"
            and r.get("ready")
            and (r.get("capabilities") or {}).get("sessions")]
    if not live:
        seen = [(r.get("name"), r.get("status"), r.get("ready"),
                 bool((r.get("capabilities") or {}).get("sessions")))
                for r in runners if not r.get("retired")]
        raise Failure(f"no online+ready session-capable runner; saw (name,status,ready,sessions)={seen}")
    ctx["runner"] = live[0]
    if ctx["project"] not in ((live[0].get("capabilities") or {}).get("projects") or []):
        raise Failure(f"{live[0]['name']} does not declare project {ctx['project']!r}; "
                      f"a turn there would queue forever")
    ctx["runner"] = live[0]
    return f"{live[0]['name']} online+ready, sessions:true, has {ctx['project']}"


def step_create(ctx):
    session = ctx["api"]("POST", "/canopy-sessions/", {
        "project": ctx["project"],
        "title": f"e2e-{ctx['nonce']}",
        "metadata": {"e2e": True, "nonce": ctx["nonce"]},
    })
    ctx["session_id"] = session["id"]
    return f"session {session['id'][:8]} on project {ctx['project']}"


def step_send(ctx):
    """The core loop. A marker the agent must echo makes the assertion exact —
    "some assistant text arrived" would pass on a preamble, which is precisely
    how a broken bridge looked correct for eleven turns on 2026-07-26."""
    marker = f"E2E-REPLY-{ctx['nonce']}"
    ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/send", {
        "text": f"Reply with exactly this token and nothing else: {marker}",
        "client_id": str(uuid.uuid4()),
    })

    def replied():
        session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        return session if any(marker in t for t in assistant_texts(session)) else None

    wait_for(f"the agent to echo {marker}", replied, timeout=ctx["timeout"])
    ctx["marker"] = marker
    return f"agent echoed {marker}"


def step_history(ctx):
    """`turn_index` is both the sort order and the paging cursor, and the scheme
    changed once already (record ordinals -> composite). A conversation that
    renders shuffled is the failure this catches."""
    session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
    rows = session.get("messages") or []
    if not rows:
        raise Failure("no messages on a session that just replied")
    indexes = [m["turn_index"] for m in rows]
    if indexes != sorted(indexes):
        raise Failure(f"transcript is not ordered by turn_index: {indexes}")
    if len(set(indexes)) != len(indexes):
        raise Failure(f"duplicate turn_index values: {indexes}")

    # Paging backwards must not explode or repeat the tail.
    oldest = session.get("oldest_loaded_turn_index")
    if oldest is not None:
        page = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}/messages?before={oldest}")
        earlier = [m["turn_index"] for m in (page.get("messages") or [])]
        if any(i >= oldest for i in earlier):
            raise Failure(f"scroll-back returned rows at/after the cursor {oldest}: {earlier}")
    return f"{len(rows)} rows, ordered, cursor={oldest}"


def step_reload(ctx):
    """The rows are a CACHE of the runner's transcript file, so reload has to be
    a pure re-derivation. If the reply survives a reset, the cache is honest."""
    before = [t for t in assistant_texts(ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}"))
              if ctx["marker"] in t]
    result = ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/reset")
    if not result.get("ok"):
        raise Failure(f"reset refused: {result.get('reason')!r}")

    def rebuilt():
        session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        return session if any(ctx["marker"] in t for t in assistant_texts(session)) else None

    # The rebuild drains asynchronously — the runner ships the history back.
    wait_for("the transcript to be re-derived after reset", rebuilt, timeout=ctx["timeout"])
    return f"reply survived reset ({len(before)} matching rows before)"


def step_menu(ctx):
    """The 2026-08-01 incident, in one step: cause a dialog, see it, answer it,
    and confirm the agent acted on the answer.

    Every earlier verification stopped short of the last clause. A menu that
    appears and a tap that is accepted still tell you nothing about whether the
    keystroke landed — which is exactly the gap that survived four fixes."""
    ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/send", {
        "text": ("Use the AskUserQuestion tool right now to ask me one question: "
                 "'Pick a colour' with exactly two options, 'Red' and 'Blue'. "
                 "Ask nothing else and do nothing else first."),
        "client_id": str(uuid.uuid4()),
    })

    def has_menu():
        session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        menu = session.get("menu")
        return menu if menu and menu.get("options") else None

    menu = wait_for("an AskUserQuestion dialog to reach the API", has_menu, timeout=ctx["timeout"])
    labels = [o["label"] for o in menu["options"]]
    if "Red" not in labels:
        raise Failure(f"dialog reached the API but without the declared options: {labels}")
    red = next(o["number"] for o in menu["options"] if o["label"] == "Red")

    answer = ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/answer-menu", {"option": red})
    if not answer.get("ok"):
        raise Failure(f"answer-menu refused: {answer.get('reason')!r}")

    # The menu clearing is the runner confirming the key LANDED. A refusal comes
    # back on the same object as `answer_error`, so this distinguishes the two
    # instead of treating silence as success.
    def settled():
        session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        menu_now = session.get("menu")
        if menu_now and menu_now.get("answer_error"):
            raise Failure(f"the runner refused the tap: {menu_now['answer_error']} "
                          f"({menu_now.get('answer_note')})")
        return session if not menu_now else None

    wait_for("the dialog to clear after answering", settled, timeout=ctx["timeout"])

    # And the agent must actually have received "Red".
    def acted():
        session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        return session if any("Red" in t for t in assistant_texts(session)) else None

    wait_for("the agent to act on the answer", acted, timeout=ctx["timeout"])
    return f"asked, answered option {red} (Red), dialog cleared, agent saw it"


def step_stale_tap(ctx):
    """The reconciliation rule: a tap always reconciles the cache to the screen.

    Answering a dialog that is already gone must CLEAR it, not leave the buttons
    sitting there. This is the step that would have caught what I shipped first —
    a refusal that kept a menu the runner had just disproven.

    Safe by construction: there is no dialog on this session's screen, so no
    keystroke can be sent (`answer_menu_with` returns before `send_keys`).
    """
    session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
    if session.get("menu"):
        raise Failure("expected no dialog here; the menu step should have cleared it")

    # Plant one through the runner's own hook listener — the same path a real
    # AskUserQuestion takes, with input we control.
    ctx["plant"]("Stale-tap probe — this dialog is not on screen.",
                 [{"label": "A", "description": ""}, {"label": "B", "description": ""}])

    def planted():
        s = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        return s["menu"] if (s.get("menu") or {}).get("question", "").startswith("Stale-tap") else None

    menu = wait_for("the planted dialog to reach the API", planted, timeout=120)
    if not menu.get("observed_at"):
        raise Failure("menu carries no observed_at, so nothing can tell how stale it is")

    answer = ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/answer-menu", {"option": 1})
    if not answer.get("ok"):
        raise Failure(f"answer-menu refused outright: {answer.get('reason')!r}")

    def cleared():
        s = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        menu_now = s.get("menu")
        if menu_now and menu_now.get("answer_error") in ("wrong_pane", "unreachable", "no_session"):
            raise Failure(f"could not consult the screen: {menu_now['answer_error']}")
        return s if not menu_now else None

    wait_for("the stale dialog to be cleared by the tap", cleared, timeout=120)
    return "planted a dialog that is not on screen; the tap cleared it"


def step_close(ctx):
    result = ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/close")
    if not result.get("ok") and result.get("reason") not in ("already_closed",):
        raise Failure(f"close refused: {result.get('reason')!r}")

    def retired():
        session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        return session if session.get("status") == "archived" else None

    # A local session is retired by the runner deleting its emdash task and that
    # riding the next report, so this is not instant.
    wait_for("the session to retire", retired, timeout=180)
    ctx["closed"] = True
    return "session archived"


STEPS = {
    "preflight": step_preflight, "create": step_create, "send": step_send,
    "history": step_history, "reload": step_reload, "menu": step_menu,
    "stale_tap": step_stale_tap, "close": step_close,
}


def make_planter(ctx):
    """POST a synthetic `AskUserQuestion` into the LOCAL runner's hook listener.

    Only usable when the script runs on the same box as the runner — which is the
    point: it needs the loopback listener and the session's worktree path. The
    nonce and port are re-read every time because both change on every runner
    restart, and the runner auto-updates itself.
    """
    import glob
    import re

    def plant(question: str, options: list[dict]):
        settings = json.loads((Path.home() / ".claude" / "settings.json").read_text())
        found = set()
        for groups in (settings.get("hooks") or {}).values():
            for group in groups:
                for hook in group.get("hooks") or []:
                    cmd = hook.get("command", "")
                    tok = re.search(r"X-Canopy-Token: ([0-9a-f]+)", cmd)
                    port = re.search(r"127\.0\.0\.1:(\d+)", cmd)
                    if tok and port:
                        found.add((tok.group(1), port.group(1)))
        if len(found) != 1:
            raise Failure(f"could not resolve one hook listener from settings.json: {found}")
        token, port = found.pop()

        session = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")
        task = session.get("session_key")
        if not task:
            raise Failure("session has no session_key yet — has it bound to a runner?")
        # The worktree suffix changes when a task is recreated, so resolve it now
        # rather than reusing one from earlier in the run.
        dirs = glob.glob(str(Path.home() / "emdash" / "worktrees" / "*" / "emdash" / f"{task}-*"))
        if len(dirs) != 1:
            raise Failure(f"could not resolve one worktree for {task}: {dirs}")

        body = json.dumps({
            "hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
            "cwd": dirs[0],
            "tool_input": {"questions": [{"question": question, "header": "E2E",
                                          "options": options}]},
        }).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/hook", method="POST", data=body,
                                     headers={"Content-Type": "application/json",
                                              "X-Canopy-Token": token})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as exc:  # noqa: BLE001
            raise Failure(f"could not reach the local hook listener on :{port}: {exc}")

    return plant


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=DEFAULT_BASE)
    p.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    p.add_argument("--token", default="")
    p.add_argument("--project", default="canopy-web",
                   help="repo to open the chat against (agentless project chat)")
    p.add_argument("--steps", default=",".join(ALL_STEPS),
                   help=f"comma-separated subset of: {','.join(ALL_STEPS)}")
    p.add_argument("--timeout", type=int, default=TURN_TIMEOUT,
                   help="seconds to wait for any one agent turn")
    p.add_argument("--keep", action="store_true",
                   help="do not close the session at the end (for inspection)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    token = args.token or Path(args.token_file).read_text().strip()
    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        print(f"unknown step(s): {unknown}; known: {list(STEPS)}", file=sys.stderr)
        return 2
    # `create` is a precondition for everything after it, and forgetting it turns
    # a targeted run into a confusing crash.
    if any(s in steps for s in ("send", "history", "reload", "menu", "stale_tap", "close")) \
            and "create" not in steps:
        print("steps after 'create' need it too — add create to --steps", file=sys.stderr)
        return 2

    ctx = {
        "api": Client(args.base_url, token, args.verbose),
        "project": args.project,
        "nonce": uuid.uuid4().hex[:8],
        "timeout": args.timeout,
        "session_id": None,
        "closed": False,
    }
    ctx["plant"] = make_planter(ctx)

    print(f"canopy session-chat e2e  ·  {args.base_url}  ·  project={args.project}  "
          f"·  nonce={ctx['nonce']}")
    print("this spawns a REAL agent session and spends tokens.\n")

    failed = None
    for name in steps:
        started = time.time()
        try:
            detail = STEPS[name](ctx)
            print(f"  PASS  {name:<10} {time.time()-started:6.1f}s  {detail}")
        except Failure as exc:
            print(f"  FAIL  {name:<10} {time.time()-started:6.1f}s  {exc}")
            failed = name
            break
        except KeyboardInterrupt:
            print(f"\n  ABORTED during {name}")
            failed = name
            break

    # Always try to clean up, including after a failure — a leaked session holds
    # a worktree and an agent process on somebody's laptop.
    if ctx["session_id"] and not ctx["closed"] and not args.keep:
        try:
            ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/close")
            print(f"\ncleaned up session {ctx['session_id'][:8]}")
        except Failure as exc:
            print(f"\ncould not clean up session {ctx['session_id']}: {exc}")
    elif ctx["session_id"] and args.keep:
        print(f"\nleft session {ctx['session_id']} open (--keep)")

    print("\nRESULT:", "FAILED at " + failed if failed else "all steps passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
