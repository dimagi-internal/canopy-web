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


def _former_urls(path: str) -> set[str]:
    """``path`` under every address this deployment has since left.

    canopy moved from labs.connect.dimagi.com/canopy to canopy.dimagi.com
    (2026-10-05), and its protocol identity followed. Without this a sync no
    longer recognised the commands it had created — `managed` compares URLs — so
    it could neither move them to the new address nor remove one whose agent was
    switched off. ``CANOPY_FORMER_BASE_URLS`` names the old addresses; the
    identity base is included while it still differs from the visited one.
    """
    from django.conf import settings

    from apps.tokens.client_identity import public_base

    bases = {public_base(), *(getattr(settings, "CANOPY_FORMER_BASE_URLS", None) or [])}
    from .services import public_url

    return {f"{b.rstrip('/')}{path}" for b in bases if b} - {public_url(path)}


def _former_commands_urls() -> set[str]:
    return _former_urls("/api/slack/commands")


#: The app's two other webhooks: (manifest path to the url, canopy's path).
#: Slack sends every event and every button press to exactly one URL each, so
#: these move with the address too, or a sync leaves them on the old one.
_WEBHOOKS = ((("event_subscriptions", "request_url"), "/api/slack/events"),
             (("interactivity", "request_url"), "/api/slack/interactions"))


def _move_webhooks(manifest: dict) -> list[str]:
    """Re-point the event and interactivity URLs that sit on a former address.

    Only a URL canopy recognises as its own old one moves; a URL pointing
    anywhere else is somebody's deliberate choice and is left alone."""
    from .services import public_url

    moved = []
    settings_ = manifest.setdefault("settings", {})
    for (section, key), path in _WEBHOOKS:
        block = settings_.get(section)
        if isinstance(block, dict) and block.get(key) in _former_urls(path):
            block[key] = public_url(path)
            moved.append(section)
    return moved


def _oauth_redirect_url() -> str:
    from django.urls import get_script_prefix, reverse

    from .services import public_url

    # The path reverse() gives, without any script prefix: public_url already
    # names the address it is served at.
    return public_url(reverse("slack_oauth_callback").removeprefix(get_script_prefix().rstrip("/")))


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


#: Slack shows a command's usage hint as you type it — the one place someone
#: learns that `--history` exists without having been told (`window.py`).
HISTORY_HINT = "[--history <minutes or time, e.g. 30 or 9am> to include recent channel messages] "


def desired_commands(installation: SlackInstallation) -> tuple[dict[str, dict], list[str]]:
    """({name: command}, [slugs that cannot be a command]) for every tenant this Slack serves."""
    from .services import enabled_agents

    url = _commands_url()
    want = {BASE_COMMAND: {"command": BASE_COMMAND, "url": url, "description": "Ask a canopy agent",
                           "usage_hint": f"<agent> {HISTORY_HINT}<ask> | agents | link", "should_escape": False}}
    unfit = []
    for agent in enabled_agents(installation):
        name = command_name(agent.slug)
        if name is None:
            unfit.append(agent.slug)
            continue
        want[name] = {"command": name, "url": url, "description": f"Ask {agent.name or agent.slug}"[:100],
                      "usage_hint": f"{HISTORY_HINT}<ask>", "should_escape": False}
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
        former = _former_commands_urls()
        want, unfit = desired_commands(installation)
        # Every tenant this Slack serves, not one: the app and its commands are
        # shared, and a sync run for tenant B that only knew B's agents would
        # delete tenant A's `/hal` as "not wanted".
        ours = {f"/{s.lower()}" for s in Agent.objects.filter(workspace_id__in=installation.workspace_ids())
                .values_list("slug", flat=True)} | {BASE_COMMAND}
        features = manifest.setdefault("features", {})
        current = list(features.get("slash_commands") or [])

        kept, removed, updated = [], [], []
        for cmd in current:
            name = str(cmd.get("command") or "").lower()
            managed = name in ours and cmd.get("url") in {url} | former
            if managed and name not in want:
                removed.append(name)
                continue
            if managed and any(cmd.get(k) != want[name][k] for k in ("description", "usage_hint", "url")):
                # Ours and still wanted, but worded as it was when created — or
                # pointing at the address canopy has moved away from.
                cmd = {**cmd, **want[name]}
                updated.append(name)
            kept.append(cmd)
        present = {str(c.get("command") or "").lower() for c in kept}
        added = [name for name in want if name not in present]
        kept += [want[name] for name in added]

        # The app's declared bot scopes must cover what the install asks for
        # (views_auth.BOT_SCOPES), or a new scope never lands. Adding one still
        # needs a re-install to take effect; this only puts it on the app. Not
        # the agent scope: that one is `declare_agent`'s deliberate act.
        from .views_auth import BOT_SCOPES

        scopes = manifest.setdefault("oauth_config", {}).setdefault("scopes", {})
        bot = list(scopes.get("bot") or [])
        scopes_added = [s for s in BOT_SCOPES if s not in bot and s != AGENT_SCOPE]
        if scopes_added:
            scopes["bot"] = sorted({*bot, *scopes_added})
        # Slack compares the install's redirect_uri exactly, and it is built from
        # the address the person is on — so when canopy's address moves, the
        # new callback has to be on the app or connecting Slack fails.
        oauth = manifest.setdefault("oauth_config", {})
        redirects = list(oauth.get("redirect_urls") or [])
        redirect_added = _oauth_redirect_url() not in redirects
        if redirect_added:
            redirects = redirects + [_oauth_redirect_url()]
        # And the callback on an address canopy has left goes: nothing builds it
        # any more (an install starts from the address the person is on, and the
        # old address redirects every browser page to the new one), so keeping it
        # only leaves Slack willing to send an install somewhere canopy is not.
        former_callbacks = _former_urls("/auth/slack/callback/")
        redirects_removed = [u for u in redirects if u in former_callbacks]
        if redirect_added or redirects_removed:
            oauth["redirect_urls"] = [u for u in redirects if u not in former_callbacks]
        webhooks_moved = _move_webhooks(manifest)
        if (added or removed or updated or scopes_added or redirect_added or webhooks_moved
                or redirects_removed):
            features["slash_commands"] = kept
            client.call("apps.manifest.update", token=token,
                        data={"app_id": installation.app_id, "manifest": json.dumps(manifest)})
        installation.commands_synced_at = timezone.now()
        installation.commands_sync_error = ""
        installation.save(update_fields=["commands_synced_at", "commands_sync_error"])
        return {"added": sorted(added), "removed": sorted(removed), "updated": sorted(updated), "unfit": unfit,
                "scopes_added": scopes_added, "webhooks_moved": webhooks_moved,
                "redirects_removed": redirects_removed}
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
