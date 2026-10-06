"""Headers every canopy-web script sends, so what it creates says it was a script.

The incident behind this (2026-10-05): a scratch script in one Claude session
posted chat messages to an agent on a person's PAT, and the turns it made were
recorded exactly like that person typing in the web UI. canopy-web now records
the program (`X-Canopy-Client`), its user agent, and the session it ran inside
(`X-Canopy-Parent-*`) on every turn and session — but only if the caller says.
Scripts in this repo say.

Also: a script must be HANDED a token (`--token` or `CANOPY_E2E_TOKEN`). It used
to default to `~/.claude/canopy/workbench-token` — the operator's own PAT — so
anything it did was indistinguishable from them, and an agent running it acted
as the human without anyone deciding it should.
"""
from __future__ import annotations

import os
import sys
import uuid

#: env var (exported by the runners into every session they launch) -> header.
PARENT_ENV = {
    "CANOPY_TURN_ID": "X-Canopy-Parent-Turn",
    "CANOPY_SESSION_ID": "X-Canopy-Parent-Session",
    "CANOPY_EMDASH_TASK": "X-Canopy-Parent-Task",
    "CANOPY_HOST": "X-Canopy-Parent-Host",
    "CLAUDE_SESSION_ID": "X-Canopy-Claude-Session",
}
TOKEN_ENV = "CANOPY_E2E_TOKEN"


def headers(script: str, nonce: str = "") -> dict[str, str]:
    """`X-Canopy-Client`, a User-Agent naming the script, an `X-Request-Id`
    carrying the run's nonce, and the parent headers from the environment."""
    out = {
        "X-Canopy-Client": script,
        "User-Agent": f"{script} (canopy-web script; python {sys.version.split()[0]})",
        "X-Request-Id": f"{script.replace('.py', '')}-{nonce or 'run'}-{uuid.uuid4().hex[:8]}",
    }
    for env, header in PARENT_ENV.items():
        value = os.environ.get(env, "").strip()
        if value:
            out[header] = value
    return out


def require_token(cli_value: str = "") -> str:
    """The token to use: `--token`, else $CANOPY_E2E_TOKEN — never a file default."""
    token = (cli_value or os.environ.get(TOKEN_ENV, "")).strip()
    if not token:
        raise SystemExit(
            f"no token: pass --token or set {TOKEN_ENV}. (Scripts no longer default to "
            "~/.claude/canopy/workbench-token — that is a person's own PAT.)")
    return token
