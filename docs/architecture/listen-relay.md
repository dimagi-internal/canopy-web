# Listen relay: record a meeting from a phone, transcribe it on your laptop

A person's phone records; their laptop, running canopy-listen, transcribes with open models
and runs Meeting assistant. Audio goes phone → canopy-web → the laptop's runner →
canopy-listen, **end-to-end encrypted**: canopy-web and the runner carry ciphertext only.
They can't read the audio or the transcript, pose as the phone, or pose as the laptop.
That holds while the `/listen` page itself is trusted (see "What this protects against"
under Crypto).

```
phone (canopy-web /listen page)
  ⇅ wss /ws/listen/<sid>/?role=phone           ciphertext frames
canopy-web (apps/listen): relays, never decrypts, never stores frames
  ⇅ wss /ws/listen/<sid>/?role=laptop          ciphertext frames
runner on the laptop: a dumb pipe (runner/canopy_runner/canopy_runner/listen_pipe.py)
  ⇅ unix socket ~/.canopy-listen/serve.sock    ciphertext frames
canopy-listen serve: decrypts, records, transcribes, starts Meeting assistant
```

The same `serve` daemon also serves the laptop's own page, at http://127.0.0.1:7351.

## Crypto

The spec is the docstring of `packages/canopy_listen/canopy_listen/relaycrypto.py`. The
phone side is `frontend/src/listen/relayCrypto.ts`. Both are tested against
`packages/canopy_listen/tests/relay_vectors.json`, which holds fixed keys, derived keys and
exact frames. Summary:
- ECDH P-256 (WebCrypto everywhere); HKDF-SHA256; AES-256-GCM.
- Pairing happens once per browser, two ways:
  - **By code, from /listen itself** (the usual way, and the only one a laptop's own
    browser needs): pick the computer; it shows a dialog with six digits, and the person
    clicks Allow if /listen shows the same. The phone commits to its key (`hello.commit`)
    before it sees the laptop's, so a server that swaps keys has a one-in-a-million chance
    of matching codes. Handshake frames are `0x02 | JSON`, in the clear; the laptop's
    `paired` is sealed under the session keys.
  - **By QR**, from the laptop's local page: the URL fragment carries the laptop's public
    key and a one-time secret, and the fragment never reaches the server.
- Each session's key needs the phone's static key and the laptop's, so only the paired
  phone and that laptop can make it.
- Frames are `0x01 | counter(8, BE) | GCM(…)`, with AAD = session id; counters strictly
  increase, so a replayed frame is refused.
- **A replayed session can't start a meeting.** A session's keys come from the phone's
  keys and the session id alone, so a server that kept an old session's frames could
  replay all of them to the laptop.
  - To stop that, the laptop's `ready` carries a fresh 16-byte `nonce`, and `start` must
    carry the same nonce back, sealed.
  - A `start` with no nonce is refused with `failed`/`update_page`: it comes from a page
    older than the laptop, and reloading fixes it.
  - A `start` with the wrong nonce is ignored.
- **What this protects against, and what it doesn't.** It protects against a relay that
  is compromised or curious: canopy-web and the runner see only ciphertext. It doesn't
  protect against canopy-web serving a malicious `/listen` page, because that page holds
  the phone's key in the browser and displays the pairing code. Trusting the page means
  trusting canopy-web's code.

Plaintext messages (after decryption):
- 0x01 + int16 LE mono 16 kHz PCM: phone → laptop, about 0.5 s each.
- 0x02 + JSON, phone → laptop:
  - `{"type":"start","notice_accepted":true,"nonce":…}`: `nonce` is the one from `ready`.
  - `{"type":"end"}`: end and save.
  - `{"type":"discard"}`
  - `{"type":"rename","speaker":N,"name":"…"}`
  - `{"type":"resume","have":N}`: sent after a gap (its socket reopened, or the laptop came
    back), saying the phone has the first N segments. The laptop resends the rest, plus
    its current state, speakers, chat link and partial words.
  - pairing only: `{"type":"pair","phone_key":…,"label":…}`
