# Runner requirements (ZDR first) — a host can require that its visitors' work runs only on zero-data-retention boxes

**Status:** design, approved in conversation 2026-09-30. Not built.
**Layers onto:** directed runner routing (2026-07-24), source rules (2026-07-27),
actor rules (2026-09-05), per-rule turn mode (2026-09-23), host grant contract v1
(`sdk/python/README.md`).

## The problem

connect-labs embeds canopy agents (ace, first). A visitor's conversation there can
carry PII — both what they type and what the agent reads back through the host's MCP
tools as that visitor (`site_call`). Where that conversation *runs* is decided by the
agent's routing ladder, which knows nothing about the data: a connect-labs visitor's
turn is an ordinary `canopy_web_chat` turn, and lands on whichever box ranks first —
possibly a laptop whose Claude login is a personal subscription that retains data.

What the author of connect-labs wants to be able to say, once, in their own
configuration: **"work for my visitors runs only on runners that use zero-data-retention
keys."** And what a runner's owner needs to be able to say: **"this box uses only ZDR
keys for Claude."**

## Vocabulary

**Runner requirements, and flags that satisfy them.** A host states a LIST of
requirements; a runner's owner declares a SET of flags; a runner may take a session's
work only when its flags include every requirement the session carries. `zdr` is the
first and, for now, only flag — the list shape exists so the next one (a region, a
data-handling tier) is a new value in the vocabulary, not a new claim, column or
routing rule.

**ZDR, not "secure".** The runner owner is vouching for exactly one thing — that the
box's Claude execution uses zero-data-retention keys. "Secure" would claim more than
anyone is vouching for, so the word does not appear in code, API or UI.

**One vocabulary, defined once.** `canopy_sdk.contract.RUNNER_FLAGS =
frozenset({"zdr"})` is the only list of known flags; canopy-web imports it for both
the runner declaration and requirement parsing, so the two cannot drift.

## What canopy can and cannot know

canopy cannot verify ZDR. It cannot see which key a box's Claude login uses, and a
box's self-report would be the box vouching for itself. So the model is two
attestations and one enforcement:

- the **host** declares the requirement (it owns the data);
- the **runner's owner** declares the box ZDR (they own the box);
- **canopy** enforces the conjunction at claim time and never degrades it.

The residual trust — that people linking runners to an agent only mark boxes that
really are ZDR — is accepted and stated in the UI. There is no way around it.

## 1. A runner declares its flags

New model `harness.RunnerFlag` — one row per flag a runner's owner has declared:

| field | type | meaning |
|---|---|---|
| `runner` | FK Runner, `CASCADE` | |
| `flag` | char, one of `RUNNER_FLAGS` | e.g. `zdr`: the owner attests this box uses only ZDR keys for Claude |
| `declared_by` | FK User, null, `SET_NULL` | who declared it |
| `declared_at` | datetime | when |

Unique on `(runner, flag)`. A table rather than a JSON list on `Runner` because each
flag is a separate attestation with its own "who said so", and a list column would
keep that only for the last change. Withdrawing a flag deletes its row (the Event
below is the history). `Runner.flags` (a property over the prefetched rows) is what
routing reads.

- **Who may set it:** the runner's owner (`paired_by`) or a `RunnerAdmin` — the same
  gate as the runner's other administration. Not a workspace owner as such, not an
  agent owner, and **never the runner itself**: the heartbeat/claim payloads do not
  carry it and the runner-protocol endpoints ignore it. A box must not be able to
  promote itself.
- **API:** `PUT /api/harness/runners/{rid}/flags` `{flags: ["zdr"]}` → the runner
  row (replace-the-set; adds and removes rows to match, stamping only the added ones;
  an unknown flag is a 422).
  Human-only (refused for `is_machine` callers, like runner-admin grants), because
  it is an attestation by a person. It is on MCP by construction (every route is);
  the `is_machine` refusal is what keeps an agent from declaring its own box.
- **Recorded as an Event** (`runner.flag_declared` / `runner.flag_withdrawn`, with
  the flag) naming
  the person, so "who said this box was ZDR?" has an answer after the fact.
- **Surfaces:** the runner's drawer in `/supervisor` → Runners gets the toggle with
  the sentence *"This box uses only zero-data-retention keys for Claude. You are
  vouching for this; canopy cannot check it."* (one checkbox per known flag, each with
