# Relay conversations: a host site as the system of record

A **relay** is a host site carrying a person's conversation with a canopy agent server-to-server, over a channel of its own (PersonalID, WhatsApp, SMS). The person never opens canopy. The host's server mints the person a contact token and drives the same `/api/contact/` routes a browser widget uses; nothing about a relay is a second way into canopy. How a site is registered and offers an agent is [host-sites.md](host-sites.md); the host's guide to the widget is [embedding-a-canopy-agent.md](embedding-a-canopy-agent.md).

## The host is the system of record

A relay is usually one surface of a bigger site ([host-sites.md](host-sites.md) § Surfaces): Connect Labs relays its Connect Workers on its `workers` surface, signed with the same key as its pages, and its relay arrivals carry no `sensitive_data` requirement because that surface holds none.

A relay site is the system of record for the people it brings ([host-systems-of-record.md](host-systems-of-record.md): canopy keeps an anchor, a pointer, a cache the host owns, and the working sessions). A relay changes none of that: the conversation's sessions are canopy's working copy, what the host knows about the person reaches the agent only as the host states it, and the agent's replies go back to the host, which carries them on its own channel.

## Relay channels

A relay arrival is a visitor assertion carrying `canopy_channel` (`canopy_sdk.contract.CHANNEL_CLAIM`, `^[a-z0-9_-]{1,32}$`). The site opts in by signing it; nothing changes for a site that does not.

- `contact_token` parses it from the **verified** assertion (`contract.parse_channel_claim`); a malformed one is refused `400 bad_channel`, never served as a widget. It is kept on the token (`ContactToken.channel`) and stamped on every session the token starts (`metadata["relay_channel"]`, server-owned: `canopy_sessions.services.RELAY_CHANNEL_KEY`).
- **A relay arrival is always a contact.** `resolve_arrival` is skipped, so a person the site could resolve to a canopy member by verified email still arrives as the contact: they are on the host's channel, not at a canopy browser, and a delegated user token would hand the host that member's own conversations.
- A turn sent with the token records its channel as `relay:<site>:<channel>` (`apps/harness/initiator.py::channel`) instead of `widget:<site>`, so the envelope's `who.via` names the channel.
- Its source (`Turn.origin`) is `site_message`. Only a relay token may name it (`Turn.RELAY_SEND_ORIGINS`, `chat_send_origin(..., relay=True)`), and it is a relay send's default. It is routable (`Turn.ROUTABLE_ORIGINS`), an admission rule and a retention rule may name it, and a relay session is a `site_message` chat to retention (`retention.policy.chat_source`). The canopy plugin treats it as a person present.

The SDK half is `canopy_sdk.host.relay.RelayClient`: `token_for` mints and caches the person's token (bound to the agent with `canopy_agent`) until five minutes before it expires, and `open_session` / `send` / `turn` / `messages` call the contact routes with it; `RelayError.code` is canopy's refusal code.

## Context the host attests on a message

