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

# What canopy records as the program behind anything this script creates
# (X-Canopy-Client, User-Agent, X-Canopy-Parent-* from the environment).
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import script_provenance  # noqa: E402

SCRIPT = "dispatch_one_continue.py"
NONCE = os.urandom(4).hex()
URL = os.environ.get("CANOPY_URL", "").rstrip("/")
PAT = os.environ.get("CANOPY_PAT", "")


def _post(path: str, body: dict) -> tuple[int, object]:
    r = urllib.request.Request(f"{URL}{path}", data=json.dumps(body).encode(), method="POST")
    r.add_header("Authorization", f"Bearer {PAT}")
    r.add_header("Content-Type", "application/json")
    for k, v in script_provenance.headers(SCRIPT, NONCE).items():
        r.add_header(k, v)
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
        "prompt": args.prompt,
        "origin_ref": {"thread_key": args.thread, "e2e": {"script": SCRIPT, "nonce": NONCE}},
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
