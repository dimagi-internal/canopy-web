# Listen relay: record a meeting from a phone, transcribe it on your laptop

A person's phone records; their laptop, running canopy-listen, transcribes with open models
and runs Meeting assistant. Audio goes phone → canopy-web → the laptop's runner →
canopy-listen, **end-to-end encrypted**: canopy-web and the runner carry ciphertext only.
They can't read the audio or the transcript, pose as the phone, or pose as the laptop.

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
- Pairing happens once, at the laptop. The QR code's URL fragment carries the laptop's
  public key and a one-time secret, and the fragment never reaches the server.
- Each session's key needs the phone's static key and the laptop's, so only the paired
  phone and that laptop can make it.
- Frames are `0x01 | counter(8, BE) | GCM(…)`, with AAD = session id; counters strictly
  increase, so a replayed frame is refused.

Plaintext messages (after decryption):
- 0x01 + int16 LE mono 16 kHz PCM: phone → laptop, about 0.5 s each.
- 0x02 + JSON, phone → laptop:
  - `{"type":"start","notice_accepted":true}`
  - `{"type":"end"}`: end and save.
  - `{"type":"discard"}`
  - `{"type":"rename","speaker":N,"name":"…"}`
  - pairing only: `{"type":"pair","phone_key":…,"label":…}`
- 0x02 + JSON, laptop → phone:
  - `{"type":"ready","name":laptop_name}`: sent first; the phone sends no audio before it.
  - `{"type":"state","state":"starting|recording|ending|ended|failed","detail":…}`
  - `{"type":"segment","start":…,"end":…,"speaker":N,"text":…}`
  - `{"type":"speakers","speakers":[{"number":N,"name":…}]}`
  - `{"type":"chat","url":…}`
  - `{"type":"saved","url":…}`
  - pairing only: `{"type":"paired","name":…}` / `{"type":"refused","reason":…}`

## canopy-web (`apps/listen`)

`ListenSession` holds, per session:
- `id` (UUID), `owner` → user, `runner` → `harness.Runner`, `kind` (`pair` | `meeting`);
- `hello`: JSON of public keys only, `{"phone_eph": b64u, "phone_key": b64u | null, "label": str}`;
- `state` (`waiting` | `live` | `ended`), `end_reason`;
- `created_at`, `connected_at`, `ended_at`, `bytes_up`, `bytes_down`.

Frames are never stored or logged, not even their sizes per frame (only totals).

REST, for the signed-in person (session cookie or PAT):
- `GET /api/listen/runners`: their runners, each `{id, name, online}`. Online is
  "connected to /ws/runner in the last minute", by the existing runner presence.
- `POST /api/listen/sessions` `{runner_id, kind, hello}` → 201 `{id}`.
  - The runner must be theirs and live, or 404/409.
  - At most one non-ended `meeting` session per runner (409).
  - Then: `group_send(runner_group(runner), {"type": "runner.listen_start", "session_id", "kind", "hello"})`.
- `GET /api/listen/sessions/<id>` → `{id, kind, state, end_reason, created_at}`, owner only.
- `POST /api/listen/sessions/<id>/end` → ended (`end_reason="ended_by_owner"`), and both
  sockets are closed.

For the runner, authenticated as the existing runner routes are:
- `GET /api/runner/listen-sessions/pending` → `waiting` sessions for this runner created in
  the last 2 minutes, each `{session_id, kind, hello}`. This is the fallback when the control
  socket is down.

`RunnerConsumer.runner_listen_start` sends the runner
`{"type": "listen_start", "session_id", "kind", "hello"}`.

WebSocket `/ws/listen/<sid>/?role=phone|laptop`:
- **Who may connect:** the signed-in user must be the session's owner. The laptop
  authenticates with the runner's bearer, which resolves to the owner, as `/ws/runner` does.
  One socket per role; a second is refused with close code 4009.
- **What gets relayed:** binary messages go to the other role, as they are. A binary
  message is at most 64 KiB, and a role may send at most 60 per second; going over closes
  that socket with 4008.
- **What the server sends:** text frames, JSON from the server only:
  `{"type":"peer","role":…,"connected":bool}` whenever the other side connects or
  leaves, and `{"type":"ended","reason":…}`. The server never reads a binary message.
- **Session states:** both roles connected → `live` (`connected_at`). The session ends
  when a role has been gone for 60 s once live, when nothing has connected 2 minutes after
  creation, or at 4 h.

## Runner (`listen_pipe.py`)

On `listen_start`, or a pending session from the REST fallback, the runner:
1. Starts `canopy-listen serve` if it isn't running (`~/.local/bin/canopy-listen serve --detach`).
2. Connects `~/.canopy-listen/serve.sock` and sends one JSON line
   `{"op":"session","session_id","kind","hello"}`.
3. Opens `/ws/listen/<sid>/?role=laptop`.
4. Copies binary frames both ways. Over the socket each frame is length-prefixed
   (4 bytes, big-endian).
5. Closes both sides when either closes.

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
- **A meeting**, from the page, a tab or the phone:
  1. Serve queues the input (`next-input.json {"relay": id}`, or nothing for the native
     microphone).
  2. It runs `canopy-runner app start meeting-assistant --accept-notice`. The person
     accepted the notice on the page or the phone.
  3. End runs `canopy-runner app end meeting-assistant`; Discard runs
     `canopy-runner app stop meeting-assistant`.
  4. It follows the active `meeting.json` and the app's `run.json`, and pushes
     segments, speakers, the chat link and state to the page and, encrypted, to the phone.
