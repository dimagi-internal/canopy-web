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


def post_message(token: str, *, channel: str, text: str, thread_ts: str = "",
                 blocks: list | None = None, persona: dict | None = None) -> str:
    """Post, optionally AS an agent (`persona` = {"username", "icon_url"}).

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
            return call("chat.postMessage", token=token, json={**payload, **persona})["ts"]
        except SlackApiError as e:
            if e.error != "missing_scope":
                raise
    return call("chat.postMessage", token=token, json=payload)["ts"]


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


def user_email(token: str, slack_user_id: str) -> str:
    return str((user_info(token, slack_user_id).get("profile") or {}).get("email") or "")


def oauth_access(*, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    return call("oauth.v2.access", data={
        "client_id": client_id, "client_secret": client_secret,
        "code": code, "redirect_uri": redirect_uri,
    })
