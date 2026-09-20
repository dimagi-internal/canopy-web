"""Keep the Slack app's slash commands in step with which agents are on for Slack.

Slack has no wildcard command: every `/hal` must be registered in the APP's own
config (its manifest), which lives in Slack, not here. So canopy edits the
manifest itself — `apps.manifest.export`, change `features.slash_commands`,
`apps.manifest.update` — whenever an agent's Slack switch flips, and on demand.

**What canopy manages, and what it never touches.** A command is canopy's to
add or remove only if it points at canopy's own commands URL AND is named after
an agent in this workspace (plus `/canopy` itself, which it only ever adds).
Anything else in the manifest — a command someone added by hand for another
purpose, the events config, scopes — is written back exactly as exported.

**The name is the agent's slug**, which is globally unique in canopy (the
`Agent.slug` unique constraint, kept on purpose for this: two tenants' agents
cannot both be `/ace`). Slack allows at most 32 characters and lowercase; a
slug that does not fit is reported, not truncated into somebody else's name.

**The credential** is an app configuration token (see SlackInstallation). Its
refresh token is single-use, so rotation happens under a row lock and the new
pair is stored before anything else can fail.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from django.db import transaction
from django.utils import timezone

from apps.common.encryption import decrypt_secret, encrypt_secret

from . import client
from .models import SlackInstallation

logger = logging.getLogger(__name__)

#: Rotate this long before expiry, so a token never dies mid-sync.
_ROTATE_MARGIN = dt.timedelta(minutes=5)
_MAX_COMMAND = 32
BASE_COMMAND = "/canopy"


class NotConfigured(Exception):
    """This install cannot edit its app: no app id, or no configuration token."""


def _commands_url() -> str:
    from .services import public_url

    return public_url("/api/slack/commands")


def _store(installation: SlackInstallation, body: dict) -> None:
    installation.config_token_enc = encrypt_secret(str(body["token"]))
    installation.config_refresh_enc = encrypt_secret(str(body["refresh_token"]))
    installation.config_expires_at = timezone.now() + dt.timedelta(
        seconds=max(0, int(body.get("exp", 0)) - int(body.get("iat", 0))) or 12 * 3600)
    installation.save(update_fields=["config_token_enc", "config_refresh_enc", "config_expires_at"])


def set_config_token(installation: SlackInstallation, refresh_token: str, *, user) -> None:
    """Accept a refresh token from an owner: rotate it at once (which both proves
    it works and retires the copy the owner just pasted) and store the pair."""
    body = client.call("tooling.tokens.rotate", data={"refresh_token": refresh_token.strip()})
    installation.config_set_by = user
    installation.save(update_fields=["config_set_by"])
    _store(installation, body)


def clear_config_token(installation: SlackInstallation) -> None:
    installation.config_token_enc = installation.config_refresh_enc = ""
    installation.config_expires_at = None
    installation.save(update_fields=["config_token_enc", "config_refresh_enc", "config_expires_at"])


def _access_token(installation: SlackInstallation) -> str:
    if not installation.manages_commands:
        raise NotConfigured("no app id or configuration token")
    with transaction.atomic():
        inst = SlackInstallation.objects.select_for_update().get(pk=installation.pk)
        fresh = inst.config_expires_at and inst.config_expires_at - _ROTATE_MARGIN > timezone.now()
        if not (fresh and inst.config_token_enc):
            body = client.call("tooling.tokens.rotate",
                               data={"refresh_token": decrypt_secret(inst.config_refresh_enc)})
            _store(inst, body)
        return decrypt_secret(inst.config_token_enc)


def command_name(slug: str) -> str | None:
    name = f"/{slug.lower()}"
    return name if len(name) <= _MAX_COMMAND else None


def desired_commands(installation: SlackInstallation) -> tuple[dict[str, dict], list[str]]:
    """({name: command}, [slugs that cannot be a command]) for this workspace."""
    from .services import enabled_agents

    url = _commands_url()
    want = {BASE_COMMAND: {"command": BASE_COMMAND, "url": url, "description": "Ask a canopy agent",
                           "usage_hint": "<agent> <ask> | agents | link", "should_escape": False}}
    unfit = []
    for agent in enabled_agents(installation):
        name = command_name(agent.slug)
        if name is None:
            unfit.append(agent.slug)
            continue
        want[name] = {"command": name, "url": url, "description": f"Ask {agent.name or agent.slug}"[:100],
                      "usage_hint": "<ask>", "should_escape": False}
    return want, unfit


def reconcile(installation: SlackInstallation) -> dict:
    """Make the app's commands match. Returns {added, removed, unfit}; raises
    NotConfigured, or client.SlackApiError on Slack's refusal (recorded too)."""
    from apps.agents.models import Agent

    try:
        token = _access_token(installation)
        manifest = client.call("apps.manifest.export", token=token,
                               data={"app_id": installation.app_id})["manifest"]
        url = _commands_url()
        want, unfit = desired_commands(installation)
        ours = {f"/{s.lower()}" for s in Agent.objects.filter(workspace=installation.workspace)
                .values_list("slug", flat=True)} | {BASE_COMMAND}
        features = manifest.setdefault("features", {})
        current = list(features.get("slash_commands") or [])

        kept, removed = [], []
        for cmd in current:
            name = str(cmd.get("command") or "").lower()
            managed = name in ours and cmd.get("url") == url
            if managed and name not in want:
                removed.append(name)
                continue
            kept.append(cmd)
        present = {str(c.get("command") or "").lower() for c in kept}
        added = [name for name in want if name not in present]
        kept += [want[name] for name in added]

        if added or removed:
            features["slash_commands"] = kept
            client.call("apps.manifest.update", token=token,
                        data={"app_id": installation.app_id, "manifest": json.dumps(manifest)})
        installation.commands_synced_at = timezone.now()
        installation.commands_sync_error = ""
        installation.save(update_fields=["commands_synced_at", "commands_sync_error"])
        return {"added": sorted(added), "removed": sorted(removed), "unfit": unfit}
    except NotConfigured:
        raise
    except Exception as e:
        installation.commands_sync_error = str(e)[:300]
        installation.save(update_fields=["commands_sync_error"])
        raise


