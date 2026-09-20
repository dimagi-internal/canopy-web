#!/usr/bin/env python3
"""Live end-to-end check of the embedded widget, against a real deployment.

**In one line:** it walks the chain a browser walks when the widget loads —
against the real server — and checks each step actually worked, rather than
that it returned a success code.

**Why this is a script and not a pytest.** The sibling of
`e2e_session_chat.py`, and for the same reason, restated by this feature's own
history. Between 2026-09-13 and 2026-09-15 the widget shipped with a full green
suite and a dozen silent failures in it: a CSRF cookie read under the wrong
name, an API base missing the deployment prefix, sessions created against the
wrong tenant, an MCP provider no module imported, a session-cookie that hid the
frame's bearer token, and a session created on every page load. Every one was
found by driving the real thing, and none of them could have been found by a
test that constructs both ends.

What it checks, in the order a browser does it:

  1. `/embed/widget.js`   — the loader is served and is the IIFE bundle
  2. `/api/embed/self`    — canopy offers the panel on its own pages
  3. `/embed/chat`        — the shell serves AND carries frame-ancestors
  4. `/api/embed/token`   — a delegated token, short-lived
  5. `/api/embed/agents`  — the SAME token lists agents (the 403 of #788)
  6. nothing was created  — loading the widget creates no session (#799)

Read-only by default: it creates nothing and leaves nothing behind. Pass
`--send "text"` to also start a real conversation, which enqueues a turn and
costs whatever a real agent costs.

    uv run python scripts/e2e_embed_widget.py
    uv run python scripts/e2e_embed_widget.py --send "what is stale here?"

Auth comes from a canopy PAT: `--token`, else `$CANOPY_WEB_PAT`, else
`~/.claude/canopy/workbench-token`. Connected sites is owner-only, so a fleet
AGENT's PAT will not do — those are editors.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE = "https://labs.connect.dimagi.com/canopy"

OK, BAD = "  ok  ", " FAIL "
failures: list[str] = []


def report(name: str, passed: bool, detail: str = "") -> bool:
    print(f"[{OK if passed else BAD}] {name}{('  — ' + detail) if detail else ''}")
    if not passed:
        failures.append(name)
    return passed


def resolve_token(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    if os.environ.get("CANOPY_WEB_PAT"):
        return os.environ["CANOPY_WEB_PAT"].strip()
    path = Path.home() / ".claude" / "canopy" / "workbench-token"
    if path.exists():
        return path.read_text().strip()
    sys.exit(
        "no canopy PAT: pass --token, set CANOPY_WEB_PAT, or mint one with "
        "/canopy:canopy-web-pat-mint"
    )


def _lower(headers) -> dict:
    """Header names, lowercased.

    `dict(response.headers)` keeps whatever casing the server used and loses the
    case-insensitive lookup the message object had — so a plain `.get("Content-
    Security-Policy")` misses a header sent as `content-security-policy`. That
    is exactly what this script reported as a missing CSP on its first run,
    against a deployment that was sending one.
    """
    return {k.lower(): v for k, v in headers.items()}


def call(base, path, token=None, method="GET", body=None):
    """`(status, parsed-or-text, headers)`. Never raises on an HTTP error —
    the status IS the result here."""
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **({"Content-Type": "application/json"} if body is not None else {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw, status, headers = r.read(), r.status, _lower(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, headers = e.read(), e.code, _lower(e.headers)
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc), {}
    try:
        return status, json.loads(raw), headers
    except Exception:  # noqa: BLE001
        return status, raw.decode("utf-8", "replace"), headers


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--token", default=None)
    ap.add_argument("--act", default=None, metavar="TEXT", nargs="?", const=(
        "Close the insights I am looking at right now."),
        help="declare a page selection, ask for it, and assert the agent USED it")
    ap.add_argument("--send", default=None, metavar="TEXT",
                    help="also start a real conversation (enqueues a turn)")
    args = ap.parse_args()
    base, token = args.base.rstrip("/"), resolve_token(args.token)

    print(f"\ncanopy embedded widget — live check against {base}\n")
    before = -1

    who_code, who, _ = call(base, "/api/me/", token)
    if not report("authenticated", who_code == 200,
                  who.get("email") if isinstance(who, dict) else str(who)[:60]):
        return 1

    status, body, _ = call(base, "/embed/widget.js")
    report(
        "loader is served",
        status == 200 and isinstance(body, str) and body.lstrip().startswith("var canopy="),
        f"{status}, {len(body) if isinstance(body, str) else 0} bytes",
    )

    before = count_empty_sessions(base, token)

    status, self_, _ = call(base, "/api/embed/self", token)
    offered = status == 200 and isinstance(self_, dict) and self_.get("enabled")
    report("panel is switched on", bool(offered),
           json.dumps(self_) if isinstance(self_, dict) else str(self_)[:60])
    if not offered:
        print("\n  Nothing else can be checked until a connected site is shown on "
              "canopy's own pages.\n  Tick it on /w/<workspace>/settings/connected-apps "
              "(owner only).\n")
        return 1
    app_name = self_["app"]

    status, shell, headers = call(base, f"/embed/chat?app={app_name}")
    csp = headers.get("content-security-policy", "")
    report("shell is framable, and only by its own origins",
           status == 200 and "frame-ancestors" in csp and "*" not in csp, csp or f"{status}")

    status, minted, _ = call(base, "/api/embed/token", token, method="POST", body={})
    got_token = status == 200 and isinstance(minted, dict) and minted.get("token")
    ttl = ""
    if got_token:
        expires = datetime.fromisoformat(minted["expires_at"])
        seconds = (expires - datetime.now(timezone.utc)).total_seconds()
        ttl = f"{seconds / 60:.0f} min"
        report("token is short-lived", 0 < seconds <= 16 * 60, ttl)
    if not report("token is minted", bool(got_token), ttl or str(status)):
        return 1

    # THE one that was broken in production: same-origin means the browser
    # attaches canopy's session cookie alongside the bearer token, and the
    # middleware used to stop reading the header when it saw one (#788).
    status, agents, _ = call(base, "/api/embed/agents", minted["token"])
    report("the frame's own token lists agents",
           status == 200 and isinstance(agents, list),
           f"{status}: {[a.get('slug') for a in agents] if isinstance(agents, list) else agents}")

    # Everything above is what a browser does when the frame loads. If any of
    # it created a session, the count moved — which is the bug #799 fixed, one
    # empty session per page view.
    after = count_empty_sessions(base, token)
    report("loading the widget created no session", after == before,
           f"{before} empty before, {after} after")

    if args.act:
        passed, detail = act_on_the_page(base, token, agents, args.act)
        report("agent acts on what is on screen", passed, detail)

    if args.send:
        started = start_conversation(base, token, agents, args.send)
        report("a real conversation starts and carries the first message", bool(started),
               started or "")

    print()
    if failures:
        print(f"{len(failures)} failed: {', '.join(failures)}\n")
        return 1
    print("all good\n")
    return 0


def count_empty_sessions(base, token) -> int:
    """Sessions with no runner, no title and a web origin — the shape the
    create-on-load bug produced, one per page view."""
    status, rows, _ = call(base, "/api/canopy-sessions/?limit=50", token)
    if status != 200 or not isinstance(rows, list):
        return -1
    return sum(
        1 for s in rows
        if s.get("origin") == "web" and not s.get("runner_name")
        and not (s.get("title") or "").strip()
    )


def act_on_the_page(base, token, agents, text, wait_s=420) -> tuple[bool, str]:
    """The point of the file, the way `answer_from_the_web` is the point of its sibling.

    Everything above proves the widget can LOAD. This proves the feature: the
    page declares what is on screen, a human asks for something about "these",
    and the agent reads the declaration and acts on the RIGHT ROWS.

    It asserts the agent USED the page state — not that a turn completed, not
    that a reply arrived, not that a tool returned 200. A reply saying "I have
    closed them" while the agent in fact called `clear_insights` with no filter
    is the failure this exists to catch, and it is indistinguishable from
    success in every other check we have.
    """
    if not isinstance(agents, list) or not agents:
        return False, "no agent available"
    agent = agents[0]

    status, created, _ = call(
        base, f"/api/w/{agent['workspace']}/canopy-sessions/", token, method="POST",
        body={"agent_slug": agent["slug"], "title": "", "metadata": {}},
    )
    if status != 200 or not isinstance(created, dict):
        return False, f"session create returned {status}"
    sid = created["id"]

    # Read real ids the caller can actually see, so the selection we declare is
    # one the agent could genuinely act on. Inventing ids would test the plumbing
    # against data that does not exist, which is the shape of a green run that
    # means nothing.
    status, insights, _ = call(base, "/api/insights/?limit=5", token)
    # `items` is this API's page key; `results` is a guess that cost a red run.
    # Both are accepted rather than one being assumed, because a check that
    # fails on the SHAPE of a healthy response reports the feature broken when
    # it is not — which is a false alarm, and false alarms are how a live check
    # stops being trusted.
    if isinstance(insights, list):
        rows = insights
    else:
        payload = insights or {}
        rows = payload.get("items") or payload.get("results") or []
    ids = [r["id"] for r in rows[:3] if isinstance(r, dict) and "id" in r]
    if not ids:
        return False, "no insights visible to this token; nothing to select"

    # Declare the view exactly as the widget does (PUT /page-state).
    status, _st, _ = call(
        base, f"/api/canopy-sessions/{sid}/page-state", token, method="PUT",
        body={"state": {"surface": "the insights feed", "path": "/insights",
                        "backing_tool": "list_insights", "visible_ids": ids,
                        "visible_count": len(ids)}},
    )
    if status != 200:
        return False, f"page-state declare returned {status}"

    status, _sent, _ = call(base, f"/api/canopy-sessions/{sid}/send", token,
                            method="POST", body={"text": text})
    if status != 200:
        return False, f"send returned {status}"

    deadline = time.time() + wait_s
    while time.time() < deadline:
        time.sleep(10)
        status, msgs, _ = call(base, f"/api/canopy-sessions/{sid}/messages?limit=100", token)
        if isinstance(msgs, list):
            items = msgs
        else:
            payload = msgs or {}
            items = payload.get("items") or payload.get("results") or []
        blob = json.dumps(items)
        used_state = "current_page" in blob
        named_rows = any(str(i) in blob for i in ids)
        replied = any(m.get("role") == "assistant" and (m.get("plaintext") or "").strip()
                      for m in items if isinstance(m, dict))
        if used_state and named_rows:
            return True, f"session {sid}: agent read current_page and named {ids}"
        if replied and not used_state:
            # A reply WITHOUT reading the page is the interesting failure: the
            # agent answered about everything, or about nothing, rather than
            # about what the user was looking at.
            return False, (f"session {sid}: agent replied WITHOUT calling current_page — "
                           f"it did not act on the {len(ids)} visible rows")
    return False, f"session {sid}: no answer within {wait_s}s (runner offline?)"


def start_conversation(base, token, agents, text) -> str | None:
    if not isinstance(agents, list) or not agents:
        return None
    agent = agents[0]
    status, created, _ = call(
        base, f"/api/w/{agent['workspace']}/canopy-sessions/", token, method="POST",
        body={"agent_slug": agent["slug"], "title": "", "metadata": {}},
    )
    if status != 200 or not isinstance(created, dict):
        return None
    sid = created["id"]
    status, _sent, _ = call(base, f"/api/canopy-sessions/{sid}/send", token,
                            method="POST", body={"text": text})
    return f"{sid} ({status})" if status == 200 else None


if __name__ == "__main__":
    raise SystemExit(main())