its own sentence). `RunnerOut` gains `flags: list[str]`, and a small
  `ZDR` badge appears wherever runners are listed — notably each row of an agent's
  Routing table (`AgentRouting.tsx`), since that is where someone decides which
  boxes an agent may use.

Withdrawing a flag takes effect at the next claim. A turn already executing on the box
is not interrupted (claim-time check only — see §3).

## 2. A host declares the requirement

### In the SDK (`dimagi-canopy`, `canopy_sdk`)

- `canopy_sdk.contract` gains `RUNNER_REQUIREMENTS_CLAIM = "canopy_runner_requirements"`
  and `RUNNER_FLAGS = frozenset({"zdr"})`.
- `canopy_sdk.host` config gains `RUNNER_REQUIREMENTS`
  (`CANOPY_HOST["RUNNER_REQUIREMENTS"] = ["zdr"]`; default empty). When non-empty,
  `sign_visitor_assertion` adds `canopy_runner_requirements: ["zdr"]` to **every**
  visitor assertion it signs — member and contact alike. The host's author sets it
  once; they do not remember it per call. Empty → the claim is omitted.
- An unknown flag, or a non-list value, is refused at config load (the host learns at
  boot, not in prod).
- SDK version bump (required by the `Backend tests` guard on `sdk/python/src`), and
  the claim documented in `sdk/python/README.md` beside the other arrival claims.

**Why the signed assertion and not a setting on canopy's Connected site row:** the
assertion is the one channel only the host can write. A column in canopy would be
editable by a canopy workspace owner, who is not the party that owns the data, and
would be a second copy of the host's intent that can drift from it.

### In canopy, at arrival (`POST /api/auth/contact-token`)

- `assertions.verify` already returns the verified claims. Arrival reads
  `canopy_runner_requirements`; a malformed claim or any unrecognised flag **fails
  closed** (the arrival is refused
  with a named error) rather than being ignored, because ignoring it would silently
  drop a requirement the host believes it made.
- The list is stored on the token canopy mints — new `runner_requirements` JSON list
  on both `DelegatedToken` (member arrival) and `ContactToken` (contact arrival),
  normalised (deduped, sorted), default `[]`.
- **Every session created under that token** is stamped
  `metadata["runner_requirements"] = [...]`. Both creation paths:
  `contact_api.start_session` (contact) and `canopy_sessions/api.py::create_session`
  (the `acting_app` branch, member). Taken from the token, never the request body.
- `runner_requirements` is added to `SERVER_OWNED_METADATA`, so no caller — user, contact
  or host — can set or clear it through `host_metadata`, and
  `tests/test_session_metadata.py` pins its owner.
- The stamp **only grows**: a session's requirements are the UNION of every token
  that has touched it, and nothing removes one (a later token without `zdr` does not
  downgrade an existing conversation — the PII already in it does not go away).
- The Connected site row records the last value the host sent
  (`AppCredential.last_runner_requirements`, display only) so the Connected sites page
  can show *"Requires runners: ZDR (declared by the host)"*. It is informational; nothing
  routes on it.

## 3. Routing enforces it

**Rule:** a turn whose session carries `runner_requirements` may be claimed only by a
runner whose flags include every one of them (`set(requirements) <= runner.flags`).
There is no fallback and no override.

- **Where:** one predicate pair, `requirements_of(turn)` (reads
  `turn.session.metadata`) and `runner_satisfies(runner, requirements)`,
  applied inside the shared routing path so `claim_next_turn` and
  `unclaimable_queued_turns` cannot disagree — the same discipline that made
  `runner_target_q` shared.
- **How it composes with the ladder:** the ladder is unchanged — actor rule → source
  rule → default list, with pins and session stickiness above it — and the ZDR
  filter is applied to its *output*: runners missing a required flag are removed from the composed
  list before the availability cascade walks it. So a person's own rule still picks
  *which* ZDR box and in what order; it can no longer pick a non-ZDR one. Removed
  runners also stop counting as better-ranked blockers, and the 60s cascade grace has
  nobody non-ZDR to promote.
- **Pins and stickiness obey it too.** A `Turn.pinned_runner` that is not ZDR does
  not claim. A session bound (`RunnerBinding`) to a non-ZDR runner does not fail over
  on its own (stickiness semantics unchanged) — it waits, and the placement banner
  offers **only ZDR runners** as places to move it.
- **Enqueue is unaffected.** The turn queues normally; it simply has no eligible
  claimant until one exists.