def sync_quietly(installation: SlackInstallation | None) -> dict:
    """reconcile(), for a caller that must not fail on Slack (flipping a switch).

    Returns {"status": "synced"|"not_configured"|"error", ...} so the caller can
    SAY what happened — a switch that silently left `/hal` unregistered would
    look exactly like one that worked until someone typed it.
    """
    if installation is None:
        return {"status": "not_configured", "detail": "Slack is not connected to this workspace."}
    try:
        return {"status": "synced", **reconcile(installation)}
    except NotConfigured:
        return {"status": "not_configured",
                "detail": "canopy can't edit the Slack app's commands yet — connect it on the Slack page."}
    except Exception as e:  # noqa: BLE001
        logger.exception("slack command sync failed")
        return {"status": "error", "detail": str(e)[:300]}


# --- declaring the app an agent (Slack's native "Working…" indicator) ---------
#
# The indicator, its Stop button and the "needs you" state are Agent Sessions
# (`client.set_session_status`), which Slack draws only for an app that declares
# itself an agent. That is three edits to the SAME manifest this module already
# owns — so it is done here, through the same configuration token, rather than
# by hand in a UI nobody can diff.
#
# NOT done on a deploy or a switch flip, unlike `reconcile`. It changes how the
# app presents itself (agent conversations render in the app's Messages tab) and
# `agent_view` cannot be swapped back to the older `assistant_view` once set, so
# it is an explicit act: `manage.py slack_declare_agent`.

AGENT_SCOPE = "assistant:write"
AGENT_EVENTS = ["agent_session_stopped", "agent_session_title_changed", "app_context_changed"]
AGENT_DESCRIPTION = "Talk to your canopy agents — they answer in the thread."


def declare_agent(installation: SlackInstallation, *, description: str = "") -> dict:
    """Add `features.agent_view`, the agent scope and the agent events.

    Returns what it changed, and what remains for a human: adding a SCOPE takes
    effect only on re-install, so the caller is told to re-authorise rather than
    left believing a silent no-op worked. Idempotent — a second run changes
    nothing and says so.
    """
    token = _access_token(installation)
    manifest = client.call("apps.manifest.export", token=token,
                           data={"app_id": installation.app_id})["manifest"]
    changed = []

    features = manifest.setdefault("features", {})
    if "assistant_view" in features:
        # Slack's own note: the swap is one-way, and this tool is not the place
        # to make an irreversible choice on someone's behalf.
        raise ValueError("this app declares the older assistant_view; migrate it in Slack's UI first")
    if "agent_view" not in features:
        features["agent_view"] = {"agent_description": (description or AGENT_DESCRIPTION)[:300]}
        changed.append("features.agent_view")

    scopes = manifest.setdefault("oauth_config", {}).setdefault("scopes", {})
    bot = list(scopes.get("bot") or [])
    if AGENT_SCOPE not in bot:
        scopes["bot"] = sorted({*bot, AGENT_SCOPE})
        changed.append(f"scope {AGENT_SCOPE}")

    subs = manifest.setdefault("settings", {}).setdefault("event_subscriptions", {})
    events = list(subs.get("bot_events") or [])
    missing = [e for e in AGENT_EVENTS if e not in events]
    if missing:
        subs["bot_events"] = events + missing
        changed.append("events " + ", ".join(missing))

    if changed:
        client.call("apps.manifest.update", token=token,
                    data={"app_id": installation.app_id, "manifest": json.dumps(manifest)})
    return {"changed": changed,
            "reinstall_required": any(c.startswith("scope") for c in changed)}
