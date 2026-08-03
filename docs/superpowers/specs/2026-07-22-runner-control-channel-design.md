# Runner control channel — one real-time layer for humans and runners

> **Status: shipped — historical record, not current-state.** `ws/runner/{id}/` carries wake/cancel/menu_answer/close_session in production.
> Kept for the reasoning; check `CLAUDE.md` and the code for how it behaves today.

**Status:** Draft for review · **Date:** 2026-07-22 · **Author:** Jonathan + Claude

> Replace the runner's poll-for-work loop with a **persistent WebSocket control
> channel** to canopy-web, so the whole system is one coherent real-time layer:
> humans and runners both connect to canopy-web over WebSockets, and canopy-web
> bridges them. This is what near-real-time chat and multiplayer require — a human
> message must reach a *running* agent live, which polling can't do well.

## Why WebSocket (and not long-poll)

Long-poll is the right pattern for **batch** dispatch (Temporal, GitHub Actions,
k8s-watch): a worker grabs a task and runs it to completion with no human in the
loop. Our target is the opposite — **interactive, multiplayer sessions** where
humans watch and interject *while the agent runs*. The distinguishing move is a
**human message injected mid-turn** reaching the agent's runner:

- **Agent → humans** already works over WS (turn events fan out through the
  turn/session group to every participant — SP1–3).
- **Humans → running agent** is the hard part. With poll/REST the runner would have
  to poll for new session messages mid-turn — laggy. A persistent WS pushes the
  message down instantly.
- **Presence + liveness** fall out of connection state for free (disconnect =
  instant), instead of heartbeat polling.

So WebSocket wins on the exact axes multiplayer needs. Durability stays in
Postgres: the socket is the *transport*; `Turn` rows + `claim_next_turn` + the lease
remain the *source of truth*. A dropped socket loses no work — reconnect, DB intact,
lease sweep backstops. WebSocket for real-time, DB for durability — both, not either.

## Architecture: canopy-web as the real-time bridge

Everything on the existing Channels layer (`apps/realtime`):

```
 humans ──WS──▶ canopy-web (Channels) ◀──WS── runners
   │  chat / multiplayer / transcript / presence   │  dispatch / events / interject / heartbeat
   └───────────── one real-time hub ───────────────┘
                        │
                   Postgres (Turn rows + lease = durable truth)
```

- **Runner ↔ canopy-web:** a `RunnerConsumer` WebSocket at `/ws/runner/{id}/`,
  PAT-authed (reusing `channels_auth`), that joins the runner's groups: `runner.{id}`
  and `runnable.{ws}` for each workspace the runner serves.
- **Server → runner frames:** `wake` (a turn is claimable — go claim), `interject`
  (a human message for a running session), `cancel`.
- **Runner → server frames:** `heartbeat`, `claim` (request the next turn), `event`
  (append turn events), `finish`. These map onto the same service functions the REST
  routes call, so the two surfaces can't drift.
- **Dispatch:** `enqueue_turn` publishes to `runnable.{ws}` (the wake group). The
  consumer relays `wake` to its runner; the runner claims via `claim_next_turn`
  (which still does all capability/tenant/preference gating and the atomic lease).
  The wake only *prompts* — a lost frame is covered by a slow heartbeat re-poll.

### Pull-on-wake, not server-push-assign (for now)

The wake tells the runner "attempt a claim"; the runner pulls via the existing,
tested `claim_next_turn`. We keep the delicate claim logic as the single source of
routing truth rather than moving the decision server-side. (A later evolution can
make the server pick the best *connected* runner and push-assign — which would make
the preference truly availability-based and retire the head-start timer — but that
is a follow-on, not this slice.)

## Decomposition

```
RC1  Server RunnerConsumer + wake     WS consumer (PAT auth, groups), enqueue→runnable
     (canopy-web)                     wake, claim/heartbeat/event/finish over WS mapping
                                      the same services. Tested with WebsocketCommunicator.
RC2  Cloud runner WS client           cloud_runner.py opens the WS (websockets lib),
                                      claims on wake, streams events up, heartbeats.
                                      REST claim/lease kept as durable fallback.
RC3  Laptop runner WS client          the emdash/canopy_runner path onto the same channel.
RC4  Human interjection               a session message for a RUNNING turn pushes down
                                      to the assigned runner; the agent sees it live.
RC5  Migrate + retire polling         heartbeat/claim polling → heartbeat-only fallback;
                                      liveness from connection state.
```
**Sequencing:** RC1 (foundation) → RC2 → RC4 (the multiplayer payoff) → RC3 → RC5.

## RC1 — the foundation (this slice)
- `apps/realtime`: `RunnerConsumer` (`AsyncJsonWebsocketConsumer`), routing entry,
  PAT auth. Joins `runner.{id}` + `runnable.{ws}` groups.
- `groups.runnable_group(ws)` (added) + a receiver publishing a `wake` on enqueue.
- Frame handlers that call the SAME `apps/harness/services` functions as REST
  (`claim_next_turn`, `heartbeat`, `append_events`, `finish_turn`), so no drift.
- Tests with Channels `WebsocketCommunicator` (InMemory layer): connect+auth,
  wake-on-enqueue reaches the socket, claim-over-WS returns a turn.

Out of scope for RC1: the runner-side WS clients (RC2/RC3), human interjection
(RC4), retiring the REST poll (RC5). REST claim/heartbeat stay fully working
alongside, so nothing regresses while the channel is built out.

## Non-goals
- Not moving durable state off Postgres — the socket never becomes the source of
  truth; a reconnect always reconciles against the DB.
- Not server-push-assign yet (RC1 is pull-on-wake); the head-start preference stays
  until push-assign makes availability exact.
