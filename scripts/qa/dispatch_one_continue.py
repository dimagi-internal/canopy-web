"""Dispatch exactly one Continue/turn against a live server and print its id.

The human then runs `python -m canopy_runner.main --drain-one --config …` and
eyeballs emdash. This helper never touches emdash — it stops at the dispatch.

Run:
    CANOPY_PAT=<raw> CANOPY_URL=<url> uv run python scripts/qa/dispatch_one_continue.py \
      --project canopy-web --thread emdash:cloud-runner --prompt "add a comment to the header" \
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


def _provenance_headers(client: str) -> dict:
    """What this script is (`X-Canopy-Client`), and — when run inside a canopy
    turn — which turn/session it runs in (canopy#768 env names), so the turns it
    creates are findable in canopy-web's TURN_CREATED log and `provenance`."""
    headers = {"X-Canopy-Client": client, "User-Agent": f"{client}/1 (canopy-web scripts/qa)"}
    for env, header in (("CANOPY_TURN_ID", "X-Canopy-Parent-Turn"),
                        ("CANOPY_SESSION_ID", "X-Canopy-Parent-Session"),
                        ("CANOPY_EMDASH_TASK", "X-Canopy-Parent-Task")):
        if os.environ.get(env, "").strip():
            headers[header] = os.environ[env].strip()
    return headers


def _post(path: str, body: dict) -> tuple[int, object]:
    r = urllib.request.Request(f"{URL}{path}", data=json.dumps(body).encode(), method="POST")
    for key, value in _provenance_headers("dispatch_one_continue").items():
        r.add_header(key, value)
    r.add_header("Authorization", f"Bearer {PAT}")
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
        "prompt": args.prompt, "origin_ref": {"thread_key": args.thread, "e2e": "dispatch_one_continue"},
    }
    if args.agent:
        body["agent_slug"] = args.agent
        path = "/api/harness/turns/"
    else:
        body["project"] = args.project
        path = f"/api/w/{args.workspace}/harness/turns/" if args.workspace else "/api/harness/turns/"

    st, turn = _post(path, body)
    if st in (200, 201) and isinstance(turn, dict):
        print(f"dispatched turn {turn['id']} (status={turn.get('status')}).")
        print("Now run:  python -m canopy_runner.main --drain-one --config ~/.canopy/runner.json")
        return 0
    print(f"dispatch failed: HTTP {st} {turn}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
