"""The browser legs: install the Slack app, and link a Slack user to a canopy user.

Bare views (redirects + session), NOT in ``PUBLIC_PATH_PREFIXES`` — both need a
signed-in canopy user, so the default-deny middleware bounces a stranger to
sign-in and back.

**The link requires the Slack email to equal the canopy email.** ace-web's link
page bound whoever opened the URL to the Slack user named inside it. That is a
phishing primitive: an attacker triggers a link for THEIR Slack account, sends it
to a colleague, the colleague signs in — and the attacker's Slack messages now
run as the colleague. Checking the Slack profile's email against the signed-in
account closes it: the link can only ever join two identities that already agree.
"""
from __future__ import annotations

import logging
import secrets
from html import escape
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core import signing
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.views.decorators.http import require_GET

from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership

from . import client, services
from .models import SlackInstallation, SlackUserLink

logger = logging.getLogger(__name__)

# Only what PR 1 uses, plus the history scopes the channel-window context read
# (spec § "Feeding it context") will need, so the one human install step does
# not have to be repeated for it. `im:history` is DMs *with the bot* only.
BOT_SCOPES = [
    "app_mentions:read", "chat:write", "chat:write.customize", "commands",
    "im:history", "im:read", "im:write",
    "users:read", "users:read.email",
    "channels:history", "groups:history",
]
_STATE_KEY = "slack_install_state"


def _page(title: str, body: str, status: int = 200) -> HttpResponse:
    return HttpResponse(
        f"<!doctype html><title>{escape(title)}</title>"
        f"<main style='font-family:system-ui;max-width:40rem;margin:4rem auto;line-height:1.5'>"
        f"<h1>{escape(title)}</h1><p>{body}</p></main>",
        status=status,
    )


def _redirect_uri(request: HttpRequest) -> str:
    # reverse(), not the request path: under the ASGI prefix strip the request
    # path has no /canopy, and Slack compares this string exactly.
    return request.build_absolute_uri(reverse("slack_oauth_callback"))


@login_required
@require_GET
def install(request: HttpRequest) -> HttpResponse:
    """``/auth/slack/install/?workspace=<slug>`` — owner of that workspace only."""
    if not services.is_configured():
        return _page("Slack is not configured", "This deployment has no Slack app credentials.", 503)
    slug = request.GET.get("workspace", "")
    if wsvc.member_role(request.user, slug) != WorkspaceMembership.OWNER:
        return _page("Not allowed", "Only an owner of the workspace can connect Slack to it.", 403)
    state = secrets.token_urlsafe(24)
    request.session[_STATE_KEY] = {"state": state, "workspace": slug}
    params = {
        "client_id": services.client_id(),
        "scope": ",".join(BOT_SCOPES),
        "redirect_uri": _redirect_uri(request),
        "state": state,
    }
    return HttpResponseRedirect("https://slack.com/oauth/v2/authorize?" + urlencode(params))


@login_required
@require_GET
def oauth_callback(request: HttpRequest) -> HttpResponse:
    pending = request.session.pop(_STATE_KEY, None) or {}
    if not pending or request.GET.get("state") != pending.get("state"):
        return _page("Install failed", "The install link expired or was not started here. Start again.", 400)
    slug = pending["workspace"]
    # Re-checked: the role may have changed while the user was on Slack.
    if wsvc.member_role(request.user, slug) != WorkspaceMembership.OWNER:
        return _page("Not allowed", "Only an owner of the workspace can connect Slack to it.", 403)
    if request.GET.get("error"):
        return _page("Install cancelled", escape(request.GET["error"]), 400)
    try:
        data = client.oauth_access(client_id=services.client_id(), client_secret=services.client_secret(),
                                   code=request.GET.get("code", ""), redirect_uri=_redirect_uri(request))
    except client.SlackApiError as e:
        logger.warning("slack oauth exchange failed: %s", e)
        return _page("Install failed", f"Slack refused the exchange ({escape(e.error)}).", 400)
    team = data.get("team") or {}
    team_id = str(team.get("id") or "")
    existing = SlackInstallation.objects.filter(team_id=team_id).first()
    if existing is not None and existing.workspace_id != slug and \
            wsvc.member_role(request.user, existing.workspace_id) != WorkspaceMembership.OWNER:
        # Re-pointing a Slack team at a different workspace moves everyone's
        # messages there. Only someone who owns BOTH ends may do that.
        return _page("Already connected",
                     "This Slack workspace is connected to a different canopy workspace you don't own.", 409)
    inst = existing or SlackInstallation(team_id=team_id)
    inst.team_name = str(team.get("name") or "")
    inst.bot_user_id = str(data.get("bot_user_id") or "")
    inst.bot_token = str(data.get("access_token") or "")
    inst.workspace_id = slug
    inst.installed_by = request.user
    inst.save()
    # The installer just proved both identities in one browser trip — Slack
    # authenticated them as `authed_user`, canopy as `request.user` — so link
    # them now rather than making the first message a refusal.
    installer = str((data.get("authed_user") or {}).get("id") or "")
    if installer:
        SlackUserLink.objects.get_or_create(
            installation=inst, slack_user_id=installer, defaults={"user": request.user},
        )
    return _page("Slack connected",
                 f"<b>{escape(inst.team_name)}</b> now talks to the <b>{escape(slug)}</b> workspace. "
                 "Turn on the agents you want reachable from each agent's Overview page.")


@login_required
@require_GET
def link(request: HttpRequest) -> HttpResponse:
    try:
        team_id, slack_user_id = services.read_link_token(request.GET.get("token", ""))
    except signing.BadSignature:
        return _page("Link expired", "Ask again from Slack with <code>/canopy link</code>.", 400)
    inst = services.installation_for(team_id)
    if inst is None:
        return _page("Not connected", "That Slack workspace is no longer connected to canopy.", 404)
    try:
        slack_email = client.user_email(inst.bot_token, slack_user_id)
    except client.SlackApiError as e:
        logger.warning("slack users.info failed during link: %s", e)
        return _page("Could not check your Slack account", f"Slack said: {escape(e.error)}.", 502)
    mine = (request.user.email or "").strip().lower()
    if not slack_email or slack_email.strip().lower() != mine:
        return _page(
            "Accounts don't match",
            f"This link belongs to a Slack account whose email is not <b>{escape(mine)}</b>. "
            "Links only join a Slack account and a canopy account with the same email — "
            "if someone sent you this link, don't use it.",
            403,
        )
    SlackUserLink.objects.update_or_create(
        installation=inst, slack_user_id=slack_user_id, defaults={"user": request.user},
    )
    return _page("Linked", "Your Slack account is connected to canopy. Head back to Slack and send that again.")
