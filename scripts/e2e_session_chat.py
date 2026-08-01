#!/usr/bin/env python3
"""Live end-to-end check of session chat, against a real deployment.

**Why this is a script and not a pytest.** Everything under `tests/*_e2e.py` runs
in-process with both ends faked — a captured screen in, a keystroke list out.
That is the right shape for seam coverage, and it is exactly what passed while
the live chain was broken: on 2026-08-01 several fixes shipped green and were
still wrong in production. Every one was found by driving the real thing.

So this drives the real thing: real canopy-web, real runner, real agent, real
tokens. It spawns a session and spends an agent turn per step.

The session is PINNED to one runner — by default the one on this machine — for
two reasons: you can watch it happen in emdash, and the answer step's keystroke
has a known destination rather than wherever routing chose.

    uv run python scripts/e2e_session_chat.py
    uv run python scripts/e2e_session_chat.py --steps check_runner    # free
    uv run python scripts/e2e_session_chat.py --keep                  # leave it open

Exit code is 0 only if every selected step passed.

**The steps, and what each guarantees:**

  check_runner            this box is online, ready, and can run the agent
  create_session          a session exists and is pinned here
  send_and_reply          you send a message and the agent answers
  answer_from_the_web     a dialog appears, you answer it FROM THE WEB, the
                          keystroke lands, and THE AGENT USES YOUR ANSWER
  scroll_back             history is ordered and pages backwards
  rebuild_from_transcript reset rebuilds the same conversation
  close_session           the session ends and stays ended

`answer_from_the_web` is the point of the whole file. Every check run during the
2026-08-01 incident stopped one clause short of it — a menu appeared, or a tap
was accepted — and neither says the key reached the terminal. Only the agent
acting on the answer says that.

Deliberately NOT here: a dialog answered at the KEYBOARD while the phone still
shows it. That race is real (the runner reports every ~10s, so there is a
window), but reproducing it means fabricating state the system cannot produce on
its own, and the recovery is already pinned by unit tests. A live script should
test what the product does, not what a test can inject.
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
RUNNER_CONFIG = Path.home() / ".canopy" / "runner.json"

ALL_STEPS = ("check_runner", "create_session", "send_and_reply",
             "answer_from_the_web", "scroll_back", "rebuild_from_transcript",
             "close_session")

# An agent turn is minutes, not seconds: a cold session builds a worktree and
# starts a fresh `claude`. Generous on purpose — a timeout here is
# indistinguishable from a real failure, and a false red is worse than a slow
# pass.
TURN_TIMEOUT = 420
POLL_EVERY = 5


class Failure(Exception):
    """A step's assertion did not hold. Carries what was SEEN, not just what was
    expected — the point of a live run is to leave you able to debug it."""


class Client:
    def __init__(self, base: str, token: str, verbose: bool = False):
        self.base, self.token, self.verbose = base.rstrip("/"), token, verbose

    def __call__(self, method: str, path: str, body=None, timeout: int = 30):
        url = f"{self.base}/api{path}"
        req = urllib.request.Request(
            url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raise Failure(f"{method} {path} -> HTTP {exc.code}: {exc.read().decode()[:300]}")
        except Exception as exc:  # noqa: BLE001
            raise Failure(f"{method} {path} -> {type(exc).__name__}: {exc}")
        if self.verbose:
            print(f"    {method} {path} -> {raw[:200]}")
        return json.loads(raw) if raw.strip() else None


def wait_for(what: str, predicate, *, timeout: int, every: int = POLL_EVERY):
    """Poll until `predicate` returns something truthy, and return it.

    The last observed value rides into the failure: "timed out" alone tells you
    nothing about which hop stalled.
    """
    deadline, last = time.time() + timeout, None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(every)
    raise Failure(f"timed out after {timeout}s waiting for {what}; last saw: {str(last)[:400]}")


def session(ctx) -> dict:
    return ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}")


def assistant_texts(sess: dict) -> list[str]:
    out = []
    for m in sess.get("messages") or []:
        if m.get("role") != "assistant":
            continue
        content = m.get("content") or {}
        out.append((content.get("text") if isinstance(content, dict) else None)
                   or m.get("plaintext") or "")
    return out


# --- steps -----------------------------------------------------------------

def check_runner(ctx):
    """Everything downstream fails as a timeout if this is wrong, which reads as
    "chat is broken" rather than "no runner"."""
    runners = ctx["api"]("GET", "/harness/runners/")
    match = [r for r in runners if r.get("id") == ctx["runner_id"]]
    if not match:
        raise Failure(f"runner {ctx['runner_id']} is not in the fleet you can see")
    r = match[0]
    problems = []
    if str(r.get("status", "")).lower() != "online":
        problems.append(f"status={r.get('status')}")
    if not r.get("ready"):
        problems.append("not ready")
    if not (r.get("capabilities") or {}).get("sessions"):
        problems.append("not session-capable")
    if problems:
        raise Failure(f"{r.get('name')}: {', '.join(problems)}")

    agent = ctx["api"]("GET", f"/agents/{ctx['agent']}/")
    ctx["workspace"] = ctx["workspace"] or agent.get("workspace") or ""
    if not ctx["workspace"]:
        raise Failure(f"could not resolve a workspace for agent {ctx['agent']}; pass --workspace")
    ctx["runner_name"] = r.get("name")
    return f"{r.get('name')} online+ready · agent {ctx['agent']} in {ctx['workspace']}"


def create_session(ctx):
    """Pinned at creation: `runner_id` pins the session's first turn, and the
    binding then forms on that runner — so every later step, including the
    keystroke, has a known destination you can watch in emdash."""
    sess = ctx["api"]("POST", f"/w/{ctx['workspace']}/canopy-sessions/", {
        "agent_slug": ctx["agent"],
        "title": f"e2e-{ctx['nonce']}",
        "metadata": {"e2e": True, "nonce": ctx["nonce"]},
        "runner_id": ctx["runner_id"],
    })
    ctx["session_id"] = sess["id"]
    return f"session {sess['id'][:8]} · agent {ctx['agent']} · pinned to {ctx['runner_name']}"


def send_and_reply(ctx):
    """The core loop. A marker the agent must echo makes this exact — "some
    assistant text arrived" would pass on a preamble, which is how a broken
    bridge looked healthy for eleven turns on 2026-07-26."""
    marker = f"E2E-REPLY-{ctx['nonce']}"
    ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/send", {
        "text": f"Reply with exactly this token and nothing else: {marker}",
        "client_id": str(uuid.uuid4()),
    })
    wait_for(f"the agent to echo {marker}",
             lambda: any(marker in t for t in assistant_texts(session(ctx))),
             timeout=ctx["timeout"])
    ctx["marker"] = marker
    return f"agent echoed {marker}"


def answer_from_the_web(ctx):
    """**The step this file exists for.**

    Ask the agent to raise a dialog, answer it over the API the way a phone
    does, and require that THE AGENT ACTED ON THE ANSWER.

    That last clause is the whole point. A menu appearing proves capture. A tap
    returning `ok:true` proves relay — the API answers before the runner has
    touched anything. Neither says the keystroke reached the terminal. Only the
    agent using the answer says that, and nothing had ever checked it.
    """
    colour = "Aubergine"          # not a word an agent reaches for unprompted
    ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/send", {
        "text": ("Use the AskUserQuestion tool right now to ask me one question: "
                 f"'Pick a colour' with exactly two options, '{colour}' and 'Beige'. "
                 "Ask nothing else and do nothing else first. After I answer, reply "
                 "with exactly: PICKED=<the option I chose>"),
        "client_id": str(uuid.uuid4()),
    })

    def dialog():
        menu = session(ctx).get("menu")
        return menu if menu and menu.get("options") else None

    menu = wait_for("the dialog to reach the API", dialog, timeout=ctx["timeout"])
    labels = [o["label"] for o in menu["options"]]
    if colour not in labels:
        raise Failure(f"dialog arrived without the declared options: {labels}")
    if not menu.get("observed_at"):
        raise Failure("dialog carries no observed_at, so nothing can tell how stale it is")
    number = next(o["number"] for o in menu["options"] if o["label"] == colour)

    reply = ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/answer-menu",
                       {"option": number})
    if not reply.get("ok"):
        raise Failure(f"the API would not even relay the tap: {reply.get('reason')!r}")

    # The menu clearing means the runner pressed the key. A refusal comes back on
    # the same object, so this tells the two apart instead of reading silence as
    # success — the exact confusion that made a dead button look like a working
    # one for 45 minutes.
    def pressed():
        sess = session(ctx)
        menu_now = sess.get("menu")
        if menu_now and menu_now.get("answer_error"):
            raise Failure(f"the runner could not press the key: {menu_now['answer_error']} "
                          f"({menu_now.get('answer_note')})")
        return sess if not menu_now else None

    wait_for("the dialog to clear, meaning the key landed", pressed, timeout=ctx["timeout"])

    # And the agent must have RECEIVED it. Without this the step proves only that
    # a dialog went away.
    wait_for(f"the agent to report PICKED={colour}",
             lambda: any(f"PICKED={colour}" in t for t in assistant_texts(session(ctx))),
             timeout=ctx["timeout"])
    return f"asked · answered option {number} ({colour}) from the web · agent replied PICKED={colour}"


def scroll_back(ctx):
    """`turn_index` is both the sort order and the paging cursor, and its scheme
    changed once already. A conversation that renders shuffled is what this
    catches."""
    sess = session(ctx)
    rows = sess.get("messages") or []
    if not rows:
        raise Failure("no messages on a session that has been talking")
    indexes = [m["turn_index"] for m in rows]
    if indexes != sorted(indexes):
        raise Failure(f"transcript is not ordered by turn_index: {indexes}")
    if len(set(indexes)) != len(indexes):
        raise Failure(f"duplicate turn_index values: {indexes}")
    oldest = sess.get("oldest_loaded_turn_index")
    if oldest is not None:
        page = ctx["api"]("GET", f"/canopy-sessions/{ctx['session_id']}/messages?before={oldest}")
        earlier = [m["turn_index"] for m in (page.get("messages") or [])]
        if any(i >= oldest for i in earlier):
            raise Failure(f"scroll-back returned rows at/after the cursor {oldest}: {earlier}")
    return f"{len(rows)} rows, ordered, cursor={oldest}"


def rebuild_from_transcript(ctx):
    """These rows are a CACHE of a file on the runner's disk, so a reload has to
    be a pure re-derivation. If the conversation survives a reset, the cache is
    honest about what it is."""
    result = ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/reset")
    if not result.get("ok"):
        raise Failure(f"reset refused: {result.get('reason')!r}")
    wait_for("the conversation to be re-derived from the transcript",
             lambda: any(ctx["marker"] in t for t in assistant_texts(session(ctx))),
             timeout=ctx["timeout"])
    return "the conversation came back from the runner's transcript"


def close_session(ctx):
    result = ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/close")
    if not result.get("ok") and result.get("reason") != "already_closed":
        raise Failure(f"close refused: {result.get('reason')!r}")
    # Not instant: the runner deletes the emdash task and that rides the next
    # report.
    wait_for("the session to retire",
             lambda: session(ctx).get("status") == "archived", timeout=240)
    ctx["closed"] = True
    return "session archived"


STEPS = {f.__name__: f for f in (
    check_runner, create_session, send_and_reply, answer_from_the_web,
    scroll_back, rebuild_from_transcript, close_session)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=DEFAULT_BASE)
    p.add_argument("--token-file", default=str(DEFAULT_TOKEN_FILE))
    p.add_argument("--token", default="")
    p.add_argument("--agent", default="hal", help="agent slug to chat with")
    p.add_argument("--workspace", default="", help="tenant; defaults to the agent's own")
    p.add_argument("--runner", default="",
                   help="runner id to pin to; defaults to the one on THIS machine")
    p.add_argument("--steps", default=",".join(ALL_STEPS))
    p.add_argument("--timeout", type=int, default=TURN_TIMEOUT)
    p.add_argument("--keep", action="store_true", help="leave the session open")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    runner_id = args.runner
    if not runner_id:
        if not RUNNER_CONFIG.exists():
            print(f"no {RUNNER_CONFIG}; pass --runner to say which box to pin to",
                  file=sys.stderr)
            return 2
        runner_id = json.loads(RUNNER_CONFIG.read_text())["runner_id"]

    steps = [s.strip() for s in args.steps.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        print(f"unknown step(s) {unknown}; known: {list(STEPS)}", file=sys.stderr)
        return 2
    if [s for s in steps if s not in ("check_runner", "create_session")] \
            and "create_session" not in steps:
        print("steps after create_session need it too — add it to --steps", file=sys.stderr)
        return 2
    if "create_session" in steps and "check_runner" not in steps:
        print("create_session needs check_runner (it resolves the workspace)", file=sys.stderr)
        return 2

    ctx = {
        "api": Client(args.base_url, args.token or Path(args.token_file).read_text().strip(),
                      args.verbose),
        "agent": args.agent, "workspace": args.workspace, "runner_id": runner_id,
        "runner_name": runner_id[:8], "nonce": uuid.uuid4().hex[:8],
        "timeout": args.timeout, "session_id": None, "closed": False,
    }

    print(f"canopy session-chat e2e · {args.base_url}")
    print(f"agent={args.agent} · pinned runner={runner_id} · nonce={ctx['nonce']}")
    print("this spawns a REAL agent session and spends tokens.\n")

    failed = None
    for name in steps:
        started = time.time()
        try:
            detail = STEPS[name](ctx)
            print(f"  PASS  {name:<24}{time.time()-started:7.1f}s  {detail}")
        except Failure as exc:
            print(f"  FAIL  {name:<24}{time.time()-started:7.1f}s  {exc}")
            failed = name
            break
        except KeyboardInterrupt:
            print(f"\n  ABORTED during {name}")
            failed = name
            break

    # Always try to clean up, including after a failure: a leaked session holds a
    # worktree and a live agent process on somebody's laptop.
    if ctx["session_id"] and not ctx["closed"] and not args.keep:
        try:
            ctx["api"]("POST", f"/canopy-sessions/{ctx['session_id']}/close")
            print(f"\ncleaned up session {ctx['session_id'][:8]}")
        except Failure as exc:
            print(f"\ncould not clean up session {ctx['session_id']}: {exc}")
    elif ctx["session_id"] and args.keep:
        # `--keep` skips the cleanup close; it does NOT un-run close_session if
        # you asked for that step. Saying "left open" about a session the run
        # just archived is the same class of lie this whole file exists to catch.
        state = "archived by close_session" if ctx["closed"] else "left open"
        print(f"\nsession {ctx['session_id']} — {state} (--keep)")

    print("\nRESULT:", f"FAILED at {failed}" if failed else "all steps passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
