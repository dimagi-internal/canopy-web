"""The handful of Slack Web API calls canopy makes, over ``requests``.

Not ``slack_sdk``: five POSTs do not need a dependency. The one trap worth a
wrapper is that Slack answers a failed call with **HTTP 200 and ``ok: false``**,
so a status-code check reports failure as success. ``call`` raises instead.
"""
from __future__ import annotations

import requests

API = "https://slack.com/api/"
TIMEOUT = 10


class SlackApiError(Exception):
    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


def call(method: str, *, token: str = "", data: dict | None = None, json: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if json is not None:
        resp = requests.post(API + method, headers=headers, json=json, timeout=TIMEOUT)
    else:
        resp = requests.post(API + method, headers=headers, data=data or {}, timeout=TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if not body.get("ok"):
        raise SlackApiError(method, str(body.get("error") or "unknown_error"))
    return body


#: `agents.sessions.setStatus` values. PROCESSING draws Slack's own "Working…"
#: indicator (with a Stop button, since we subscribe to `agent_session_stopped`);
#: SUSPENDED is "it needs a person"; ACTIVE is "ready for your next message".
PROCESSING, ACTIVE, SUSPENDED, CLOSED = "processing", "active", "suspended", "closed"

#: Every way this call can mean "this app/workspace does not do agent sessions".
#: All are configuration facts, not failures of the thing being reported, so the
#: caller degrades to the text line it already posts.
_NO_AGENT_SESSIONS = frozenset({
    "feature_disabled", "missing_scope", "not_allowed_token_type", "invalid_arguments",
    "unknown_method", "method_not_supported_for_channel_type", "invalid_channel_type",
    "agent_not_enabled", "not_an_agent",
})


def set_session_status(token: str, *, channel: str, status: str, thread_ts: str = "") -> bool:
    """Drive Slack's NATIVE working indicator for this thread. True if it took.

    The one thing a posted message cannot do: an edit to text is silent and
    static, while this is the spinner Slack draws under the composer in the
    thread itself, and it is what carries the Stop button. It needs the app to
    be declared an agent (`features.agent_view` + `assistant:write` — see
    `commands.declare_agent`), so on any deployment where that has not been done
    every call answers one of `_NO_AGENT_SESSIONS`. That is not an error worth
    raising: the status line says the same thing in words, so this returns False
    and the thread is no worse off than before.
    """
    payload = {"channel_id": channel, "status": status}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        call("agents.sessions.setStatus", token=token, json=payload)
        return True
    except SlackApiError as e:
        if e.error in _NO_AGENT_SESSIONS:
            return False
        raise


def post_message(token: str, *, channel: str, text: str, thread_ts: str = "",
                 blocks: list | None = None, persona: dict | None = None) -> str:
    """Post, optionally AS an agent (`persona` = {"username", "icon_url"}). Returns the ts."""
    return post_message_body(token, channel=channel, text=text, thread_ts=thread_ts,
                             blocks=blocks, persona=persona)["ts"]


def post_message_body(token: str, *, channel: str, text: str, thread_ts: str = "",
                      blocks: list | None = None, persona: dict | None = None) -> dict:
    """`post_message`, returning Slack's whole answer — including the channel ID,
    which is what a caller that posted to a `#name` needs to store.

    A persona needs the `chat:write.customize` scope. An install from before it
    was added answers `missing_scope`; the post is retried as the plain bot, so
    a scope nobody has re-approved costs the agent's face, never the reply.
    """
    # `text` is always sent, blocks or not: it is what notifications, screen
    # readers and any client that cannot render blocks show.
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if blocks:
        payload["blocks"] = blocks
    if persona:
        try:
            return call("chat.postMessage", token=token, json={**payload, **persona})
        except SlackApiError as e:
            if e.error != "missing_scope":
                raise
    return call("chat.postMessage", token=token, json=payload)


def update_message(token: str, *, channel: str, ts: str, text: str, blocks: list | None = None) -> None:
    call("chat.update", token=token, json={"channel": channel, "ts": ts, "text": text,
                                           "blocks": blocks or []})


def post_ephemeral(token: str, *, channel: str, user: str, text: str, thread_ts: str = "") -> None:
    payload = {"channel": channel, "user": user, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    call("chat.postEphemeral", token=token, json=payload)


def user_info(token: str, slack_user_id: str) -> dict:
    return call("users.info", token=token, data={"user": slack_user_id}).get("user") or {}


def lookup_user_id(token: str, email: str) -> str:
    """The Slack user with this email, or "" (needs `users:read.email`)."""
    try:
        return str((call("users.lookupByEmail", token=token, data={"email": email}).get("user") or {})
                   .get("id") or "")
    except SlackApiError:
        return ""


def permalink(token: str, *, channel: str, ts: str) -> str:
    try:
        return str(call("chat.getPermalink", token=token,
                        data={"channel": channel, "message_ts": ts}).get("permalink") or "")
    except SlackApiError:
        return ""


def user_email(token: str, slack_user_id: str) -> str:
    return str((user_info(token, slack_user_id).get("profile") or {}).get("email") or "")


def oauth_access(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    return call("oauth.v2.access", data={
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "redirect_uri": redirect_uri,
    })
