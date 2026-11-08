# Tenancy: sensitive conversations

What makes a conversation sensitive (`apps/harness/sensitivity.py`), who may mark and read it, and what it changes. Part of the [tenancy](tenancy.md) topic; the index there lists every part.

## Sensitive conversations (`apps/harness/sensitivity.py`)

`is_sensitive(session | turn)` — the requirements (`runner_requirements.requirements_of`,
so a child, dispatched or forked turn inherits it) include `sensitive_data`, or cannot be read. It is
THE predicate; every surface below asks it, one test each in
`tests/test_sensitive_storage.py` (spec `2026-10-22-sensitive-data-agents` §4):

- **Stored less:** no `TurnTranscript` (`ledger.append_transcript` answers the runner with
  an empty success); a `tool_end`/`tool_result` event, a `TOOL_RESULT` `Message` and its
  live frame keep `{tool_use_id, is_error, status, withheld}`, never the result
  (`sensitivity.strip_result`, in `ledger.append_events`, `persist_transcript_rows` and
  `post_session_stream`; an allowlist, so a new field stays withheld, and each kept
  string in its shape: ids and names as identifiers, `status` from `TOOL_STATUSES`) —
  plus the host's **resource links** (`resource_links`: `<scheme>://<url-safe id>`, a
  mimeType from `canopy_sdk.resources.MIME_TYPES`, and the scheme as the name — addresses,
  not data; spec `2026-10-23-host-held-results`). A runner pulls links out of the raw
  result before cutting its text (`canopy_transcript.tool_result_links`). A link is kept
  only when the result is the **site's own tool's** — `site_call`, or a site tool canopy
  indexed reached directly (`site_needs.is_site_tool`, registered as
  `sensitivity.register_site_tools`; the call is placed from its batch, `calls_in`, or the
  stored call, `producing_tool`) — so link-shaped output of any other tool is dropped,
  and when it is a valid MCP `resource_link` (at most 10, never a reserved scheme such as
  `http(s)`/`data`/`ui`) under a URI scheme the conversation's site declares in its
  signed `site.json` (`site.resource_schemes`, with the exact id length from
  `site.resource_id_lengths` where declared; read live through
  `site_needs.resource_schemes_for`, registered as `sensitivity.register_resource_schemes`;
  unreadable → none kept). The host keeps the artifact; a View
  (`mcp_apps_views.withheld_result`) and the agent (`site_read`) read it back from the host
  as the person. Nothing else of the result survives, which
  `tests/test_host_held_results.py`'s canary checks across every table.
