"""Runner config: one JSON file, stdlib only."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    base_url: str
    token: str
    runner_id: str
    emdash_db: str
    # Claim cadence — how fast a queued turn (e.g. a phone "Continue this session")
    # gets picked up. Kept low so delivery feels near-immediate; the claim + heartbeat
    # are cheap HTTP. The heavier session report is throttled separately below.
    poll_seconds: int = 5
    heartbeat_seconds: int = 30
    # Session report throttle: reading up to session_tail_count transcripts is the one
    # expensive thing per tick, so do it at most this often even as poll_seconds drops.
    # Kept modest so a message you send (and the reply) show up in the phone's tail
    # within ~10s; the bounded tail-read keeps even ~30 transcripts/10s cheap.
    session_report_seconds: int = 10
    # How many emdash tasks the report carries. DISTINCT from session_tail_count
    # below (which bounds the expensive transcript reads): a task truncated off THIS
    # limit stops being reported at all, and after SESSION_STALE_AFTER the server
    # auto-archives it. Silent truncation is therefore not cosmetic — keep it well
    # above any realistic open-task count.
    session_report_limit: int = 100
    state_path: str = ""
    # The runner drives emdash's real UI over CDP (create/reuse sessions); it needs
    # emdash launched with --remote-debugging-port=<cdp_port> (see "Emdash CDP").
    cdp_port: int = 9222
    inbox_poll_seconds: int = 300
    # {agent_slug: {"account": "<mailbox>", "client": "<gog client>", "query": "<opt>"}}
    # — the deterministic email trigger polls these and enqueues email-origin turns.
    # Per-mailbox "query" overrides the default Gmail search (e.g. restrict to certain
    # senders/labels) so junk never becomes a turn (= a session = tokens).
    mailboxes: dict = field(default_factory=dict)
    # Hard safety cap: at most this many threads become turns per mailbox per poll,
    # so a flooded/misconfigured inbox can't spawn dozens of sessions at once.
    inbox_max_threads: int = 8
    # Phase B: the recent-message tail attached to reported sessions. `_limit` caps
    # messages per session (a "recent tail", not the full transcript); `_count` caps
    # how many of the top (most-recently-active) sessions get a tail each tick.
    session_tail_limit: int = 8
    # Cover every reported session (report cap is 30), not just the most-recent few,
    # so a project-grouped list doesn't show tails on some rows and not others. The
    # bounded tail-read (TAIL_BYTES) keeps ~30 transcript reads/tick cheap.
    session_tail_count: int = 30

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(Path(path).read_text())
        token = raw["token"]
        if token.startswith("@"):
            token = Path(token[1:]).expanduser().read_text().strip()
        known = {f for f in cls.__dataclass_fields__}
        kwargs = {k: v for k, v in raw.items() if k in known}
        kwargs["token"] = token
        cfg = cls(**kwargs)
        if not cfg.state_path:
            cfg.state_path = str(Path(path).with_name("runner-state.json"))
        return cfg