- 0x02 + JSON, laptop → phone:
  - `{"type":"ready","name":laptop_name,"nonce":…}`: sent first; the phone sends no audio
    before it.
  - `{"type":"state","state":"starting|recording|ending|ended|failed","detail":…}`
    - For `ended`, the detail says how it ended:
      - `saved`: a `saved` message follows.
      - `not_saved`: Meeting assistant didn't save the meeting.
      - `discarded`
    - The phone shows "saved" only for `saved`.
    - For `failed`, the detail is a code or a sentence. `recorder_stopped` means the
      recorder died.
  - `{"type":"segments","from":N,"items":[{"start","end","speaker","text"}…]}`: final
    words.
    - Batches hold at most 20 segments or 20,000 characters, so the relay's message rate
      can't be exceeded.
    - The first item is segment N of the meeting; the phone skips any segments it already
      has.
    - The phone also reads `{"type":"segment",…}`, one segment at a time.
  - `{"type":"partial","text":…}`: words heard but not final yet, shown greyed after the
    transcript; each replaces the last, and `""` clears it. The recorder steps every second
    and starts listening before its models load, so nothing said while they load is lost.
  - `{"type":"speakers","speakers":[{"number":N,"name":…}]}`
  - `{"type":"chat","url":…}`
  - `{"type":"saved","url":…}`
  - pairing only: `{"type":"paired","name":…}` / `{"type":"refused","reason":…}`. When
    pairing by code, the reasons are:
    - `busy`: another dialog is open;
    - `declined`;
    - `wait`: a pairing was declined less than 30 s ago. Each attempt puts a dialog on the
      Mac, so attempts can't be repeated back to back.
- In the clear, `0x02 | {"type":"refused","reason":"unpaired"}`: the laptop doesn't know
  this phone's key. It was unpaired there, or the laptop's key changed. The phone then
  offers to pair again.

## canopy-web (`apps/listen`)

`ListenSession` holds, per session:
- `id` (UUID), `owner` → user, `runner` → `harness.Runner`, `kind` (`pair` | `meeting`);
- `hello`: JSON of public keys only, `{"phone_eph": b64u, "phone_key": b64u | null, "label": str}`,
  plus `commit` (a base64url SHA-256) on a pairing by code;
- `state` (`waiting` | `live` | `ended`), `end_reason`;
- `created_at`, `connected_at`, `ended_at`, `bytes_up`, `bytes_down`.

Frames are never stored or logged, not even their sizes per frame (only totals).

REST, for the signed-in person (session cookie or PAT):
- `GET /api/listen/runners`: their runners, each `{id, name, online}`. Online is
  `Runner.is_reachable`: the runner's daemon sent a heartbeat recently. It doesn't mean the
  control socket is up; when that socket is down, the runner's pending poll covers it.
- `POST /api/listen/sessions` `{runner_id, kind, hello}` → 201 `{id}`.
  - The runner must be theirs and live, or 404/409.
  - At most one non-ended `meeting` session per runner (409).
  - Then: `group_send(runner_group(runner), {"type": "runner.listen_start", "session_id", "kind", "hello"})`.
- `GET /api/listen/sessions/<id>` → `{id, kind, state, end_reason, created_at}`, owner only.
- `POST /api/listen/sessions/<id>/end` → ended (`end_reason="ended_by_owner"`), and both
  sockets are closed.

For the runner, authenticated as the existing runner routes are:
- `GET /api/runner/listen-sessions/pending` → the sessions this runner should be on, each
  `{session_id, runner_id, kind, hello}`:
  - `waiting` sessions created in the last 2 minutes;
  - `live` meetings whose laptop side has gone, so the runner can rejoin them.

  The runner polls this route every 15 s, or every 10 s while its control socket is down.
  It polls only if Canopy Listen is installed. Polling is needed for two reasons: the
  control frame is sent once and pub/sub may drop it, and a runner that restarts
  mid-meeting gets no new frame. A runner's own key reads its own runner's
  (`apps/harness/runner_auth.py` lets it reach this route and nothing else under listen); a
  person's token reads the runner named by `?runner_id=`, or all of theirs.

`RunnerConsumer.runner_listen_start` sends the runner
`{"type": "listen_start", "session_id", "kind", "hello"}`.

WebSocket `/ws/listen/<sid>/?role=phone|laptop`:
- **Who may connect:** the signed-in user must be the session's owner. The laptop
  authenticates with the runner's bearer, which resolves to the owner, as `/ws/runner` does;
  a runner's own key opens only the laptop side of sessions on that runner (the handshake
  puts its runner on `scope["runner_id"]`, `apps/realtime/channels_auth.py`). Anyone else is
  refused 4003 before the socket is accepted. One socket per role: a newer socket takes the
  role over, and the older one is closed 4009. The older socket is usually the same phone
  or laptop from before a network change, which still looks alive to the server. Which socket holds a role is on the row
  (`phone_channel`/`laptop_channel`), refreshed every 10 s; one silent for 30 s has gone
  (a web process that died never said so).
- **What gets relayed:** binary messages go to the other role, as they are. A binary
  message is at most 64 KiB, and a role may send at most 60 per second; going over closes
  that socket with 4008. A frame sent while the other role is not connected waits in that
  socket's memory, at most 10 s and 64 frames, and goes on when the other side arrives, so
  neither side's first message is lost to who connected first; it is never written down.
  Text from a client is ignored. Frames cross the channel layer as `bytes` in a
  `listen.frame` group message, which msgpack (the Redis layer) carries exactly.