- **Turn status:** `apps/harness/turn_status.py` gains the reason (worded from the
  missing flags; ZDR shown here) for the `unrouted`
  / `waiting_runner` states: *"Waiting for a ZDR runner — this site requires one, and
  none of ace's runners is declared ZDR"* (no ZDR runner in the list at all → the
  remedy is routing, i.e. `unrouted`) vs *"…ace's ZDR runners are offline"*
  (`waiting_runner`). Every channel (chat page, widget, Slack) renders it for free,
  since they all render `turn_status`.
- **Second check at the data seam (defence in depth):** `host_gateway.site_call`,
  which attaches the visitor's grant and is where the host's PII actually flows into
  a turn, refuses when the runner executing the turn does not satisfy the session's
  requirements. Claim routing should make this unreachable; the gateway check means a
  future routing bug fails as a refusal rather than a leak.

### Worked example

connect-labs sets `CANOPY_HOST["RUNNER_REQUIREMENTS"] = ["zdr"]`. ace's routing: default list
`[laptop, cloud-zdr]`, and an actor rule "jjackson@dimagi.com → laptop".

- A contact in the connect-labs widget asks ace something → the default list filtered
  to ZDR is `[cloud-zdr]` → cloud-zdr claims.
- Jonathan, arriving through the connect-labs widget → his actor rule gives `[laptop]`,
  then the default list; filtered: `[cloud-zdr]` → cloud-zdr claims. (If his rule
  were strict, the filtered list would be empty → the turn waits, with the
  "no ZDR runner" status.)
- Jonathan chatting with ace in canopy's own chat page → no ZDR requirement on that
  session → his rule applies as today → laptop.

## Out of scope

- **Verifying ZDR.** Not observable by canopy.
- **Agent- or workspace-level requirements** ("ace always requires ZDR"). Not asked
  for; the host is the party that owns the data. Easy to add later as another source
  of the same `runner_requirements` stamp.
- **Data that does not arrive through a visitor session** — e.g. an agent calling
  connect-labs tools with its *own* credentials from an unrelated turn. A host grant
  exists only inside the visitor's session, so the host's visitor-scoped data is
  covered; anything the agent can reach with its own credentials is the agent
  owner's decision, as today.
- **Interrupting executing turns** when a runner's ZDR is withdrawn.

## Tests

- **Runner declaration:** owner and runner-admin may set; an unknown flag 422s; a non-admin 404/403s; an
  `is_machine` caller is refused; runner-protocol endpoints cannot change it; an
  Event records who.
- **Arrival:** assertion with `canopy_runner_requirements: ["zdr"]` → token carries it → a session
  created with that token (contact and member paths) is stamped; an unknown flag or a
  non-list claim is refused; a request body cannot set or clear
  `runner_requirements`; a later token without the claim does not un-stamp an
  existing session.
- **Claim — one test per rung, each asserting a non-ZDR runner does not claim and a
  ZDR one does:** default list, source rule, actor rule, strict rule (empty after
  filter → waits), `pinned_runner`, bound session, cascade grace (a non-ZDR lower
  rank is never promoted).
- **Parity:** `unclaimable_queued_turns` reports the ZDR-required turn exactly when
  `claim_next_turn` refuses it (extend the existing parity pattern).
- **Turn status:** the two new reasons render.
- **Gateway:** `site_call` in a ZDR session on a non-ZDR runner is refused.
- **Vocabulary:** canopy-web has no flag list of its own — a test asserts the
  declaration API, arrival parsing and the UI's checkbox list all derive from
  `contract.RUNNER_FLAGS`.
- **Round trip:** `tests/test_sdk_round_trip.py` — the SDK's real host half with
  `RUNNER_REQUIREMENTS=["zdr"]` signs, canopy's real arrival stamps, routing enforces.

## Rollout

1. canopy-web: runner declaration + arrival stamping + routing filter + status +
   gateway check, and the SDK claim, in one PR (the SDK half is in this repo).
   Regenerate `generated.ts`; bump `dimagi-canopy`.
2. Declare the cloud runner's `zdr` flag (its owner), once its Claude login is confirmed ZDR.
3. connect-labs: bump its `dimagi-canopy` pin and set
   `CANOPY_HOST["RUNNER_REQUIREMENTS"] = ["zdr"]`.
   Until step 3, nothing changes for connect-labs visitors — the requirement is off
   until the host turns it on.
