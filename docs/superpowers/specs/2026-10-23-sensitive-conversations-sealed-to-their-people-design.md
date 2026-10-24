# Sensitive conversations sealed to their people — canopy-web can write a sensitive conversation but never read it back; the agent keeps its own session

**Status:** design, agreed with Jonathan 2026-10-23. Builds on
`2026-10-22-sensitive-data-agents-design.md` §4 (sealed words, one read gate).

## The problem

Today a sensitive conversation's words are kept in `Message.sealed`, encrypted with
canopy-web's own field key (`FIELD_ENCRYPTION_KEY`). Every normal door is closed
(`sensitivity.reads_content`), but anyone with production access to canopy-web can
decrypt them, and canopy-web itself decrypts them for two reasons:

1. to show them back to the conversation's people (REST, socket snapshot);
2. to hand the agent the conversation so far (`sensitivity.history` in the caller
   envelope), because every sensitive turn starts a fresh Claude session and the box
   deletes it afterwards.

Goal: canopy-web can **write** a sensitive conversation but can **never read it
back**. Only the conversation's people can, in their browsers, with their own keys.
Out of scope for now: someone with admin access to the sensitive-data box seeing a
conversation while it runs or rests on the box.

## 1. The agent keeps its session (removes reason 2)

Within one sensitive conversation every turn is confined to the same capability, for
the same people, so the rule "a confined turn is never resumed" (which protects
conversations that mix staff and outside callers, such as email threads) protects
nothing here.

- A confined turn **in a sensitive conversation** resumes the Claude session of the
  conversation's previous turn when it runs on the same box, in the same capability,
  and the box still holds it. Otherwise it starts fresh: canopy-web cannot supply
  history any more, and the agent is told it is starting fresh.
- The box keeps such a conversation's transcript (and only what `--resume` needs)
  until the conversation has been **idle for 24 hours** or is closed, then deletes
  it (the same deletion `_sensitive_data_forget_turn` does today, deferred). A box
  restart sweeps anything past its deadline. The caller envelope, profiles and
  scratch files are still deleted at the end of every turn.
- Kept data never outlives the box's right to it: a refused sweep ask (401/403/404,
  a retired runner), a retired runner's start and the box's own shutdown forget every
  kept conversation, and the sweep re-reads a record before deleting it so a turn
  that kept a newer session meanwhile loses nothing.
- No turn reads another conversation's kept session: a sensitive-data turn's read
  tools reach only its own directory, its own tool output and its own envelope, and
  every transcript, the kept records and the other per-turn state are denied by name.
  `profile_guard` refuses a payload it cannot parse.
- A resumed turn ships only what it added — the records after the resume point and
  after what its live tail landed — in chunks, with retries; the tail ticks once more
  as it stops, so the reply's last rows are never left behind.
- `sensitivity.history` and the envelope's `conversation.history` are removed; the
  canopy plugin hook ignores an absent field.

## 2. A key per conversation, wrapped to each of its people (removes reason 1)

- **The conversation keypair, made in the starter's browser.** A chat started from
  canopy's own chat surfaces carries a P-256 ECDH keypair the starter's browser made,
  its private half wrapped there to the starter's vault key (checked against the key
  that browser last opened as theirs); canopy-web keeps it even while the chat is
  ordinary and seals to it if it becomes sensitive, so it never sees the private half.
- **Where no browser is there** (a session made sensitive from Slack, MCP, a host's
  grant, or one created with no key): when its requirements gain `sensitive_data`,
  canopy-web makes a P-256 ECDH keypair for it. The public
  key is stored on the session (`SessionKey.public_jwk`). The private key is
  immediately **wrapped** (ECDH-ES + HKDF + AES-GCM, the vault's existing
  `wrapEcdhEs` construction, info `canopy-chat/v1/wrap|<session>|<user>`) to the
  starter's vault public key (`UserVaultKeyset.public_jwk`), and the plaintext
  private key is discarded. canopy-web saw it once because it made it — the same
  stance as `zk_seal` — and cannot read anything sealed to it afterwards.