- **Any web task:** the phone and the laptop may be on different web tasks or workers;
  the Redis pub/sub layer (`config/channel_layers.py`) carries frames and notices
  between them. A socket subscribes to its groups before it claims its role, so a peer
  arriving on another task in between can't be missed, and every beat re-reads whether
  the peer is there, as a backstop for a notice pub/sub didn't deliver.
- **What the server sends:** text frames, JSON from the server only:
  `{"type":"peer","role":…,"connected":bool}` whenever the other side connects or
  leaves, and `{"type":"ended","reason":…}`. The server never reads a binary message.
- **Session states:** both roles connected → `live` (`connected_at`). The session ends
  when a role has been gone for 3 minutes once live (`peer_gone`), when it is not live 2 minutes
  after creation (`not_connected`), or at 4 h (`max_duration`): `apps/listen/services.py::sweep`,
  a job on the off-request clock every 10 s. Ended rows (state and byte totals) are deleted
  after 30 days. Connecting to an ended session gets its `ended` notice, then a close.

## The phone (`/listen`)

`frontend/src/pages/ListenPage.tsx` over `frontend/src/listen/`:
- `store.ts`: the phone's static key (made once, private half non-extractable) and its
  pairing `{runner_id, laptop_pub, laptop_name}`, in IndexedDB.
- `pairLink.ts`: the `#pair=` fragment is moved to this tab's sessionStorage and stripped
  from the address first thing at startup (`main.tsx`), and again on a same-tab hash change.
- `relay.ts`: one session's socket.
  - Everything goes out through one queue and is sealed as it leaves, so counters always
    rise.
  - Control messages go ahead of waiting audio, except `end`, which follows it.
  - Audio waits before `ready`, while the socket is down, and while the laptop is away. Up
    to 3 minutes is kept, oldest dropped first.
  - When catching up, it sends at most 30 frames a second.
  - A dropped socket is reopened with backoff for up to 170 s. After any gap, the page
    sends `resume`.
  - The pairing frame is sent when the server says the laptop is connected.
- `audio.ts`: microphone (echo cancellation, noise suppression) → AudioWorklet → box-filter
  downsample to 16 kHz → int16 LE → 0.5 s chunks. Started inside the tap, as phones require.
- `controller.ts`: pairing and a meeting as classes the page drives; REST, socket, storage
  and microphone come in as `ListenDeps`. The notice must be acknowledged once before the
  first recording, and `start` says `notice_accepted`.
- Waiting for the laptop: after 15 s with no laptop on the session, the page says it hasn't
  answered yet (asleep, offline, Canopy not running) and keeps waiting. It gives up at
  100 s, which covers a runner reopening a control socket that went quiet (about 75 s).
  Once the laptop is connected, it has 45 s to say `ready`.
- The microphone: the page reads the permission before the tap and, if it is denied, says
  how to allow it in the browser's settings, because a page can't ask twice. While
  recording, the screen is kept on (wake lock). If the browser pauses the microphone
  anyway (screen locked, a call, another app), the page says so. Coming back to the page
  resumes it when the browser allows; otherwise a tap starts a new capture in the same
  session.

### A voice note (`/listen/voice`)

Meeting assistant adds Voice session to the account menu when its person turns it on in
the app's settings (`local_apps/README.md`, "Add to the account menu"). They pick an agent
there, and `VoiceNotePage.tsx` over `voiceNote.ts` does the rest:
- It uses the same pairing, session kind and relay as a meeting, so canopy-web and the
  runner can't tell a note from a meeting. Only the laptop and the phone know.
- The laptop's `ready` lists `modes`. The phone asks for a note only from a laptop that
  lists `note`, because an older one would take the start for a meeting's.
- The phone sends `start {mode: "note", nonce}`. No notice is asked for, because nobody
  else is recorded.
- Done sends `end`. Done before the laptop answers keeps the audio on the phone, and the
  end follows it once the laptop does.
- The laptop answers `state ending/transcribing`, then `note {text}` and
  `state ended/transcribed`. If it can't read it, it answers `state failed` with
  `no_words`, `transcribe_failed` or `transcribe_timeout`.
- The words come back into an editable box. Send opens a chat with the agent, as the
  person (`createSession` then `sendMessage`), with the words as the first message.
  Nothing reaches the agent before Send.
- A note stops at 10 minutes on both sides.

## Runner (`listen_pipe.py`)