- **Metadata in every readable column:** every stored event and every `Message`'s
  `plaintext`/`content` (the person's, the agent's, a tool call's) keeps kind, tool name,
  argument names, ids, error flags, size and hash only (`sensitivity.redact_payload`). The
  prompt is wiped at the turn's end (`turn_close.close_turn`), the result note withheld, and
  there is no owner-ask line, live tail (`RunnerBinding.tail`) or autotitle.
- **The conversation, sealed to its own people** (spec
  `2026-10-23-sensitive-conversations-sealed-to-their-people` §2,
  `apps/canopy_sessions/conversation_keys.py`): canopy-web can WRITE a sensitive conversation
  and cannot read it back.
  - *The key.* A chat started from canopy's own chat surfaces (the chat page, the embedded
    panel) carries a P-256 key pair its starter's BROWSER made (`SessionCreateIn.conversation_key`,
    `sealedChat.ts::newConversationKey`): the public half, a fresh conversation id, and the
    private half wrapped (ECDH-ES + HKDF-SHA256 + AES-256-GCM, the vault's `wrapEcdhEs`, info
    `canopy-chat/v1/wrap|<conversation>|<user>`) to the starter's vault key
    (`GET /my-vault-key`) — its PUBLIC half, so starting a chat or sending in one, private
    or not, never asks for a passkey (`sealedChat.offerForNewChat`). A key pinned in this
    browser (`trust.checkSelf`) is used silently. A key never seen in this browser (a fresh
    browser, the panel's frame, storage the browser will not keep), or CHANGED since this
    browser pinned it (the person replaced their key in another browser), is **trusted on
    first use**: the chat starts wrapped to the key canopy names, pinned here as unconfirmed
    with the chat's id (`trust.trustSelfOnFirstUse`). The next time the person opens their
    vault key in that browser for any reason (`unlockVaultKey`, `stepUpAndUnlock`,
    `unlockLocally` → `trust.rememberSelf`), the key they actually hold — derived from its
    private scalar — is checked against it; a mismatch is a warning naming those chats on the
    sealed-conversation banner (`useSealedConversation.warning`), the pin becomes the key
    they hold, and the disproven key stops any later chat here (the panel and the chat page
    say why and keep what was typed). **The trade-off (the owner's decision):** a key
    substituted by a write to the database, with no login or passkey, is DETECTED at the
    next unlock in that browser, not prevented, so a chat that turns private before then
    could be read by whoever holds that key. Refusing would stop every browser but
    one for anyone who replaced their key, with no way to tell the two apart. With storage
    blocked nothing is remembered, so each start trusts anew and nothing is checked.
    canopy-web keeps it (`SessionKey.public_jwk`, a `SessionKeyWrap`) even
    while the chat is ordinary, so if it becomes sensitive its words are sealed to a key
    canopy-web never saw the private half of (`conversation_keys.check_offer`, `ensure_key`).
    **The limit:** a session that becomes sensitive with no browser there — made from Slack,
    MCP, a contact or a host's grant, or a chat that came with no key — gets a pair made on
    the server, its private half wrapped to the starter and dropped at once (the `zk_seal`
    stance: canopy-web saw it once). A fork, successor or branch (`parent_session`) carries
    the same key and every wrap (`copy_key`) — only when its creator is one of the
    parent's own people (`conversation_keys._carries_on`, `is_conversation_person`):
    naming a private chat as `parent` gives anyone else a key of their own, no copy in
    their `/my-keys`, and none of its people (`services._inherit_participants` asks the
    same of a sensitive parent).
  - *Sealing.* Each stored message's words (`{plaintext, content}`; never a tool's result)
    go into `Message.sealed` as a `cc1:` ECIES envelope to the conversation's public key,
    info and AAD `canopy-chat/v1/message|<conversation>|<turn_index>|<role>`, the role also
    named as `r` (`sensitivity.seal`, from `send.py` and `transcript_ingest`; an envelope
    without `r` predates the role binding). The browser opens a row only where its envelope
    says it belongs: the conversation, its index (`i == turn_index + d`, d in
    [0, …`sealed_key.index_offsets`] — a fork's copied rows sit the recorded offset below the
    index they were sealed at) and its speaker. No key canopy-web holds — the field key, the
    legacy `SECRET_KEY` key — opens it, and no private key is stored.
    `docs/vault/chat-v1/chat_v1_vectors.json` pins the bytes for both languages.
  - *Reading.* ONE gate hands the ciphertext out, `sensitivity.reads_content`: the
    conversation's own people (its creator, its participants) on its own chat surfaces (a
    browser session, the widget's site token; never MCP, a PAT, OAuth or a minted session).
    The chat REST reads (`GET /{id}`, `/messages`) and the socket snapshot return each row's
    `sealed` (`sensitivity.readable`) and `GET /{id}` the reader's own wrap
    (`sealed_key`); the chat page and the embedded panel unlock the person's vault key
    (passkey or passphrase), unwrap, and decrypt in the browser (`frontend/src/vault/sealedChat.ts`,
    `useSealedConversation`). Live frames to the admitted sockets still carry the words in
    transit; only what is stored is unreadable to canopy-web. Which socket is a chat surface
    is read from the session STORE at the handshake (`channels_auth`): a cookie minted from a
    token is `auth_method="minted"`, never "session", and `scope_is_chat_surface` refuses it. Every other reader — search,
    human inputs, a session review, an agent's or the fleet's reads, an admin who is not in
    it — reads the metadata columns. The reply reaches the asker the one way every turn's
    does: its transcript rows. A confined turn has no binding, so its box tails the turn's
    own transcript while it runs (`_TurnTail`, every ~2 s, one row per message or tool call as
    Claude writes it — never token by token) and ships through the unbound route
    (`session_sync.ingest_turn_end_rows`), then ships the whole transcript once more at the
    end, before deleting it. Every ship of one transcript lands in the same epoch, and a row
    already held is neither stored nor fanned out again, so the final ship adds nothing the
    live tail sent. A sensitive-data box still runs no `/streams` tail or backfill.
  - *An agent's own login.* It holds no vault key, so it cannot start or read one —
    unless an admin of the agent turned on its TEST vault key, which receives copies only
    of the conversations that login is itself in (`docs/architecture/vault.md` § An agent
    login's test vault key). A harness signed in as the agent then reads them exactly as a
    person does: the passphrase unlock in its browser.
  - *Who may send into one.* Its own people on its chat surfaces, the read gate
    (`reads_content`, asked in `send_paths.send_from` for every send path: REST, socket, contact,
    app view, MCP, Slack) — 403 `NOT_IN_IT_SEND` otherwise, `forbidden` on the socket.
    Whoever may not read the answer may not steer the agent either. A leg that only lets
    someone see one exists (an agent's admins, a co-tenant of a runner-discovered one)
    gives them VIEWER, never owner or editor (`access.role_for`), and nobody becomes its
    key's holder by sending: the key is its creator's (`require_key`), so a conversation
    nobody started stays keyless and keeps no words.
  - *Who may hold one.* A canopy user with a vault key. A contact, or a starter with none,
    is refused in a sentence ("a sensitive conversation needs a canopy account with a vault
    key; set yours up at …") at create (422) and at every send path (`CannotHold`: 422 on
    REST, `session.error` `cannot_hold` on the socket). A send that would MAKE an older
    conversation sensitive is refused before the switch, in its transaction
    (`add_runner_requirements(..., refuse_keyless=True)` from `send_paths.send_from`): all or
    nothing, so a refused send never leaves it private with its history blanked. Rows an agent ships into a keyless
    sensitive conversation keep no words at all.
  - *Multiplayer.* Adding someone is a browser act by someone in it: their browser unwraps
    its copy and wraps it to the newcomer's current vault key (`POST …/participants` with
    `wrap`; the server checks the shape and `recipient_jkt`). The key the server names for
    them is never taken on its word: the browser wraps only to a key it already trusts for
    that person (pinned in this browser) or one the adder accepts now, having seen its
    fingerprint (`trust.trustPerson`, `PersonTrustPrompt`; then pinned) — the ZK vault's
    rule. A key that does not match its thumbprint stops the add with a sentence. A key
    that changed under a pinned person is a STOP: the prompt shows no new fingerprint and
    no button that continues until the adder types the fingerprint the person read them
    by another channel (`fingerprintMatches`). So does an adder's own copy gone stale ("ask someone else in it
    to give you a fresh copy"). Removing someone deletes their wrap; the key is not rotated
    (they could have kept what they read). Nothing else makes
    anyone one of its people: `participants.ensure_participant` adds nobody to a sensitive
    conversation who is not its creator and holds no wrap of its key, so no entry point — a Slack
    reply, a dispatch's asker — can. Asking to join is once an hour per conversation
    (`join_requests.TooSoon`, 429; `CANOPY_JOIN_REQUEST_REASK_SECONDS`).
  - *Given it before it became sensitive.* A participant with no copy for their current
    vault key is listed `needs_key` (People); an editor or the owner in it holding a current
    copy gives them one in a click (`POST …/key-copies`, the same trust rule; 403 for a
    viewer). A copy is written only where the person holds none for their CURRENT vault key
    (`conversation_keys.add_wrap` → `CopyExists`, 409): the server cannot tell a good wrap
    from random bytes, so an overwrite could lock anyone out, the owner included. A copy
    for an older vault key is provably stale (its `recipient_jkt` names a key they no
    longer have) and is replaced; a current one that does not open is set aside by its
    holder alone (`DELETE …/key-copies/mine`, the banner's "Set my copy aside"), after
    which they are `needs_key` again. A re-add or role change keeps a current copy. Until they hold a copy the
    live socket, whose frames carry the words, turns them away (`sensitivity.hears_live`,
    asked at connect and on every re-check; `session.error` `needs_key`).
  - *A new vault key.* Settings → Security → Create a new vault key needs no passphrase,
    Secret Key or passkey that opens the current key, and deletes nothing stored in any vault. One screen lists
    every vault the person holds a copy of, who can let them back in, and any only they can
    open (`vault/newKeyImpact.ts`). If they are in private conversations it asks whether they
    can unlock the current key. If so, it opens every copy they hold (`GET /my-keys`), sets
    the new key, and stores each copy re-wrapped to it (`PUT /my-keys`;
    `vault/conversationCopies.ts`). If not, the new key is set without them. A passkey with no
    PRF output seals nothing, so the new key is sealed by a new passphrase and Secret Key
    instead. A copy that was not carried, or a starter who replaced their key elsewhere, is
    refreshed by an editor in it (`POST …/key-copies` takes the starter too, since their copy
    is provably stale).
  - *Rows from before it became sensitive.* Sealed in the transaction that makes it
    sensitive (`services.add_runner_requirements` → `conversation_keys.seal_existing`): the
    server holds the conversation's PUBLIC key, so it seals what it can no longer read back,
    clears the columns, and drops a tool's result; an ended turn's prompt is wiped and its
    note withheld, its events reduced to metadata, its raw transcript deleted, and the
    binding's tail, summary and dialog words cleared (`_withhold_what_it_kept`). A live
    turn keeps its prompt until it ends. "Its turns" are every turn whose conversation it
    is (`sensitivity.conversation_of`, `turns_of_q`): a chat turn's `chat_session`, and an
    agent, board or email turn's `canopy_session` — which is also in what a turn requires
    of its runner (`runner_requirements.requirements_of`), so such a turn routes only to a
    sensitive-data box, stores metadata and loses its prompt at its end. Those turn rows
    are locked by the switch, and `ledger.append_events` reads sensitivity under the same
    lock from fresh rows, so an event written as it switches is reduced or stored as
    metadata, never kept in clear; a keystroke racing it (`drafts.update_draft`) asks again
    under the session row's lock. Every file attached to it, sent or not, is deleted in
    the same transaction (rows now, stored bytes on commit, `services._drop_attachments`):
    it was kept in clear with its name, and sealing it would need a browser holding the key
    at the switch, which a send the server handles has not got — so it is not carried in.
    A send there claims only a sealed file (see *Files*). A FORK made while it was ordinary holds a copy of the
    same words, so it becomes sensitive with it, recursively (its rows sealed to the key
    its forker carries on); any other child keeps its own state.
  - *Every row is a `cc1:` envelope.* No sealed row is under canopy's field key, and canopy-web
    has no code that would open one; `readable` still hands out only `cc1:` envelopes.
  - *In the browser.* A quiet "Private" line under the header says so on the chat page and
    the panel. The unlock banner shows only when sealed rows are in view (history, scroll-back),
    never for a conversation whose messages all arrived live; its one click goes straight to
    the passkey (`unlockLocally`, `useVaultUnlock`), and a passphrase is asked only when the
    passkey is absent or failed. What arrived live is kept in the tab by place (turn index +
    role), so a reconnect's snapshot — the same rows, sealed — still reads without an unlock
    (`useSealedConversation`); a row with only its metadata reads "Sealing…". An MCP Apps View
    loads again, in a fresh frame, once the key opens (`AppHost.inputRevision`), so its input
    is the decrypted one.
  - *The keyset and the host.* `sealed_key.keyset` (the reader's vault public key and its
    seals) goes only to the person's browser: canopy's own signed-in page, or the panel's
    frame, whose fetches are `Sec-Fetch-Site: same-origin` (`sensitivity.request_is_panel_browser`).
    A site's backend calling canopy with the same delegated token gets `keyset: null`. The
    trust boundary: a host site vouches for who its visitor is, so it can always act
    as them and read what canopy hands them — ciphertext and their wrap, never anything that
    opens it. Their seals would let a site that forged the browser's header try passphrases
    offline (a passkey seal cannot be attacked so); the header keeps them out of every
    ordinary server-to-server call, and is not a proof against a site that forges it.
    **A host site's backend hears live words.** It holds the visitor's delegated
    token, so it can open the chat socket or send as them, and the socket's live frames
    carry the words in transit: sealing protects what canopy-web STORES, not a site the
    person chose to use canopy through. Only connect a site you would trust with the
    conversation.
  - *Drafts stay in the browser.* A sensitive conversation stores no draft: the keystroke
    path stores nothing there (`drafts.update_draft` → None), the composer never mirrors
    keystrokes (canopy-ui `localDrafts`), the text reaches canopy-web only inside the send,
    and peers see no typing preview. Becoming sensitive empties every draft
    (`drafts.forget_words`); a body kept under the field key before reads as nothing.
  - *Files: the agent's copy for one turn, the people's sealed* (`private_attachments`).
    The agent reads a file on its box, which holds no key to the conversation, so a file is
    treated like the prompt. Its people's browser — canopy's chat page, or the embedded
    panel's frame (`api._from_its_peoples_browser`: a delegated token only with
    `Sec-Fetch-Site: same-origin`, the keyset's rule, `request_is_panel_browser`); a token,
    MCP, a minted session, a contact or a site's backend gets 422 — uploads it in clear for one turn, under
    a key that names no file (`plain_key`), the row keeping a size bucket and a type family;
    then seals the name, type and size (`cf1:`) and the padded bytes (`cfb1`) to the
    conversation's public key with the message construction, bound to the conversation, the
    attachment and its uploader (`vault/sealedFiles.ts`, `POST …/attachments/{id}/sealed`;
    canopy-web checks the shape, once, from the uploader, before the send). A send claims
    only a file whose sealed copy is stored, and the turn's `origin_ref` names it by id
    alone (`send.claim_pending_attachments`, `bind_turn`). The runner's download route serves
    it ONCE, to the box holding that turn, deleting object, name and key as it does
    (`take_for_runner`); the turn's end (`close_turn` → `forget_turn`, so the unrun deadline
    too) and the sensitive sweep (`private_attachments.sweep`, its own tick and the retention
    sweep) delete any copy not fetched, and an unsent file whole. The plaintext route
    (`/attachments/{id}/content`) serves it to nobody. Its people read `GET
    …/sealed-attachments` (no name; each placed at the line it was sent with) and the sealed
    bytes (`GET …/attachments/{id}/sealed`), through `reads_content`, and open both in the
    browser: "Attachment" under the line until the key opens, then the name, a preview and a
    download (`SealedAttachments`, canopy-ui `AttachmentHost`). On the sensitive-data box the
    file lands in the turn's own directory (`<cwd>/.canopy-attachments/<turn>/`, owner-only),
    readable by that turn alone (`sensitive_data_settings(attachment_dir=…)`, and the
    profile's `attachment_dirs`, which profile_guard ≥ 4 — canopy 0.2.781 — lets Read open
    and nothing else; the profile's own tools and read_paths are unchanged, and an older
    guard gets no file, named as not available), and goes when the turn ends
    (`_sensitive_data_forget_turn`); what the agent read of it survives only in the kept
    session's transcript, which lives no longer than the kept conversation. The transcript
    the box ships back names it only inside sealed words (the user row's note, a Read
    call's path), never in a readable column.
- **Memory is off, both ways:** nothing is written (`hcp.refuse_if_sensitive`), and nothing is
  read into one — `hcp.memory_state` reads every feature as not effective in a sensitive
  session, `hcp.effective` / `hcp.require` refuse a sensitive turn (so the envelope's `person`
  runs no search, whose query would keep the turn's words, and `hcp_searchPreferences` is a
  403), and the person cannot switch it on or grant it there. The Learn/Use switches are not
  offered in one (`tests/test_hcp.py`).
- **Live and log routes ask the same gate:** the chat socket (`SessionConsumer`), whose live
  frames carry the words, admits only `reads_content`; the turn socket's tail gets the stored
  metadata; and `turn_access.can_read_turn_content` is False for a sensitive turn, so no turn
  read (REST, MCP, the board, the unclaimable report, the caller envelope) returns its
  prompt, note or report while it runs. The `TURN_CREATED` log line keeps no prompt head.
  A sensitive turn no runner has started (QUEUED or CLAIMED) is closed MISSED through
  `close_turn`, prompt wiped, `CANOPY_SENSITIVE_TURN_DEADLINE_SECONDS` (1 h) after it could
  first RUN (`harness.sweeps.expire_unrun_sensitive_turns`, its own tick, and the
  sensitive sweep): never while another turn of its conversation is running, and then
  from when the last turn before it that ran ended, or when it was asked if later. Session info (`GET /{id}/info`) shows an allowlist of metadata
  (`api.INFO_METADATA_KEYS`) and, for a sensitive one, no box Claude session or summary.
  Its "done" push says "Reply ready" or "The turn failed", nothing more.
- **Only its people hand it on:** adding a participant (which needs their wrap, made in a
  browser already in it) and forking (which copies the sealed words, the key and the wraps)
  need `reads_content` too, so an agent's admin who may open a thread they
  are not in cannot add themselves or copy it. A fork is the OWNER's act as well
  (`access.can_share`): it carries the key, the wraps and the agent's memory into a
  conversation its forker owns, so an editor in it cannot. A fork is canopy's to make:
  `metadata.fork` is server-owned (create takes only an allowlist of host keys,
  `services.HOST_METADATA_KEYS` and `x_…`), and what the runner is told
  (`placement.fork_plan`) is read from canopy's own columns — `worktree_of` only within
  the fork's lineage up `parent_session` (`fork_lineage`, which the cloud runner checks
  before running in another chat's directory), the cut only from the recorded
  `parent_claude_session`. No member can aim their turn at someone else's worktree,
  where their transcripts sit.
- **Nothing from outside its people:** Slack takes no message into one — a reply in its
  thread, a mention, a branch, a channel read, even from its owner — and says so with the
  link (`slack.services.private_refusal`); an email in its thread runs as the agent's own
  turn beside it (`channels.email.arrive`). Nor is it moved from Slack: "Run on cloud" and
  `/canopy cloud` leave a private conversation as it is and say so (`_move_to_cloud`),
  since its stranded turn could only be re-asked by Slack sending into it.
- **The agent's memory:** every turn of a sensitive conversation runs confined to the same
  host site, so it resumes the Claude session of the conversation's previous turn when it
  runs on the same sensitive-data box, in the same host site for the same agent, and the
  box still holds the transcript. The box records which session belongs to which
  conversation itself (`~/.canopy/kept-conversations/<session>.json`, runner/ec2
  `kept_resume` / `keep_conversation`), told by the claim that the turn is in one
  (`ClaimedTurnOut.sensitive_conversation`); it keeps only that session's transcript and
  sidecar, until the conversation has been idle 24 hours or is closed (the box asks
  `POST /runners/{id}/kept-conversations`; a 401, 403 or 404 there, a retired box among
  them, forgets them all, and so does the box's own shutdown). Otherwise the turn starts fresh and is told so
  (`conversation.starts_fresh`, which the runner adds to the envelope and the canopy
  plugin's hook renders): canopy-web never reads the sealed words back to hand them on,
  so the envelope carries no history. The resumed turn's ship sends only the records after
  the resume point (and after what its live tail already landed), in chunks, under the same
  id, retrying a failed post (`session_sync.ingest_turn_end_rows` keeps each row once). No
  turn's read tools reach another conversation's kept words: they are scoped to the turn's
  own directory and envelope, and every transcript, the kept-conversation records and the
  other per-turn state under `~/.canopy` are denied by name (`sensitive_data_settings`). A
  member's `who_is_asking` is gated like its REST twin (`can_read_turn_content`), so a
  sensitive turn is "turn not found".
- **No route around the gate** (`tests/test_sensitive_read_routes.py`,
  `tests/test_sealed_server_routes.py`): a session minted from a
  token is refused by the app-view routes (`app_views_api.browser_only`) and the panel's
  own token (`embed_api.embed_self_token`), both via `is_machine`, and by the chat socket
  (`auth_method="minted"`); every page route — its state and actions, declared, listed,
  invoked and answered (`/{id}/page-state`, `/run-input`, `/page-actions…`; MCP
  `read_page_state`, `list_page_actions`, `invoke_page_action`) — asks `reads_content`
  (`api._page_in_it_or_403`), `current_page` and the `page_*` tools over MCP leave a
  sensitive one out (the turn in it still reads and acts on its own, by its caller token
  or chat key, asked again at the call), an action's arguments and answer are not kept on
  its row (`page_actions._forget`), and the page is forgotten when its
  last live turn ends (`turn_close._forget_the_page`), or after
  `CANOPY_SENSITIVE_PAGE_TTL_SECONDS` (15 min) with no live turn
  (`page_state.forget_idle_sensitive_pages`, a tick); the unbound stream route
  (`api_session_sync._turn_end_session`) takes only the confined turn's own transcript
  (the first it ships, stamped on `Turn.session_key`) from a box that may still hold the
  agent (`runner_may_hold_agent`); a list reads who is in which once per page
  (`sensitivity.people_in`); a View's `tool_input`
  asks the request's real surface (`Viewer.chat_surface`); attachment bytes ask
  `reads_content` (404 otherwise).
- **A blocked agent's dialog is metadata:** `RunnerBinding.pending_question` keeps source,
  stamp, numbered options and a digest, never the question or option words
  (`sensitivity.menu_metadata`, both producers in `session_sync`); its push says only that
  it "needs your answer", and it is not relayed to Slack.
- **The socket re-asks:** `access.recheck` also asks `reads_content`, and is sent to every
  socket on a session that becomes sensitive (`services.add_runner_requirements`) and to a
  removed participant's (`services.recheck_sockets`).
- **No title:** create drops one, becoming sensitive clears one, `PUT …/title` is 422
  (`sensitivity.NO_TITLE`; clearing is allowed), and the `SESSION_CREATED` line keeps none.
- **Who a site may say a visitor is takes a passkey:** a signed `email_verified` claim
  resolves to a member, whose token reads their sensitive chats, so registering a host
  site or changing its public keys, JWKS URL or issuer needs a fresh step-up; a token is
  refused (`embedding/host_sites_api.py`).
- **The chat page says so:** `SessionOut.sensitive` draws a banner and turns off Share to
  Slack.
- **MCP Apps:** which View a call has is decided from its metadata columns alone (call id,
  tool name, the host tool a gateway call reached: `sensitivity.call_of`); a View gets
  `tool_result: null` and fetches its data as the viewer from its input, which the server
  sends sealed (`tool_input_sealed`, `sensitivity.sealed_call_for`) and the browser decrypts
  and hands it; a receipt keeps no excerpt;
  `ui/update-model-context` is accepted and not kept (`apps/mcp_apps_host/mcp_apps_views.py`).
- **Never re-exposed:** share-link upload (403), Slack share / new thread (403) and
  mirroring (`slack.relay`), export (403).
- **Never re-processed:** auto-debug starts nothing for a turn with any requirement; the
  fleet's transcript readers find none to read.
- **A sensitive turn's token reaches a fixed few tools:** its caller token's tools are the
  host site's globs ∩ `sensitive_data.SENSITIVE_CANOPY_TOOLS` (`current_page`,
  `who_is_asking`, `site_tools`, `site_call`, `site_read`) plus its own page's actions
  (`page_*`, `sensitive_data.sensitive_tool`: each runs in its person's tab and answers
  only the turn, so nothing leaves its people), whatever the host site's profile lists; it resolves to
  nothing once the host site stops saying `data: sensitive` (re-read on every call) or the
  turn is not confined (`caller_tokens._sensitive_grant`, `mcp/turn_scope.py`). So a
  sensitive turn creates no schedule, task or turn of its own.
- **Lineage is never dropped:** a caller token's own turn is the parent of anything its
  request starts, whatever a header or payload names (`request_context.from_request`,
  `provenance.requested_parent`); a payload `parent` that names nothing is a 422
  (`provenance.UnknownParentError`). A session started from a sensitive turn or
  conversation — a `parent`, a Slack branch, a transcript successor — takes its
  requirements at birth (`provenance.inherit_requirements`); a turn started from one makes
  the conversation it joins sensitive (`sensitivity.take_on`, at enqueue). A queued turn
  whose recorded parent has since been deleted is unsatisfiable
  (`runner_requirements.lost_lineage`).
- **One sealing decision:** every writer of `Message` rows (`send.py`, `transcript_ingest`)
  asks `sensitivity.seals(session, turn)` on the locked session, which first raises it to
  the turn's requirements — so what is sealed and what `reads_content` admits agree.
- **Elsewhere:** a turn refused at creation keeps no prompt (`services._apply_capability`);
  forced retention reaches a session-less turn sensitive through any ancestor
  (`retention.services._sweep_sensitive`); a draft's body is stored encrypted
  (`drafts.SEALED_PREFIX`) and reaches only a socket `reads_content` admits. Tests:
  `tests/test_sensitive_lineage.py`.