A send may carry `context`: what the host knows about this moment (the person's stage, the task they are on), as a compact JWS the site signs with its own key (`typ: canopy-context+jwt`; `canopy_sdk.attested_context.sign_context`, or `RelayClient.send(context=...)`). The person's words stay the message's `text`; the host's facts never join them.

- `contact_api._attested_context` checks it with the site's own keys (`assertions.keys_for_app`, the keys its arrival verified with): the `typ`, the site's name as `iss`, at most 60 seconds, a single-use `jti` (failing closed), `format` `markdown` or `json` and a body of at most 16384 characters. Its `sub` must be the conversation's contact (`Contact.external_id`) and its `session` the conversation's id, else `422 context_mismatch`; anything else that does not verify is `422 bad_context`. A sensitive conversation takes none.
- It is kept on the one turn it rode with (`origin_ref["host_context"] = {site, format, body}`), and the envelope carries it as `host_context: {site, format, body, attested: true}` on that turn only (`caller_context._host_context`); the next turn does not inherit it. The canopy plugin prints it fenced and labelled as the site's, data and not instructions.
- It is content: retention's turn scrub drops it with the prompt (`retention.services._scrub_turns`), and a person deleting a session's words drops it with the rest of `origin_ref` (`canopy_sessions.content_deletion`).

## Reply push

A host site with a `reply_webhook_url` (`AppCredential.reply_webhook_url`, set by the workspace owner on the host-site PATCH behind a passkey step-up, and checked by `outbound.check_url` when set and again at every send) never polls: canopy POSTs the agent's replies in its contacts' conversations there (`apps/host_grants/reply_relay.py`).

- **Which turns.** A turn on a RELAY session (`metadata["relay_channel"]`) whose contact the site vouched for (`Contact.app`), held on that site (`metadata["embed_app"]`), while the site is live and has a URL. A sensitive conversation's words are never pushed, as Slack never mirrors them. The webhook is the site's, one per site whatever its surfaces ([host-sites.md](host-sites.md) § Surfaces): only a relay needs one (a widget's visitor reads the reply in the panel), and the host routes each reply by its `session`.
- **What.** `Content-Type: application/jwt`, a JWS with `typ: canopy-reply+jwt`, signed with canopy's OAuth client key (the key in canopy's client metadata, which a host already trusts for `Canopy-Execution`): `{iss: client_id, aud: reply_webhook_url, sub: the contact's external_id, site, session, turn, seq, text, ended, error?, iat, exp: +300s, jti}` (`canopy_sdk.host.replies.sign_reply`). `reply_mode` `final` (the default) is one post when the turn ends, with all its assistant prose; `stream` is a post per assistant row as it lands (`turn_events_appended`), then an `ended` post with no text. A turn that did not finish well carries `error` (its status) on its `ended` post.
- **Exactly once per `(turn, seq)`.** A `HostReplyDelivery` row is inserted before the first attempt under a unique `(turn, seq)` and one `ended` row per turn, so a re-delivered signal stops there; each attempt signs afresh, and the host dedupes on `(turn, seq)`. The `ended` post is queued after the turn's close commits (`session_turn_finished` → `transaction.on_commit`).
- **Retries.** The first attempt runs on a background thread; a failure (a non-2xx answer, a timeout, a refused URL) is retried by the off-request clock (`host_grants.reply_deliver`, `reply_relay.deliver_due`) at `reply_relay.BACKOFF`, then given up on. Never a redirect.
- **What it keeps.** The row holds the text only until it is delivered or given up on; a person deleting the conversation's words empties it (`reply_relay.forget_session`, on `session_content_deleted`).
- **The host's half** is `canopy_sdk.host.replies.verify_reply(token, *, config)`, which checks canopy's key from its client metadata, `aud` = `HostConfig.reply_url`, `site` = the host's name, the lifetime and the `jti`.

## Limits

Three budgets bound what one site, one person and one agent can spend. Each is off or generous for a site that never relays.

| Budget | Bounds | Where |
|---|---|---|
| Contact tokens per site per window | the contacts a leaked signing key can create, and the signature checks a site can make canopy do | `rate_limit.check_contact_token_issuer`: the site's `AppCredential.contact_token_issuer_limit`, else `CONTACT_TOKEN_ISSUER_LIMIT`. Only a canopy superuser sets a site's own budget (`PATCH /api/workspaces/{slug}/host-sites/{app_id}`, `contact_token_issuer_limit`; 0 resets it) |
| Sends per contact | the turns one person, or a host looping on one person, can start | `rate_limit.check_contact_send`: `CONTACT_SEND_PER_MINUTE`, `CONTACT_SEND_PER_DAY`. Over either is `429 rate_limited`, before anything is written. Counted on the contact send and a contact's app-view message |
| Turns per agent per 24 hours | what one agent spends | an admission rule of kind `agent_daily_turns` with `max_turns` (`AdmissionRule.AGENT_DAILY_TURNS`, `evaluate.daily_turns`): off unless a workspace admin adds one. Reject only; its other conditions narrow which turns it rejects, not what it counts. Counted over the 24 hours before the turn, so the claim agrees with the enqueue; a turn a rule rejected does not count |

## Code map

| Question | Where |
|---|---|
| The arrival: channel claim, always a contact, the site's budget | `apps/contacts/contact_api.py::contact_token` |
| A send: the relay source, the send budget, the attested context | `apps/contacts/contact_api.py::send`, `_spend_send`, `_attested_context` |
| The context on the envelope | `apps/harness/caller_context.py::_host_context` |
| Reply push: which turns, what is posted, retries | `apps/host_grants/reply_relay.py`; its receivers and clock job in `apps/host_grants/apps.py` |
| The channel on a turn | `apps/harness/initiator.py::channel`, `apps/tokens/middleware.py` (`request.relay_channel`) |
| The budgets | `apps/embedding/rate_limit.py`, `apps/admission/evaluate.py` |
| The host's client; the context's and the reply's wire | `sdk/python/src/canopy_sdk/host/relay.py`, `sdk/python/src/canopy_sdk/attested_context.py`, `sdk/python/src/canopy_sdk/host/replies.py` |
