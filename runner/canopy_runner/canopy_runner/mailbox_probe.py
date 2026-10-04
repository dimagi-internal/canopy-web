"""Which mailboxes this box can READ — discovered, not configured.

Reading an agent's inbox used to need a hand-kept ``mailboxes`` map in
~/.canopy/runner.json. The owner did not know it existed, pairing wrote ``{}``,
and a box with ``{}`` was still rung by the push doorbell for every mail and
silently ignored each ring (canopy-web#1087). So the runner now asks the only
authority there is — the token:

  1. candidates: every mailbox canopy-web serves this caller
     (``GET /api/inbound/runner-mailboxes`` — address + agent slug);
  2. which gog clients hold a token for each: ``gog auth tokens list --json``,
     whose keys are literally ``token:<client>:<mailbox>`` (no network);
  3. proof: one ``gog gmail search in:inbox --max 1`` per mailbox, trying those
     clients in order. The first that answers is the client to read with.

The result is the in-memory mailbox map the inbox trigger and the watch re-arm
use INSTEAD of ``cfg.mailboxes``, and the readable address list the heartbeat
reports so the doorbell rings only boxes that can actually look. A non-empty
``runner.json`` entry still wins for its agent — an explicit override — so no
existing box changes behaviour by upgrading.

Mirrors canopy's ``agent_email.token_pairs`` (the runner cannot import canopy)
and the cloud runner's ``_resolve_mailbox_clients``. Module state, like
``inbox_due``: one probe result per process, refreshed on a timer.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time

logger = logging.getLogger("canopy_runner")

#: How soon to retry after the candidate list could not be fetched at all. Much
#: shorter than the probe interval: at startup the alternative is reporting
#: "unknown" for ten minutes because canopy-web blinked once.
RETRY_SECONDS = 60

#: Tried first when several clients hold a token for one mailbox: the fleet's
#: shared OAuth app. The agent's own slug comes next (the older per-agent apps).
PREFERRED_CLIENT = "canopy"

_state: dict = {"next_at": 0.0, "discovered": {}, "readable": None}


def reset() -> None:
    """Forget the last probe (startup, and between tests)."""
    _state.update(next_at=0.0, discovered={}, readable=None)


def readable() -> list[str] | None:
    """The addresses the last probe proved readable, or None before the first one.

    None is what the heartbeat must send until there is an answer: the server reads
    it as "not reported" and keeps ringing this box, where [] would stop it.
    """
    r = _state["readable"]
    return None if r is None else list(r)


def effective_mailboxes(cfg) -> dict:
    """``{agent_slug: {"account", "client", ["query"]}}`` — what to read this tick.

    The discovered map, with every NON-EMPTY ``runner.json`` entry laid over it:
    an explicit entry is an override for its agent (a custom query, a pinned
    client), so it wins. An empty entry overrides nothing.
    """
    return {**_state["discovered"], **_overrides(cfg)}


def _overrides(cfg) -> dict:
    """The runner.json ``mailboxes`` entries that actually name a mailbox."""
    return {slug: box for slug, box in (getattr(cfg, "mailboxes", None) or {}).items()
            if isinstance(box, dict) and box.get("account")}


def token_pairs(*, runner=subprocess.run) -> tuple[set[tuple[str, str]] | None, str]:
    """Every stored ``(client, mailbox)`` pair, and why not when it cannot be read.

    Returns ``(None, reason)`` when the token store cannot be listed — gog missing,
    a keyring that wants a TTY, unparseable output. The caller reports every
    candidate as NOT readable then: a box that cannot open its own keyring cannot
    read mail either, and saying "unknown" would keep it being rung for nothing.
    """
    try:
        r = runner(["gog", "auth", "tokens", "list", "--json"],
                   capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None, "gog not installed"
    except subprocess.TimeoutExpired:
        return None, "gog auth tokens list timed out (keyring prompt?)"
    if r.returncode != 0:
        return None, (r.stderr or "").strip()[:200] or f"gog exited {r.returncode}"
    try:
        keys = json.loads(r.stdout or "{}").get("keys", [])
    except (ValueError, TypeError, AttributeError):
        return None, "non-JSON from gog auth tokens list"
    pairs: set[tuple[str, str]] = set()
    for key in keys or []:
        # `token:<client>:<mailbox>` — split from the left, exactly twice: a mailbox
        # never contains a colon, so anything after the second one is the mailbox.
        parts = str(key).split(":", 2)
        if len(parts) == 3 and parts[0] == "token" and parts[1] and parts[2]:
            pairs.add((parts[1], parts[2].strip().lower()))
    return pairs, ""


#: Interchangeable with PREFERRED_CLIENT: canopy-web's "Connect Google mailbox"
#: button mints under it (a Web client — the only kind a browser sign-in can use).
FLEET_CLIENTS = (PREFERRED_CLIENT, "canopy-web")


def _client_order(clients: set[str], slug: str) -> list[str]:
    """Search order for the clients holding one mailbox's token: the fleet's two
    clients first, then a legacy client named after the agent, then the rest."""
    head = [c for c in (*FLEET_CLIENTS, slug) if c in clients]
    return head + sorted(c for c in clients if c not in head)


def probe(candidates: list[dict], overrides: dict, *, runner=subprocess.run) -> tuple[dict, list[str]]:
    """Find which candidates this box can read. Returns ``(discovered, readable)``.

    ``candidates`` are runner-mailboxes rows (``address``, ``agent_slug``);
    ``overrides`` are the non-empty runner.json entries, probed with their own
    pinned client so the readable list covers them too. Never raises: one line
    per mailbox says what was found.
    """
    from .inbox import search_threads

    pairs, why = token_pairs(runner=runner)
    discovered: dict = {}
    readable: set[str] = set()

    def confirm(address: str, client: str) -> str:
        """Empty when ``client`` reads ``address``, else the error."""
        try:
            search_threads(address, client, "in:inbox", 1, runner=runner)
            return ""
        except Exception as exc:  # noqa: BLE001 — a failed probe is an answer, not a crash
            return str(exc)[:200] or type(exc).__name__

    for slug, box in overrides.items():
        address = (box.get("account") or "").strip().lower()
        client = (box.get("client") or "").strip() or PREFERRED_CLIENT
        err = confirm(address, client)
        if err:
            logger.info("mailbox %s: runner.json override, but search failed: %s", address, err)
        else:
            readable.add(address)
            logger.info("mailbox %s: readable via client %s (runner.json override)",
                        address, client)

    for row in candidates:
        address = (row.get("address") or "").strip().lower()
        slug = (row.get("agent_slug") or "").strip()
        if not address or not slug or slug in overrides:
            continue
        if pairs is None:
            logger.info("mailbox %s: not readable — token store unreadable: %s", address, why)
            continue
        clients = {c for c, m in pairs if m == address}
        if not clients:
            logger.info("mailbox %s: no token", address)
            continue
        errors = []
        for client in _client_order(clients, slug):
            err = confirm(address, client)
            if not err:
                discovered[slug] = {"account": address, "client": client}
                readable.add(address)
                logger.info("mailbox %s: readable via client %s", address, client)
                break
            errors.append(f"{client}: {err}")
        else:
            logger.info("mailbox %s: token but search failed: %s", address, "; ".join(errors))
    return discovered, sorted(readable)


def maybe_probe(cfg, client, *, now_fn=time.time, runner=subprocess.run) -> bool:
    """Re-probe when due (at startup, then every ``mailbox_probe_seconds``).

    Returns whether a probe ran. A candidate list that cannot be fetched keeps
    the last result and retries soon; it never blanks a map that was working.
    """
    now = now_fn()
    if now < _state["next_at"]:
        return False
    try:
        candidates = client.runner_mailboxes()
    except Exception as exc:  # noqa: BLE001 — a config read never breaks the tick
        logger.warning("mailbox probe: could not list candidate mailboxes (%s); "
                       "retrying in %ss", exc, RETRY_SECONDS)
        _state["next_at"] = now + RETRY_SECONDS
        return False
    discovered, found = probe(candidates, _overrides(cfg), runner=runner)
    _state.update(discovered=discovered, readable=found,
                  next_at=now + float(getattr(cfg, "mailbox_probe_seconds", 600) or 600))
    return True