- **Messages are sealed to the conversation's public key.** Each stored message's
  words (`Message.sealed`) are an ECIES envelope: a fresh ephemeral ECDH key, HKDF to
  an AES-256-GCM key, the payload `{plaintext, content}` encrypted with associated
  data `canopy-chat/v1/message|<session>|<turn_index>|<role>` (the role named as `r`
  too); the browser opens a row only at the index and as the speaker its envelope names
  (a fork's copied rows at a recorded offset). canopy-web holds only the
  public key, so it can **write** every new message (the person's send, the agent's
  transcript rows) and can **read none**. This is the write-only path: no shared
  symmetric key ever sits on the server.
- **Reading.** The read routes (REST, socket snapshot, contact routes, still gated by
  `reads_content`) return the ciphertext and the caller's own wrap. The browser
  unlocks the person's vault key (passkey, `useVaultUnlock`), unwraps the
  conversation private key, and decrypts each message. Live frames to the
  conversation's admitted sockets keep carrying the words in transit, as today; only
  what is **stored** becomes unreadable to canopy-web.
- **Multiplayer.** One keypair per conversation, wrapped once per person (no
  re-encryption of messages). Adding a participant to a sensitive conversation is a
  browser action by someone already in it: their browser unwraps the private key and
  wraps it to the new person's vault public key — only once that key is one the browser
  trusts for them (pinned, or its fingerprint accepted now: the ZK vault's rule) — and
  the server stores the new wrap (`SessionKeyWrap`). Someone given the conversation
  before it became sensitive, or whose vault key changed, is listed as needing the key;
  anyone in it gives them a copy the same way, and until then the live socket (whose
  frames carry the words) turns them away. Replacing your own vault key re-wraps every
  copy you hold to the new key while the old one is unlocked. Removing someone deletes
  their wrap. (Rotation on removal is
  not in scope: a removed person could have kept what they read.)
- **Who may start one.** Only a canopy user with a vault keyset. A contact (no
  canopy account) cannot start or hold a sensitive conversation for now: refused
  with a clear sentence. Designed for later: an authorized contact is just another
  recipient once contacts have a key, and nothing about the messages changes.
- **What canopy-web keeps able to read:** the metadata columns (kind, tool names,
  argument names, ids, sizes, hashes), turn status, the ledger (already metadata) and
  logs — everything troubleshooting needs.
- **What a sensitive conversation does not take.** A draft on the server (the composer
  keeps it in the browser and sends the text with the send).
- **Files: treated like the prompt, not like an archive.** The agent reads a file on its
  box, which holds no conversation key, so a file is two copies
  (`apps/canopy_sessions/private_attachments.py`):
  - *The agent's copy is transient.* The browser uploads it in clear for one turn, stored
    under a key that names no file, with a size bucket and a type family on the row. A
    send claims it only once the people's copy is stored, and the turn's `origin_ref`
    names it by id alone. The runner's download route serves it ONCE, to the box holding
    the claiming turn, and deletes object, name and key as it does; the turn's end
    (`close_turn`, the unrun deadline among its callers) and the sensitive sweep delete
    any copy not fetched. Never served to a person, MCP, a PAT or another box; no log
    line names it.
  - *On the box* it lands in the turn's own directory (`<cwd>/.canopy-attachments/<turn>/`,
    owner-only), readable by that turn alone (a Read rule in its locked settings, the same
    path in profile_guard's profile), and goes when the turn ends with the rest of its
    per-turn state. What the agent read of it survives only in the kept session's
    transcript, which lives no longer than the kept conversation.
  - *The people's copy is sealed in the uploader's browser* to the conversation's public
    key with the message construction (ECDH-ES + HKDF-SHA256 + AES-256-GCM), bound to the
    conversation, the attachment id and the uploader: the name, type and size as a `cf1:`
    envelope, the bytes, padded to within an eighth, as a `cfb1` blob
    (`docs/vault/chat-v1/file_v1_vectors.json` pins both). canopy-web checks the shape and
    cannot open either. Members see "Attachment" under the line until the key opens,
    then the name, a preview and a download, all decrypted in the browser.
  - *Becoming sensitive* still deletes every file the conversation held while ordinary:
    sealing them would need a browser holding the key at the moment of the switch, which
    happens on a send the server handles; what was attached in clear is not carried in.
  - *A fork* carries the sealed words, not the files (rows are per session).
- **Server-side consumers of the words, after this:**
  - MCP Apps: a View's `tool_input` is decrypted in the browser and handed to the
    View by the client (which loads the View again once the key opens); the server derives which View a call has from the metadata
    columns (tool name, host tool) only.
  - Fork: copies ciphertext and wraps as they are (same conversation).
  - Export, share, Slack, reviews, search: already refused or metadata-only.
- **Existing rows.** Rows sealed with the field key before this ships are
  re-sealed by a migration (the field key can still decrypt them) to each
  conversation's new public key, wrapped to its starter. A session whose starter has
  no vault keyset keeps field-key encryption and is listed as needing migration.

## Rollout

1. Agent session resume and idle deletion (runner + server), history removed.
2. Conversation keys, write-only sealing, browser decryption (chat page and the
   embedded panel), multiplayer wrap on add, the migration.
3. Red team: the canary must appear in nothing canopy-web can decrypt; a database
   read, a shell with the field key, and every API door yield only ciphertext and
   metadata.
