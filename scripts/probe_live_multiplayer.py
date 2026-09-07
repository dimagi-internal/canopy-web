"""Live multiplayer probe: two REAL identities, one session, on the deployed site.

The e2e suite proves the mechanism against a local server with seeded users.
This proves the same thing where it actually has to work — labs — using two
credentials that exist independently of any test fixture: a human's workbench
PAT and ACE's own agent PAT.

Reports, rather than asserts, so it is equally useful before and after a deploy:
run it now and `presence.joined` carries only a user_id (the bug), run it after
#692 and it carries the participant.

    uv run python scripts/probe_live_multiplayer.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("CANOPY_BASE", "https://labs.connect.dimagi.com/canopy")
WS_BASE = BASE.replace("https://", "wss://").replace("http://", "ws://")


def _read(path: str) -> str:
    with open(os.path.expanduser(path)) as fh:
        return fh.read().strip()


def _ace_pat() -> str:
    env = os.path.expanduser("~/.claude/plugins/data/ace-ace/.env")
    with open(env) as fh:
        for line in fh:
            if line.startswith("CANOPY_WEB_PAT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("no CANOPY_WEB_PAT for ace")


def api(method: str, path: str, token: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as exc:
        # The body carries the actual reason; a bare traceback hides it.
        raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:400]}") from None


async def main() -> int:
    try:
        import websockets
    except ImportError:
        print("needs `websockets` (uv run --with websockets python ...)")
        return 2

    human = _read("~/.claude/canopy/workbench-token")
    ace = _ace_pat()


    # Tenancy rides the PATH (apps/api/tenancy.WorkspaceResolveMiddleware), not a
    # header or a body field. The flat mount resolves the caller's SOLE
    # membership and 422s for anyone in more than one workspace.
    status, session = api("POST", "/api/w/connect/canopy-sessions/", human,
                          {"agent_slug": "ace", "title": "live multiplayer probe"})
    print(f"created session -> {status} {session.get('id') if session else None}")
    sid = session["id"]
    url = f"{WS_BASE}/ws/canopy-sessions/{sid}/"

    findings: list[str] = []
    try:
        async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {human}"}) as a:
            snap = json.loads(await asyncio.wait_for(a.recv(), 20))
            print(f"[human] first frame: {snap.get('event')}")
            parts = snap.get("data", {}).get("participants", [])
            print(f"[human] participants at connect: {[p.get('display_name') for p in parts]}")

            # Drain the human's own presence.joined so the next one is ACE's.
            await asyncio.wait_for(a.recv(), 20)

            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {ace}"}) as b:
                await asyncio.wait_for(b.recv(), 20)  # ace's snapshot

                joined = None
                for _ in range(8):
                    frame = json.loads(await asyncio.wait_for(a.recv(), 20))
                    if frame.get("event") == "presence.joined":
                        joined = frame
                        break
                print(f"[human] saw: {json.dumps(joined)}")

                if not joined:
                    findings.append("FAIL  the human never saw ACE join at all")
                elif joined["data"].get("participant"):
                    findings.append("PASS  presence.joined carries the participant — a newcomer is renderable")
                else:
                    findings.append(
                        "BUG   presence.joined carries only a user_id. Everyone already in "
                        "the room filters participants by presence, so ACE is INVISIBLE to "
                        "them until they reload. (This is what #692 fixes.)")

                # Co-edited draft: ACE types, the human must see it.
                await b.send(json.dumps({"action": "draft.update",
                                         "data": {"body": "typed by ACE on the live site"}}))
                seen = None
                for _ in range(8):
                    frame = json.loads(await asyncio.wait_for(a.recv(), 20))
                    if "draft" in (frame.get("event") or ""):
                        seen = frame
                        break
                # `draft.updated` carries the draft FLAT in `data` — there is no
                # nested `draft` key. Reading one produced a false FAIL against a
                # frame that was plainly correct.
                d = (seen or {}).get("data") or {}
                body = d.get("body") if "body" in d else (d.get("draft") or {}).get("body")
                findings.append(
                    f"PASS  the human sees ACE's draft live: {body!r}" if body
                    else f"FAIL  the human never received ACE's draft (last: {json.dumps(seen)})")
    finally:
        try:
            api("POST", f"/api/w/connect/canopy-sessions/{sid}/archive", human, {})
            print(f"archived probe session {sid}")
        except Exception as exc:  # noqa: BLE001 — cleanup must never mask a finding
            print(f"(could not clean up {sid}: {exc})")

    print("\n--- findings ---")
    for f in findings:
        print(" ", f)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
