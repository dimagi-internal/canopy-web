"""Tell an agent's owner, and whoever lent its GitHub token, before the token expires.

A GitHub fine-grained token ends on its date and nothing renews it. Canopy knew the date
(`delegations.status` -> `expiring_soon`, `expired`) but only the runner health check showed
it, so on 2026-11-02 the tokens for ace, ada, eva and hal expired with nobody told: their
turns could no longer push or open a PR. A health badge nobody is looking at is not a warning.

This sweeps every agent holding a delegated GitHub token and, as the date nears, raises one
`github-expiry` alert (`vault_alerts`: an email and a Web Push to the owner and the lender,
listed beside the agent's other vault alerts) at 14, 7, 3 and 1 days out and once on expiry.
Each step is sent once per token: a new token has a new expiry date, so it starts over, and
a token replaced before its next step sends nothing more.

Swept on the off-request clock (apps/common/ticks.py), hourly. BEST-EFFORT: a failure here
must never cost a request or a heartbeat.
"""
from __future__ import annotations

import datetime as dt
import logging

from django.utils import timezone

from ..models import Agent, AgentCredential, AgentCredentialAlert
from . import accounts, delegations, vault_alerts

logger = logging.getLogger(__name__)

#: Days left at which to speak, widest first; 0 is "expired".
STEPS = (14, 7, 3, 1)
SWEEP_EVERY_S = 3600


def step_for(expires: dt.datetime, now: dt.datetime) -> int | None:
    """The sharpest step this token has reached: 0 once expired, else the smallest of
    STEPS at or above the days left; None while more than the widest step away."""
    if expires <= now:
        return 0
    left = (expires - now) / dt.timedelta(days=1)
    for step in sorted(STEPS):
        if left <= step:
            return step
    return None


def _token_ref(expires: dt.datetime) -> str:
    return f"token:{expires:%Y%m%dT%H%M%S}"


def _told_at_or_below(agent: Agent, expires: dt.datetime, step: int) -> bool:
    """Whether this token already raised an alert at `step` or a sharper one."""
    ref = _token_ref(expires)
    for alert in AgentCredentialAlert.objects.filter(agent=agent, kind=AgentCredentialAlert.GITHUB_EXPIRY):
        refs = list(alert.refs or [])
        if ref not in refs:
            continue
        for r in refs:
            if r.startswith("step:") and int(r.split(":", 1)[1]) <= step:
                return True
    return False


def _detail(agent: Agent, expires: dt.datetime, step: int, login: str) -> str:
    who = f"@{login}" if login else "its delegated identity"
    if step == 0:
        return (f"{agent.slug}'s GitHub token ({who}) expired on {expires:%Y-%m-%d %H:%M} UTC. "
                f"Its turns cannot push or open pull requests until a new one is lent.")
    when = "within a day" if step == 1 else f"within {step} days"
    return (f"{agent.slug}'s GitHub token ({who}) expires on {expires:%Y-%m-%d %H:%M} UTC, {when}. "
            f"After that its turns cannot push or open pull requests.")


def sweep(now: dt.datetime | None = None) -> list[tuple[str, int]]:
    """One pass. Returns [(agent slug, step)] for each alert raised."""
    now = now or timezone.now()
    raised: list[tuple[str, int]] = []
    horizon = now + dt.timedelta(days=max(STEPS))
    rows = (AgentCredential.objects.filter(name=accounts.GITHUB_ENTRY, provider="github")
            .select_related("agent", "delegated_by"))
    for entry in rows.filter(provider_expires_at__isnull=False, provider_expires_at__lte=horizon):
        agent = entry.agent
        try:
            current = delegations.github_entry(agent)
            if current is None or current.pk != entry.pk:
                continue                       # a different delegation is in force now
            expires = entry.provider_expires_at
            step = step_for(expires, now)
            if step is None or _told_at_or_below(agent, expires, step):
                continue
            login = (entry.identity or {}).get("login", "")
            alert = vault_alerts.raise_alert(
                agent, AgentCredentialAlert.GITHUB_EXPIRY, detail=_detail(agent, expires, step, login),
                refs=[_token_ref(expires), f"step:{step}"])
            if alert is not None:
                raised.append((agent.slug, step))
        except Exception:  # noqa: BLE001 — one agent's failure must not stop the others
            logger.exception("github expiry sweep failed for %s", getattr(agent, "slug", "?"))
    return raised


def tick() -> None:
    try:
        sweep()
    except Exception:  # noqa: BLE001
        logger.exception("github expiry sweep failed")