On `listen_start`, or a pending session from the REST fallback, the runner:
1. Starts `canopy-listen serve` if it isn't running (`~/.local/bin/canopy-listen serve --detach`).
2. Connects `~/.canopy-listen/serve.sock` and sends one JSON line
   `{"op":"session","session_id","kind","hello"}`.
3. Opens `/ws/listen/<sid>/?role=laptop`.
4. Copies binary frames both ways. Over the socket each frame is length-prefixed
   (4 bytes, big-endian).
5. For a meeting, a dropped socket to canopy-web is reopened while the local socket stays
   open.
   - It retries with backoff for up to 170 s; frames from serve wait for the new socket.
   - It pings when idle, and gives up on a socket that has been silent for 75 s.
   - Close codes 4001, 4003 and 4009 are final.
6. When the server ends the session, it writes an empty frame to serve (the explicit end)
   and closes both sides. When serve closes, it closes the socket to canopy-web.
7. When it is the side that leaves, it first sends canopy-web the text
   `{"type":"bye","reason":code}` (`listen_closed`, `server_lost`, `closed`). The server
   logs the code, and only a code, so a failure on the laptop can be told apart from a
   network drop.

Serve's socket path follows `store.socket_path`'s rule: a home too deep for a unix socket
gets a short stand-in under the temp folder. The runner repeats that rule because it can't
import canopy-listen, and both test suites pin the same example.

The runner never holds a key.

## canopy-listen serve

- **A unix socket**, `~/.canopy-listen/serve.sock` (0600). The first line is a JSON op:
  - `session`: from the runner. Ciphertext frames follow, length-prefixed, both ways.
  - `audio`: from the recorder's `RelaySource`, `{"op":"audio","input":id}`. Serve then
    writes plaintext PCM frames, length-prefixed, for that input. They stay on this Mac.
  - `ping`.
- **An HTTP and WebSocket server on 127.0.0.1:7351**, for the local page:
  - the page itself;
  - live state and transcript;
  - Start, from the microphone or from a browser tab's audio (`getDisplayMedia` sends PCM
    up the local socket);
  - End, Discard, rename a speaker;
  - choose models;
  - pair a phone (with the QR code) and list or remove paired phones.
- **Keeping the Mac awake**: while a recorder runs it holds `caffeinate -i -s -w <its pid>`,
  so the Mac doesn't idle to sleep mid-meeting (closing the lid on battery still sleeps it).
- **A phone meeting outlives its pipe.** If the pipe closes without the empty frame (the
  runner restarted), serve keeps the session for 200 s so the runner can rejoin. It keeps
  the keys, counters, input and recorder.
  - The runner rejoins with the same session id and `phone_eph`.
  - Serve then carries on without sending a second `ready`, and resends from the phone's
    `resume`.
  - A frame that doesn't open is dropped on its own, because a reconnecting pipe may resend
    one. 200 of them end the session.
- **The laptop key** is `relay-key.json` (0600). If that file exists but can't be read, it
  is kept as `relay-key.json.unreadable-<time>` and a warning is logged before a new key is
  made, which means every phone must pair again.
- **A start that never records:**
  - After 240 s without becoming a recording, it reads as `failed`/`did_not_start`.
  - Its input is ended, and its `next-input.json` is removed.
  - A queued relay input carries `queued_at`. `canopy-listen start` ignores one older than
    5 minutes, so a dead phone is never recorded in place of the microphone.
- **A recorder that died** reads as `failed` / `recorder_stopped`: its pid is gone while
  `meeting.json` still says loading, recording or finishing.
- **A meeting**, from the page, a tab or the phone:
  1. Serve queues the input (`next-input.json {"relay": id}`, or nothing for the native
     microphone).
  2. It runs `canopy-runner app start meeting-assistant --accept-notice`. The person
     accepted the notice on the page or the phone.
  3. End runs `canopy-runner app end meeting-assistant`; Discard runs
     `canopy-runner app stop meeting-assistant`.
  4. It follows the active `meeting.json` and the app's `run.json`, and pushes
     segments, speakers, the chat link and state to the page and, encrypted, to the phone.
- **A voice note** starts no app and no meeting, so it never makes this Mac busy:
  - The audio is held in memory, at most 10 minutes.
  - At `end`, it is written to `notes/<random>.pcm` (0600, in a 0700 folder), and
    `canopy-listen _transcribe` reads it in its own process with the configured speech
    model. Long notes are read in pieces of up to 60 s, each cut at the quietest moment
    near its end (`note.py`).
  - The file is deleted as soon as it has been read. Nothing of the note is kept.
  - If the pipe drops while the note is being read, the words are kept, and sent again
    when the phone asks with `resume`.
  - The log records only the note's length and how many characters came back.
