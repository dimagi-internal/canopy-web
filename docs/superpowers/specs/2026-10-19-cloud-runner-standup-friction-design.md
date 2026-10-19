# Standing up a cloud runner without a person at every step

## Why

On 2026-10-18 a test box, `cloud-ec2-fork` (stack `canopy-cloud-runner-fork`), was
stood up to test chat forking: `runner/ec2/up.sh` → pairing approval → manual
credential staging. It took hours and needed a person at several steps that should
have been automatic or caught up front. Ten problems were hit. This design fixes all
ten. Three of them were product decisions, and Jonathan has made them (marked
**decided**).

The bar: an operator runs `./up.sh … && ./wire.sh …`. Every human step is either
asked for up front with the exact fix, or is one Touch ID click. Nothing the box
reports as healthy is actually broken. Nothing a half-configured box does reaches
another person's work.

## Goals

- A usage cap is reported as a cap everywhere (turn text, alert, readiness), with
  its reset time, never as "not signed in".
- `ready` and the runner page's credential dots mean "this credential just
  answered a prompt", not "a value is stored".
- Credential alerts from a box always land somewhere a person sees them.
- A new box pairs with no long-lived secret: no shared PAT, nothing to expire.
- A test box can be stood up that takes nothing it is not explicitly given.
- A project chat (no agent, just a repo) runs only on a box that has the repo, that
  the workspace shares or the chatting person administers.

## Non-goals

- Making full Claude logins travel between boxes. They cannot: their refresh token
  rotates (`docs/architecture/runners.md` → "A cloud runner's Claude login"). This
  design only makes the copy step and the page say so.
- Changing agent-chat routing, agent routing rules or the tenant gate.
- Laptop runner pairing (`install-runner.sh`); this is the EC2 path only.
- Creating or tearing down AWS stacks as part of shipping this.

## Anti-goals

- A probe that spends real usage on every heartbeat. Probes run only when a
  credential is staged or changes, and when a turn's own failure is ambiguous.
- A "fix" that makes a dead credential read as capped or the other way round. The
  two need opposite responses: wait for the reset, or fetch a person.
- Any path where a box, or a token a box holds, can approve its own pairing.

## The ten problems, and what changes

### 1. A capped token reported as "Not logged in" (cloud_runner.py)

Cause: over ACP, a capped setup-token and a signed-out one both fail with a bare
`runner error (acp): authentication required`. `execute_prompt` counts it as
capped only if an earlier credential in the same turn already capped
(`_is_bare_acp_refusal` branch, investigation 9). On a box with ONE credential,
nothing capped first, so it took the dead branch, notified auth-required, and the
turn said "not signed in".

Change: on a bare ACP refusal that nothing else in the turn explains, **probe that
credential** (below) before classifying it. Probe says capped → count it as capped:
its reset is read from the probe text (`_cap_reset_epoch`), `cap_line` is the
probe's line ("You've hit your weekly limit · resets 11pm (UTC)"), and the existing
out-of-usage path runs (park until reset, `claude_usage_exhausted` alert, turn note
naming the reset). Probe says dead → today's dead path. Probe inconclusive (timeout,
no output) → dead path, as today. The CLI's own sentences (`_AUTH_REQUIRED_MARKERS`)
still win without a probe.

### 2. "Ready" does not mean a working login (cloud_runner.py + runner page)

**The credential probe.** One function, `_probe_credential(index) -> {status,
detail, reset}`. It runs `claude -p "Reply with OK" --output-format json
--max-turns 1` in a scratch dir, with an environment built the way
`_apply_claude_credential` builds it but **without mutating `os.environ`** (turns
run concurrently). Timeout 90 s. Classification uses the existing
`_is_usage_cap` / `_is_auth_required`:
`ok` | `capped` (+ reset epoch when readable) | `dead` | `unknown` (timeout,
crash, unreadable). Each result is kept per credential label in memory with the
time it was taken.

When it runs:
- after `fetch_and_stage_credential` stages a bundle, **synchronously, before the
  first ready heartbeat** (the box is not serving yet, so nothing waits on it);
- after a re-read (`_reload_claude_credentials`, the periodic re-read) yields a
  changed bundle: only the new or changed entries, in a background thread;
- for a `capped` entry, once its reset has passed (checked on the main loop tick);
- never on a plain heartbeat.

A turn's own outcome updates the same state for free: a turn that succeeded marks
its credential `ok`; a classified cap or dead marks it so.

**What it feeds.**
- Health checks, one per credential: `claude.probe.<label>` with `ok` / `warn`
  (capped: "at its weekly limit — resets 2026-10-18T23:00Z") / `fail` ("not signed
  in — someone has to sign it in again") / `warn` (unknown). These ride the existing
  `health` report, so no server schema change.
- Readiness: `_heartbeat_body` reports `ready=False` with a note when probes have
  run and **no** credential is `ok` or `unknown`. The note names each credential's
  state and, when all are capped, the earliest reset.
- `ready` alone does not stop claims (it feeds only `Runner.is_available`, the
  cascade's "may a lower rank go first"; the claim and the box's REST loop never
  read it). So a box whose probes find no working credential also **pauses
  itself**, through the same `POST /runners/{id}/pause` the all-capped path already
  uses: with `until` = the earliest reset when every credential is capped, and with
  no `until` when any is dead ("not signed in: <labels>"). A pause is what the
  claim, the chat banner (`parked`) and `turn_reach` already understand, so pinned
  and bound work waits visibly instead of failing turn after turn. The box
  **unpauses itself** when a later probe finds a working credential, but only a
  pause it set itself (its note prefix identifies it); a person's pause is never
  lifted by the box. *(Builder's call, recorded: reuses the existing cap-pause.)*
- The runner page (`frontend/src/components/supervisor/RunnerCredentials.tsx`):
  each token and login dot takes its colour from that credential's
  `claude.probe.<label>` check when present (ok → success, capped → warning, dead
  → destructive), with the detail as its title/inline text. With no check, today's
  "set" dot stays. Semantic tokens only (CLAUDE.md → Frontend).

Mapping a page row to a probe label: tokens are `subscription-1`/`subscription-2`,
the API key `api-key` (whatever `_BUNDLE_SLOTS` names), logins `_login_label(key)`.
The builder should serve that mapping from one place on each side and pin it with a
test.

### 3. `auth-required notify returned 422` (apps/events/api.py)

Cause: the box posts its credential alerts to the flat `POST /api/events/`.
`record_events` picks a workspace from the pinned slug, else
`user_default_workspace(owner)`, which is None for an owner in 2+ workspaces → 422.
It has nothing to do with agents. `claude_auth_required`, `claude_usage_exhausted`
and `claude_api_key_fallback` all take this path, so all three were being dropped
for any multi-workspace owner.

Change, in two parts:
- **Where it is recorded.** When the request is authenticated by a runner token
  (`request.runner_token`, set by `apps/tokens/middleware.py`) and no workspace is
  pinned, record the event in that runner's `Runner.workspace`. The runner's
  owner already must hold `AGENT_WORK` in its tenant to run anything, so the
  `EVENTS_WRITE` check stays. A runner with a NULL workspace keeps today's
  fallback. The box-side log line stays as a backstop. Test: a runner token whose
  owner is in two workspaces records an event (it 422'd before).
- **Who is told.** Nothing reads `runner.credential` events today except the
  admin-only event log, so recording alone tells nobody. The three credential
  alerts (`claude_auth_required`, `claude_usage_exhausted`,
  `claude_api_key_fallback`) posted by a runner token also **mail and Web-Push the
  runner's approvers** (owner + explicit runner admins), once per (runner, kind)
  per coalescing window, deep-linking to that runner's credentials. This follows
  `apps/harness/device_key_notify.py`, which does exactly this for a pending device
  key, and is best-effort in the same way. *(Builder's call, recorded.)*

### 4. Full logins don't travel, and nothing said so (wire.sh + runner page)

- `wire.sh` step 2 prints exactly what it copied (which tokens and API key, with
  their names) and, from the source's credential status `logins`, each full login
  it could NOT copy ("ace@dimagi-ai.com — full logins stay on the box they were made
  on"). It then prints the new box's credentials URL to "+ Add a login".
- After staging, `wire.sh` waits (bounded, default 5 min) for the new box's
  `claude.probe.*` checks and prints them. If none is `ok`, it exits non-zero with
  that message and the "+ Add a login" link, before any routing change.
- Runner page: when a cloud box has no full login, the "Full Claude logins" block
  says "No full login on this runner. Logins don't copy between runners: add one
  here to use claude.ai connectors."

### 5. `wire.sh` copies credentials from `cloud-ec2-1` by default (wire.sh)

New default order for `--credential-from`: (1) the flag; (2) a same-name
predecessor (a rebuild keeps its own bundle); (3) **the live cloud box**: not
retired, `status == online`, `ready`, not paused, most recent heartbeat; (4)
Secrets Manager's build fallback. Step 3 no longer names `DEFAULT_RUNNER_NAME`.
`wire.sh` prints which source it used and why.

### 6. A test box that serves nothing until a chat is pointed at it (**decided**)

Two parts, both decided: tighten project-chat routing, and a directed-only box.

**Project chats follow the rules repo turns follow.** A project chat is a chat
session with no agent, only a repo (`Session.project`). Today
(`claim._refined_allows`: "agentless session: runner_target_q had the last word")
its unbound first turn goes to any session-capable box in the tenant. New rules,
applied to an **unbound** agentless session turn, in the claim and in every reader
that mirrors it (`_refined_allows`, `turn_reach`, `unclaimable_queued_turns`,
topology) so they cannot disagree:

1. The box declares the repo: `Session.project` is in the box's
   `capabilities.projects` (`Runner.project_names()`).
2. If the session's workspace has an enabled runner order (`WorkspaceRunnerOrder`,
   nearest up the tree, as `repo_order_for` resolves it), the turn follows it: the
   same cascade and grace as a repo dispatch, among listed boxes that pass rule 1.
3. With no order, only a box the **chatting person administers**
   (`harness.services.can_administer_runner(person, runner)`). The person is the
   turn's `enqueued_by`; a turn with no member (a contact) gets no box under this
   rule, so it needs an order.
4. Choosing a box for a project chat (the `runner_id` at creation, and an explicit
   placement) needs `can_administer_runner(user, runner)`, as an agent chat
   already requires for a non-admin. Today a project chat's `runner_id` is not
   checked at all (`canopy_sessions/api.py` only checks it when `agent is not
   None`; `_resolve_placement` likewise).

Bound sessions are unchanged: a binding still claims only on its holder. Pins
(directed starts) still bypass 1–3, though not 4, which decides whether the pin may
be set at all.

Known behaviour change, accepted: a teammate's new project chat in a workspace with
no runner order no longer lands on someone else's box. `/supervisor` and the
unclaimable report must say why such a turn waits ("no runner order in <ws>, and you
administer no box with <repo>"), using the same predicates.

**Directed-only box.** A box may declare `capabilities.directed = true`. It is a
self-restriction, so box-declared is safe: it can only make a box take less. A
directed box claims only turns **pinned to it** and turns of sessions **bound to
it**. No routed agent turns (even with an enabled assignment row), no repo turns,
no unbound sessions, no inherited workspace order. It stays session-capable, so the
chat runner picker (`frontend/src/components/chat/runnerEligibility.ts`) still
offers it, and a chat started on it binds to it. The picker and the runner page
label it "directed only". A workspace order or agent list that includes it should
show it as never taking routed work (topology / `InheritedOrder`), rather than
silently skipping it.

**`--test` on the scripts.**
- `up.sh --test` (implies a non-default name): `RunnerAgents=''`,
  `RunnerProjects=<--projects, default canopy-web>`, `RunnerSessions=1`, new
  `RunnerDirected=1` CFN parameter → `RUNNER_DIRECTED=1` → `capabilities.directed`.
  Needs `--workspace` (see #7).
- `wire.sh --test`: discover the box, stage credentials, run the probe wait (#4),
  and stop. It changes no assignment row, rule or workspace order, so it needs no
  `--agents`. It prints how to point a chat at the box.
- `--standby` stays as it is.

### 7. Pairing enrolled the box in `dimagi` (**decided: require `--workspace`**)

`up.sh` refuses to create a **new** stack without `--workspace <slug>` and prints
the workspaces the operator can pick from (`GET /api/workspaces/`). It passes
`RunnerWorkspace=<slug>`, and the pairing invitation (#9) carries the same slug. An
update to an existing stack keeps that stack's current `RunnerWorkspace` (read from
the stack's parameters), so the live box's rebuild needs no new flag. The CFN
default for `RunnerWorkspace` no longer matters for new boxes, and the docs say so.

### 8. A paired-but-unstaged box was never discovered (wire.sh)

For a box paired by invitation (#9), `wire.sh` takes the runner id from the
invitation (`up.sh` prints it and records it; `wire.sh --name` reads the newest
consumed invitation for that name). The fallback discovery (for boxes paired the old
way, or `--runner-id` absent) matches a cloud runner with that name, not retired,
with **no credential staged** (`GET …/credential/status`: no tokens, no API key, no
signed-in login), most recent heartbeat first. It no longer looks for "never
heartbeated", which stopped being true when the credential wait started
heartbeating.

### 9. The shared pairing PAT expired silently (**decided: pairing by invitation**)

The PAT on the box exists only to get through pairing. After that the box signs in
with its instance-role device key (keyless). So a new box holds no PAT at all.

**Flow.**
1. `up.sh` (as the operator, with their workbench token) checks that token up
   front (`GET /api/harness/runners/` → 200). If it is dead, stop with: "your
   workbench token is dead — run /canopy:canopy-web-pat-mint".
2. After the stack's `RunnerRole` exists, `up.sh` creates a **pairing invitation**:
   `POST /api/harness/runner-invitations/` `{name, workspace, role_arn}`. The
   caller must be a member of the workspace with what pairing needs today. The
   response has `id`, `approve_url`, `expires_at` (1 hour).
3. `up.sh` prints the approve link and waits (bounded) for approval. The operator
   opens it and approves with Touch ID. Approving is a browser action behind
   `stepup.require_recent_step_up`; a token cannot approve (403), so neither
   `up.sh` nor the box can approve its own invitation.
4. The box, finding no runner state file and no `CANOPY_TOKEN`, pairs by
   invitation: `POST /api/harness/runners/pair-by-role` with its instance-role
   proof (`aws_iam_proof`, the same proof `api_device_keys` verifies). canopy
   verifies the role ARN with STS, finds the approved, unexpired, unconsumed
   invitation for that ARN and name, and in one transaction: creates the `Runner`
   (owner = invitation creator, `workspace` = invitation workspace, kind cloud,
   the box's declared capabilities minus server-owned keys, as `pair_runner`
   does); enrolls it exactly as pairing does today; creates its `aws-iam`
   `RunnerDeviceKey` already **active** (approved_by = the invitation's approver);
   marks the invitation consumed with the runner id. The box then gets a
   `RunnerAccessToken` via `POST /api/runner-sessions/`, as today.
   A pending invitation answers "waiting for approval" (the box retries on its
   poll interval and heartbeats nothing until paired). No invitation, an expired
   one, or a mismatched name answers one uniform refusal, as
   `api_device_key_requests` does (no existence leak).
5. `GET /api/harness/runner-invitations/{id}` gives `up.sh`/`wire.sh` the state
   and, once consumed, the runner id.

**Who may approve.** Only the invitation's creator, who becomes the runner's
owner. This mirrors a device key, which only the box's owner or its runner admins
approve, and no other runner admin can exist before the runner does. *(Builder's
call, recorded; the conservative choice.)* The approve link is also mailed and
pushed to the creator, as `device_key_notify` does.

**The zero-knowledge ECDH key.** A cloud box's ECDH key lives in KMS in the
`<stack>-ecdh` stack, which `up.sh` deploys. `up.sh` reads its public key (KMS
`GetPublicKey`) and puts it in the invitation, so one Touch ID approves both the
device key and the ECDH key, as one approval does today. `pair-by-role` refuses an
ECDH key that differs from the invitation's. A new box routes no agents, so there
are no vault wraps to make at approval; when agents are routed to it later, the
existing wrap flow applies. *(Builder's call, recorded.)*

**The same role, a new instance.** An `--allow-instance-change` update replaces
the instance but keeps the stack's role, and the role IS the box's identity
(`jkt` = sha256 of the role ARN). So `pair-by-role` with a role that already has
an ACTIVE key on a non-retired runner **re-attaches to that runner**: it returns
that runner's id, and no invitation is needed or consumed. The role was already
approved, and there is no second key to collide with. A role whose key is revoked,
or whose runner is retired, needs a new invitation; the old key row is replaced, so
the unique `jkt` holds. `wire.sh`'s predecessor retire step does nothing on this
path, because the runner is the same one. *(Builder's call, recorded.)*

**No silent old-style pairing in the meantime.** A stack `up.sh` creates from now
on gets **no** canopy PAT. A new CFN parameter (`CanopyPatSecret`, default `''`
for new stacks) leaves `CANOPY_TOKEN` unset, so the box pairs only by invitation,
even while the shared secret still exists. An existing stack keeps its current
value. *(Builder's call, recorded.)*

**Rules.** Single use. One live invitation per (role ARN, name). The approve page
shows the name, workspace, role ARN and who asked, so the approver knows what they
are approving. The routes are declared in `apps/api/route_gates.py`: creating and
approving are not settings changes, but they are audited as events
(`apps.events.activity.emit`). They are exposed over MCP automatically (approve
still needs step-up, which a token cannot do). Frontend: an approve page (new route
→ descriptor in `frontend/src/guide/surfaces.ts`; a tenant route under `/w/:ws/`
unless there is a reason to put it at root, recorded in `routeTenancy.test.ts`).
Regenerate `generated.ts`.

**Box and stack.** `cloud_runner.py` pairs by invitation when it has no state
file and no usable `CANOPY_TOKEN`; an existing box (state file present) is
untouched. `canopy-fetch-env` and cloud-init must tolerate a missing
`canopy/cloud-runner/canopy-pat` secret. `up.sh` stops requiring that secret, and
the README says it can be deleted once no stack reads it. **Retiring the secret
itself is an AWS change for a human to make after the deploy; it is not part of
the PR.**

### 10. Bootstrap tried every agent and logged a 403 for each (bootstrap_agents.sh)

Cause: `resolve_roster` treats an EMPTY answer from `GET /runners/{id}/agents` as
"unknown" and falls back to the whole registry. A new box serves nothing, so it
tried every agent the owner's token can list, and each resolve 403'd ("does not run
echo").

Change: `served_agents` distinguishes **answered** (HTTP 200 with a JSON list,
empty included) from **unknown** (no runner id, error, unparseable). An answered
list is final, even empty: provision nothing, log "this box serves no agents yet".
Only unknown falls back to the registry, then `AGENT_SLUGS`. A box that later gains
an agent is refreshed by `request_setup` / the lazy `_ensure_agent_clone`, which
already exist. Test: an empty 200 provisions nothing and calls no resolve.

## Constraints (from CLAUDE.md, load-bearing here)

- `runner_tenant_slugs` stays the tenant gate; `runner_may_hold_agent` is
  unchanged; claim and every mirroring reader use the same predicates (topology,
  turn status, unclaimable report).
- Nothing outside `apps/workspaces/` names a role; gates are capabilities.
- Every route declares its gate; settings writes are classified.
- `cloud_runner.py` stays stdlib-only and a single file
  (`runner/ec2/tests/test_stdlib_only.py`).
- Changing `apps/**/schemas.py` or `api.py` → regenerate and commit `generated.ts`.
- The runner kind's source paths change, so the code sha moves and boxes
  auto-update after a deploy. Deploying needs Jonathan's approval.

## Delivery

Three PRs, each landing on its own with `gh pr merge <n> --auto`:

- **PR 1, credentials and wiring** (#1, #2, #3, #4, #5, #8 fallback, #10): runner,
  events API, wire.sh, bootstrap, runner page.
- **PR 2, project chats and the test box** (#6): routing, directed-only,
  `--test`, chat picker.
- **PR 3, pairing by invitation** (#9, #7, #8 by invitation): model, routes,
  approve page, box pairing path, up.sh/wire.sh, CFN.

Docs updated in each: `docs/architecture/runners.md`, `runner/ec2/README.md`,
`docs/architecture/api-surface.md` for new routes, `docs/architecture/pages.md` for
the approve page, and `docs/architecture/chat-sessions.md` for project-chat routing.

## Left to the builder

- Exact probe prompt and flags, as long as it is one short turn with no tools.
- Where the label↔row mapping lives, as long as each side has one source and a test
  pins them equal.
- The invitation model's field names, and whether the approve page reuses the
  device-key approve page's layout.
- How `/supervisor` words the new "why it waits" reasons, using semantic copy that
  reports state and does not render verdicts.
