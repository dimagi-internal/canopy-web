# Runners: the credentials a turn gets

What a turn is handed to act with: its GitHub identity (delegation, the per-user GitHub App), the canopy login a turn gets, a cloud runner's Claude credentials and the probe that says "ready", how a runner signs in without a key, the runner software and how the cloud runner drives a CLI.

**See also:** [runners.md](runners.md), [runner-routing.md](runner-routing.md), [runner-flags-and-schedules.md](runner-flags-and-schedules.md), [runner-fleets.md](runner-fleets.md), [vault.md](vault.md), [access.md](access.md) ("Whose authority an agent acts with").

## The GitHub identity a turn gets

**An agent acts on GitHub with a delegated credential — a token its OWNER or one of its
ADMINS gave it from their own account — never a box credential.** Boxes hold no GitHub
token of their own. The token is a vault entry of the agent's (`github-token`,
`account: "delegated"`), sealed only to the machines that run the agent
(`apps/agents/credentials/accounts.py`, `delegations.py`; spec
`2026-10-26-delegated-credentials-in-the-vault-design.md`).

- The owner or an admin pastes a fine-grained PAT on the agent's Settings → External accounts →
  GitHub (canopy pre-fills GitHub's creation form, `delegations.create_url`; GitHub has
  no API to mint one). Its repository selection IS the agent's scope. It is refused
  unless it can push and open a pull request on the agent's repo (`check_repo`: probes
  that GitHub answers 422 for a branch/commit that cannot exist, 403/404 for no
  permission — nothing is created), and (409 `no_machine_can_open`) while no machine
  that runs the agent has an approved device key. Then canopy-web seals it to those
  machines (and to the person's own vault key) and keeps nothing it can read. Giving one
  replaces the one in force.
- In force (`accounts.serves`): while its person is one of the agent's admins (the owner
  counts; `is_admin` fails closed on someone who left the workspace). Never on a date:
  GitHub's own expiry is shown, and GitHub refuses it.
- Per turn, `POST /api/harness/runners/{rid}/turns/{tid}/github-token`: only for a turn
  that runner is executing, re-checking `runner_may_hold_agent`; the principal is
  resolved server-side (`delegations.turn_agent`); **409 with the reason and no shared
  fallback** when absent or no longer in force; always refused for a confined (caller)
  turn. It returns the git identity (noreply email), `requested_by` for a
  `Requested-by:` trailer, and `delegated: "github-token"` with the `agent` whose vault
  holds it — no token. The cloud runner opens it from its OWN resolve of that agent
  (`delegated` field, opened by the canopy CLI with the box's key;
  `cloud_runner._delegated_value`). `token` is always empty now (older runners still read it).
- **Checked by the boxes.** canopy-web cannot open it, so a box that does probes it at
  most every six hours (`cloud_runner.probe_github_token`) and reports
  `POST /api/harness/runners/{rid}/delegated-checks`; a failed probe turns `github.<slug>`
  red with "replace it". `github-readiness` reads what the boxes found.
- **A new machine** gets it from one that holds it: the keeper (`canopy vault zk keep`)
  sees it in `GET …/vault-keeper` (`delegated`), checks the newcomer's endorsement, and
  re-seals it (`PUT …/vault-keeper/{slug}/delegated/{name}`).
- **Cloning a new agent.** The box-wide prepare (`bootstrap_agents.sh`, `canopy agent
  prepare --runner`) therefore has no GitHub token: an agent whose private repo is not
  on the box yet is cloned at its FIRST turn, with that turn's token
  (`cloud_runner._ensure_agent_clone` → `_clone_agent_repo`, using the `repo` the
  github-token answer names), and prepare then runs with that turn's GitHub env (the
  canopy CLI's `git` falls back to `GH_TOKEN`), so its private plugin repos install. The
  first turn of each agent after every box-wide bootstrap prepares it again the same
  way (`_PREPARED_WITH_GITHUB`), so private plugins stay as fresh as the box-wide pass
  keeps public ones. A clone the box-wide pass could not make is not reported
  unprepared — that would stop canopy-web sending the turn that clones it; a failed
  in-turn clone is.
- **Agentless repo turns and project chats** (`Turn.project`, or the chat
  session's `project`, = repo name) run as no one on GitHub and are
  refused the same way, unless the repo has an `agents.RepoIdentity` row
  (`repo_slug` unique → agent; edited in Django admin, listed as
  `identity_for_repos` on `GET /api/agents/{slug}/github`), in which case the turn
  runs as that agent under the same rule.
- **Which agent's token a session uses is shown, not inferred.** Session info → Where it
  came from → *GitHub identity* (`GET /api/canopy-sessions/{id}/info`, `github_identity`;
  `delegations.session_identity`, the one decision `turn_agent` also takes) names the agent
  and the reason: `agent` (an agent chat runs as its own agent), `repo_default` (an agentless
  repo chat, run as the agent its repo's `RepoIdentity` names) or `none` (refused a token).
  A workspace project (`AgentProject`) has a lead agent but no default agent of its own: the
  identity comes from the chat's agent or the repo's `RepoIdentity`, never from the project.
- **canopy-web's MCP sign-in is per turn.** Claude Code remembers a 401 from an
  MCP server in `mcp-needs-auth-cache.json` and skips it for ~15 minutes in every
  session sharing that file — on a box, every login's copy is a symlink to the
  one in `~/.claude`. The cloud runner removes the canopy-web entry before each
  turn (`_clear_canopy_needs_auth`), so one session with no credential cannot
  take canopy's tools away from the turns after it.
- Readiness: a box calls `GET /api/harness/runners/{rid}/github-readiness` at boot
  (checked live per agent it serves), and the readiness drill covers it — a missing
  or expired token is a red health check, not a mid-turn 403.

Spec: `docs/superpowers/specs/2026-09-24-agent-github-delegation-design.md`
(`tests/test_agent_delegations.py`). Salesforce for agents: a workspace vault holding `salesforce` (Eva's chrome-sales credential), attached to each agent that uses it; boxes stage it from the resolved values.

### The canopy login a turn gets (turn-scoped agent token)

A runner turn acts as its agent with a login canopy-web issues for THAT turn, in place
of the agent's stored, never-expiring `canopy-pat` (phase 1: clients prefer it and fall
back to the stored PAT; phase 2 removes the PAT).

- `POST /api/harness/runners/{rid}/turns/{tid}/agent-token`
  (`apps/harness/api_turn_agent_token.py`, `turn_agent_tokens.py`): only with runner
  `rid`'s own runner token (a PAT is 403), for a turn it is executing, re-checking
  `runner_may_hold_agent`; 409 for a direct turn, a confined (caller) turn, or an
  agent with no active `Agent.user`. Mints a `PersonalToken` for `Agent.user` (12 h),
  recorded as a `TurnAgentToken`; logged as an `AgentCredentialRead`
  (`turn-agent-token`, `via=turn`) and an `agent.canopy_token.issued` event.
- Revoked when the turn stops executing on that runner: `finish_turn` (every exit,
  requeues included), every terminal transition (`harness.turn_close.close_turn`: the
  lease sweep, a stop, an admission refusal, a moved turn), and `revoke_stale` on
  every queue-sweep tick.
- **Session-scoped on a laptop.** An emdash turn finishes as soon as its Claude session
  exists, and the person goes on driving the session. So on an `emdash` runner a turn
  whose session is still active and bound to that runner under the turn's session key
  gets a login tied to the SESSION (`TurnAgentToken.session`; `via=session` in the read
  log), held while that stays true and revoked by `revoke_stale` when the session leaves
  the runner or is archived. `finish_turn` leaves it alone. `?scope=session` asks for that
  login only (404 with no live session), for a client that outlives the turn. A turn's
  session is its `chat_session`, or the chat it names in `origin_ref.chat_session_id` (a
  laptop chat turn records no session key), else the binding its session key names; it must
  be the turn's agent's session. Never for a cloud or `computer` runner. Every action it takes is tied to
  `turn`, the turn that opened the session.
- Cloud runner: `CANOPY_WEB_PAT` in the turn env + `~/.canopy/agent-token/turn/<tid>.token`
  for the MCP headers helper. Laptop: the canopy CLI asks on demand with the box's
  runner token and the session's turn id, for session scope; the MCP headers helper asks
  through `canopy agent session-token`.

Spec: `docs/superpowers/specs/2026-10-21-turn-scoped-agent-token-design.md`
(`tests/test_turn_agent_token.py`).

### The per-user GitHub App (`apps/github_connect/github_app.py`)

A public GitHub App (`canopy-agents`) authorized per user (user-to-server flow).
It is canopy-web's OWN server-side GitHub access as a person — skill history
(`apps/agents/skill_history.py`, as the agent's owner) and the installation list
call `access_token_for(user)`; no route hands it to a runner.

- `GitHubConnection` stores the refresh token and current 8-hour access token,
  Fernet-encrypted. The access token is reused until `ACCESS_TOKEN_MARGIN` (5 min)
  before expiry. **The refresh token rotates**, so a refresh runs under
  `select_for_update` with a re-read after the lock — two concurrent refreshes
  spending one refresh token kill the grant (`refresh_failed_at` → "reconnect").
- **No `Administration` permission**: repo creation needs it, a user token cannot
  be down-scoped, and it would let agents delete repos. The user creates a blank
  repo; canopy only pushes.
- **Authorizing is not installing:** `/login/oauth/authorize` has no repository
  picker, so Connect points at `/apps/{slug}/installations/new`
  (`install_url`). `connected` alone is not enough state; an empty installation list
  means access to nothing.
- Browser legs: bare views `/auth/github/{start,callback}/` (`config/urls.py`); JSON:
  `/api/tokens/github{,/installations}`.
- Config: `GITHUB_APP_CLIENT_ID` and `GITHUB_APP_SLUG` are public (plain env);
  only `GITHUB_APP_CLIENT_SECRET` is a Secrets Manager entry. Unconfigured is a real
  state — `/settings` says so and everything else works (`is_configured`).

Spec: `docs/superpowers/specs/2026-09-12-github-backed-agent-creation-design.md`
(`tests/test_github_connect.py`).

## A cloud runner's Claude credentials (one ordered list)

A cloud runner holds **one ordered list** of Claude credentials (`RunnerLogin`),
any number of each kind, in any order a person sets on its credentials page. The
box uses the first that works and moves down the list when one hits its usage cap
or stops working. Three kinds:

- **Login** — a full `claude auth login --claudeai`, signed in through the browser
  relay (`RunnerMint` with `slot: box` and the entry's `login_key`; URL out, code
  back). It **stays on the box** — its refresh token rotates, so a copy anywhere
  else would break both. canopy-web holds only the entry and what the box last
  reported about it (`signed_in`, `account`). The only kind that carries claude.ai
  connectors: a setup-token is inference-only (`[claudeai-mcp] Missing
  user:mcp_servers scope`).
- **Token** — a `claude setup-token`, made on the box through the same relay
  (`slot: token`; `login_key` names the token to replace, "" adds one). Good for
  a year with nothing to renew. The box reads the token off a replayed screen
  (`render_screen`: the CLI redraws only changed cells, so stripping its cursor
  moves loses characters), probes it (a token Claude refuses is a failed sign-in,
  with Claude's words), names its entry (`POST …/mint/entry {vault_key}`), seals
  it into the runner's vault (`canopy vault runner-seal`) and only then reports it
  (`…/mint/result {vault_key}`) — done only if the vault holds it; any failure is a
  failed sign-in saying why, and an entry made for it goes again.
- **API key** — sealed into the runner's vault in the browser (`POST …/vault/entries`,
  then `PUT /api/vaults/{id}/zk/values`). Billed per use: stepping onto one raises
  the coalesced `claude_api_key_fallback` alert.

**Values live only in the runner's vault** (`docs/architecture/vault.md` § A
runner's own vault): canopy-web keeps the list — names, kinds, order — and never a
token or key. The bundle names each value's secret (`cascade[].vault_secret`); the
box opens them with its own device key (`canopy vault runner-credentials`), keeps
what it opened until the bundle changes or `VAULT_REREAD_SECONDS` (1 h) passes, and
says how it went in its `claude.vault` check. Nothing takes a plaintext value (no
paste route, no fixed-slot write, no token in a sign-in result), so a runner needs a
vault with a key before it can hold a token or API key; a login needs none.

A token or key cannot say whose it is, so a person names it (`name`). The box
reads the list as `cascade` on `GET …/credential`, with `logins` (the login
entries) and `vault_open`. The status view's `has_claude_token`,
`has_claude_token_secondary`, `has_claude_api_key` read the first two tokens and the
first API key whose value the vault holds.

- **On the box**, each login has its own config directory: `default` is the CLI's
  own (`~/.claude`, state in `~/.claude.json`); every other login is
  `~/.claude-logins/<key>`, holding its own `.credentials.json` and `.claude.json`
  with everything else symlinked to `~/.claude` — so transcripts (`projects/`),
  plugins and settings stay in one place whichever login a turn ran on. Selecting
  a login sets `CLAUDE_CONFIG_DIR` (unset for `default`) and clears both auth env
  vars; concurrent turns each keep the directory they started with. A login is
  in the cascade only when its directory holds a login with the
  `user:mcp_servers` scope.
- **Add** a login (`POST …/logins`) appends an entry and starts its sign-in; it
  reads "not signed in yet" until the box reports otherwise. **Add** a token by
  signing it in, an API key by sealing it (above); **rename** one
  (`PATCH …/credentials/{key}`, `name` only — a `value` is refused).
  **Remove** any entry (`DELETE …/credentials/{key}`); on its next bundle read
  (≤5 min) the box drops it, deleting a login's directory (for `default`, only
  its `.credentials.json`). **Reorder** (`PUT …/credentials/order`) takes every
  key, first to last.
- **The box reports** (`POST …/logins/report`) which listed logins are signed in
  and as which account, whenever that changes. A default login the list has never
  held (signed in before the list existed, or by hand) is reported with
  `adopt_default` and joins the list first; `~/.claude-logins/.default-adopted`
  then records that a missing `default` means removed.

**A token or key keeps the box alive.** A login renews itself, and a renewal can
fail; a box with nothing else then stops. So removing
the **last** token or API key is refused (422; `LAST_FALLBACK_REFUSAL`): add
another first. A box with no usable credential at all waits in
`fetch_and_stage_credential`, heartbeating as online but not ready with a note
that says to sign in, and serves the browser sign-in from there — it does not
read "stale".

The runner reports a `claude.login` health check (ok = signed in on the box as
the first login's account; absent when the list has no login). Any mix of
logins, tokens and API keys, one or several, is a healthy box: `claude.credentials`
fails only when the list is empty, and nothing warns about how many there are. Tests:
`runner/ec2/tests/test_box_login.py`, `tests/test_runner_logins.py`,
`tests/test_runner_mint.py`, `tests/test_runner_credential.py`.

**Nothing copies between boxes.** `wire.sh` gives a new box its own vault (or, with
`--vault`, attaches it to one it shares an isolation boundary with) and prints its
Credentials tab, where its owner makes the key and signs it in; it never holds a
token. A cloud box with no signed-in full login says so on its page.

### The credential probe: "ready" means a credential answered

A stored value is not a working one. So the box asks each credential one short
question with no tools (`cloud_runner._probe_credential`: `claude -p "Reply with
OK" --output-format json --max-turns 1` in a scratch dir, with that credential's
environment built as a copy — never `os.environ`, turns run concurrently; 90 s
timeout) and classifies the answer the way a turn's failure is (below): the
result's `api_error_status` first (401 = `dead`), then the `_is_usage_cap` /
`_is_auth_required` / `_is_out_of_credit` text matchers: `ok`, `capped` (+ the reset
when readable), `dead`, `no_credit` (the account has no credit), or `unknown` (timeout, crash, unreadable). `CLAUDE_PROBE=0` turns probing off
(every probe answers `unknown`).

- **When:** after `fetch_and_stage_credential` stages a bundle, synchronously,
  then reported at once in a heartbeat (the next one is otherwise after the
  bootstrap); after a re-read changes the bundle, only the new or changed entries,
  in the background — and every entry when the bundle was re-written unchanged
  (its `updated_at` moved); after a login is signed in again or the default login
  is adopted (its value, a config dir, does not change). A re-probe keeps the
  entry's old check in the heartbeat until the new answer replaces it — the
  `runner_credential` alert reads a missing check as fixed. For a capped entry once
  its reset has passed and a dead one every `DEAD_REPROBE_SECONDS` (10 min) — the
  main loop's poll tick (`no_credit` like dead, so a topped-up account unpauses the box); and when a turn's own failure is ambiguous (below).
  Background requests queue on one prober thread. Each check's detail ends
  `· probed <ISO>`. Never on a plain heartbeat — probes spend usage. A turn's own outcome
  updates the same state for free (a turn that ran marks its credential `ok`; a
  classified cap or dead marks it so).
- **Health:** one check per credential, `claude.probe.<label>` (`box-login`,
  `box-login:<key>`, `token:<key>`, `api-key:<key>`): ok / warn "at its
  usage cap — resets <ISO>" / fail "not signed in — someone has to sign it in
  again" / fail "out of credit — someone has to add credit to its account" / warn
  "could not check: …". The runner page colours each token and login
  dot by it (`RunnerCredentials.tsx` `probeLabel`, pinned to
  the box's labels by `tests/test_probe_labels_match.py`); with no check the dot
  says only that a value is set.
- **Readiness:** the heartbeat sends `ready=False` with a note naming each
  credential's state (and, when all are capped, the earliest reset) only when every
  staged credential was probed and none is `ok`, `unknown`, or capped with its
  reset passed. A hung probe is `unknown`, so it never keeps a box from serving.
  The WS beat carries `ready`/`ready_note` like the REST beat.
- **Self-pause:** `ready` alone does not stop claims (it feeds only
  `Runner.is_available`), so the box also pauses itself (`POST …/pause`, note
  prefixed `[box] `): until the earliest reset when every credential is capped,
  open-ended when any is dead. The usage-cap park after a turn uses the same
  prefix. It lifts its own pause (`POST …/unpause`) once a probe answers `ok` —
  only a pause whose note carries the prefix, read off the heartbeat reply
  (`paused`/`paused_note`, on both the REST reply and the WS ack). It never
  pauses over, or lifts, a person's pause. A beat with no usable reply leaves
  the pause alone. After anyone changes the pause (a person's unpause, the
  scheduled wake) the box pauses again only on probes taken after that change.
- **A `claude -p` turn's failure is read from the stream, not its text**
  (`cloud_runner._credential_fault`): an assistant message's `error`
  (`authentication_failed`, `oauth_org_not_allowed`, `account_on_hold` → dead;
  `rate_limit` → capped; `billing_error` → no credit), a `system/api_retry` with a dead
  category, a result with `api_error_status: 401`, and a `rate_limit_event` whose
  `status` is `rejected` (capped, its `resetsAt` the exact reset). Dead fails over
  and raises `claude_auth_required`, like a signed-out login. **No credit** (an API
  key whose account is empty, "credit balance is too low") is not a cap — nothing
  resets it: the box fails over like a cap, raises `claude_credit_exhausted` at once,
  and when every credential is spent pauses itself (`[box] out of credit`, retried on
  `CAP_PAUSE_FALLBACK_SECONDS`, lifted by a probe that answers). The text matchers
  decide only when the stream named nothing (ACP, an older CLI): an invalid
  setup-token's "API Error: 401 OAuth access token is invalid" matches none of
  them. An assistant message the stream marked as a credential error is held back
  like a cap sentence. Tests: `runner/ec2/tests/test_structured_credential_errors.py`.
- **An ACP turn is read from its transcript.** The adapter can end a turn the API
  refused with a clean stop and the error as plain text, and refuses a dead token and
  a capped one with the same bare `auth_required`. Claude Code writes the refusal into
  the session's transcript as an `isApiErrorMessage` record with its `error` category,
  so after an ACP turn the runner reads the last record written since the turn began
  (`_transcript_fault`): an API error there is the turn's fault — dead, capped or no
  credit, exactly as under `claude -p` — and fails the turn. Tests:
  `runner/ec2/tests/test_out_of_credit_and_acp_faults.py`.
- **A bare ACP refusal with no transcript is probed.** Over ACP a capped token and a signed-out one
  both fail with `runner error (acp): authentication required`. When nothing
  earlier in the turn capped, the box probes that credential: capped → it is a cap
  (reset from the probe, the probe's line in the turn note, the out-of-usage park
  and alert); dead or inconclusive → not signed in, as before. The CLI's own
  sentences (`_AUTH_REQUIRED_MARKERS`) still win without a probe.

**The alerts reach a person.** The box posts `claude_auth_required`,
`claude_usage_exhausted`, `claude_credit_exhausted` and `claude_api_key_fallback` to
`POST /api/events/` with
its runner token; with no workspace pinned they file in `Runner.workspace`, and
each also mails and Web-Pushes the runner's approvers (owner + runner admins)
once per (runner, alert) per UTC day, linking to the runner on `/supervisor`
(`apps/harness/credential_notify.py`). The exception: a signed-out credential on
a box whose health reports a signed-out `claude.probe.<slot>` check is the
`runner_credential` alert instead (`apps/harness/alerts/runner_credential.py`; so is
an out-of-credit one, "… is out of credit") —
titled with the token's name or the login's account (never the slot), notified
once to the owner and runner admins, resolved when the probe passes — and its
`claude_auth_required` (or `claude_credit_exhausted`) is not also mailed. A laptop (no probe checks) or an
inconclusive probe is still mailed. Tests:
`runner/ec2/tests/test_credential_probe.py`, `runner/ec2/tests/test_auth_required.py`,
`tests/test_credential_notify.py`, `tests/test_alerts_runner_credential.py`,
`tests/test_realtime_runner_consumer.py`.

## How a runner signs in (keyless)

- A paired box signs in **as itself**: it trades a fresh proof of its APPROVED device key (an EC2 box's instance role; a laptop's Secure Enclave/TPM key) for an hour-long `RunnerAccessToken` at `POST /api/runner-sessions/`, renews it before it runs out, and uses it for REST and its `ws/runner/<id>/` socket. Nothing long-lived lives on the box, and nothing in Settings → Tokens can cut a runner off.
- **One protocol, two channels.** The `ws/runner/<id>/` socket's `heartbeat` and `finish` frames are read and applied by the same functions as `POST …/runners/{id}/heartbeat` and `POST …/turns/{id}/finish` (`apps/harness/runner_protocol.py`, over `HeartbeatIn` / `TurnFinishIn`): a WS beat carries `degraded`/`note`, `host`, `projects` and `mailboxes_readable` with the REST rules (absent leaves a list alone; a malformed field costs that field, never the beat), and a WS finish takes only `done`/`failed`/`cancelled` and records `session_key` (a refused frame acks `ok: false` with an `error`). Who may act as a runner is `runner_protocol.acting_runner_q` — its owner; a runner with no owner connects for nobody and is in nobody's `/supervisor` snapshot.
- The token opens the runner protocol only (`apps/harness/runner_auth.py`) and authenticates as `Runner.owner`, so every owner-scoped runner gate is unchanged. Revoking the device key (Settings → Security) stops the box at once — the socket re-checks its credential on every frame (`channels_auth.still_signed_in`).
- **A laptop installed from a download pairs in the browser and holds no PAT** (`apps/harness/api_runner_pairings.py`, `runner/canopy_runner/canopy_runner/browser_pair.py`). The box makes its device key, then `POST /api/harness/runner-pairings/` with the public key and a DPoP proof of it, the loopback port it listens on, its hostname, account and whether emdash is present; it shows the returned code and opens `/pair/<code>`. The person signs in, checks the code, picks a workspace where they hold `agent.work` and a name, and approves behind a passkey step-up (a token is refused). The approval's one-time secret comes back only as a redirect to `http://127.0.0.1:<port>/paired` on the machine whose browser approved, so an approve link sent to someone else gives the sender nothing. The box claims (`…/{id}/claim`, DPoP of the same key plus the secret) and gets a `Runner` (`kind=emdash`, runtime `claude-cli` when there is no emdash — and on Windows unless emdash is already serving its DevTools port, since the "Emdash CDP" launcher that reopens it with one is macOS-only; `Runner.platform` keeps the OS it paired from, so its remedy names the fix that exists there) whose device key is already ACTIVE, ECDH key approved with it; the approver must still hold `agent.work` there. A key canopy-web already has ACTIVE on a live runner re-attaches instead; any other known key, an expired, declined or used pairing, or a wrong secret or proof is one uniform 401. Pairings last 15 minutes, one live per key. `runner.json` then has no token, and `~/.canopy/device-key/status.json` is written as canopy's CLI writes it so `canopy vault` shares the key.
- A developer laptop (`install-runner.sh`) pairs with its person's PAT (`CANOPY_TOKEN`), which also registers its key. A **cloud box pairs by invitation and holds no PAT** (below). A box paired by invitation, and every runner with its own Unix user on a machine, has **no PAT fallback**: without a token it reports itself unauthenticated. Only a cloud box from an older stack that still has `CANOPY_TOKEN` (and never paired by invitation) falls back to it while no approved key can get a token (`runner/ec2/cloud_runner.py::_bearer`). No turn inherits the box's credential: an agent turn acts with its own login, and an agentless repo turn (anyone's, once routed here) reaches canopy only through its asker's read-only caller token (`cloud_runner._agent_env` strips `CANOPY_TOKEN` from both).
- **Pairing a cloud box by invitation** (`apps/harness/api_runner_invitations.py`). `runner/ec2/up.sh`, with the operator's own workbench token, creates a `RunnerInvitation` once the stack's instance role exists: `{name, workspace, role_arn, ecdh_public_jwk}` (the `<stack>-ecdh` KMS public key), in a workspace where the operator has `agent.work`. It lasts an hour, and one live invitation exists per (role, name): your own new one replaces yours, and someone else's live one is never replaced (409). The approve link is printed, mailed and pushed to the creator. **Only the creator approves it**, from a browser behind a passkey step-up (`stepup.require_recent_step_up`); a token gets 403, so neither `up.sh` nor the box can approve its own pairing. The box (no state file, no `CANOPY_TOKEN`) calls `POST /api/harness/runners/pair-by-role` with an instance-role proof bound to that URL (`aws_iam_proof.verify`): waiting → 202; approved → one transaction creates the `Runner` (owner = creator, `workspace` = the invitation's, enrolled there by `Runner.save`), its `aws-iam` `RunnerDeviceKey` already ACTIVE (and the invitation's ECDH key approved with it), and marks the invitation consumed. The creator must still have `agent.work` there at that moment. Every other answer (no invitation, expired, used, another name, another ECDH key, a bad or replayed proof) is one uniform 401 that creates nothing. The box then signs in at `POST /api/runner-sessions/` as any keyless box does.
- **A replaced instance re-attaches.** The role is the box's identity (`jkt` = sha256 of the role ARN), and `up.sh --allow-instance-change` keeps the stack's role. So `pair-by-role` from a role with an ACTIVE key on a non-retired runner of the same name returns 200 with that runner's id; no invitation is needed or used, and `wire.sh` has no predecessor to retire. A role whose runner was retired needs a new invitation from that runner's owner; the old key row is replaced, so `jkt` stays unique. A key for the role on another owner's runner (live, revoked or retired) is never erased, and a pending or revoked key on a live runner is never replaced either: a revocation is undone by its owner, not by re-inviting. Each is the uniform 401.
- A machine running several runners pairs every runner in one call (`runners` on the invitation and on `pair-by-role`; see "Setting up the runners on a machine" in [runners.md](runners.md)).
- **A fleet member joins with no invitation of its own.** When the proven role is a fleet's, `pair-by-role` is `fleets.join`: the fleet's one passkey approval plus AWS's word that the instance is the fleet group's (§ Runner fleets). Its key is role + instance, so sign-in (`aws_iam_proof.verify_caller` → `known_key`) tells members apart.
- Cloud runner: `runner/ec2/cloud_runner.py::_bearer` signs its instance-role proof itself; a runner with its own Unix user runs `canopy vault runner-session` with its own software key. Laptop runner: `runner/canopy_runner/canopy_runner/runner_token.py::RunnerCredential` runs `canopy vault runner-session` when the canopy CLI is installed, else signs the proof itself (`device_proof.py`, the same key files and formats as canopy's `orchestrator/device_key.py`: `se.key` through `canopy-device-key`, the TPM key `canopy-device-key`, or `sw.pem`), shared by its REST client and wake socket. Both renew 10 min before expiry, retry a 401 once with a fresh token, and fall back to the PAT only while no approved key can get one.

## The runner software

A runner is an installed package, never a checkout, and updates itself to what is deployed.

**Laptop install from a download.** `/get-started` and the topology page offer the installers (`GET /api/harness/runner-releases/downloads`, `/download/runner/<platform>`: a signed-in person gets a 302 to a 15-minute presigned S3 URL). macOS (`macos-arm64`, `macos-x86_64`): a DMG holding `Canopy Runner.app`, which copies itself to `~/Applications` and runs `canopy-runner setup` from `Contents/Resources/runtime` — browser pairing when there is no `runner.json`, `~/.canopy/bin/canopy-device-key` linked into the bundle, and the `com.canopy.runner`, `.updater` and `.menubar` LaunchAgents pointing at the app's version-free path. Windows (`windows-x86_64`): an Inno Setup installer that never asks for admin, installs `%LOCALAPPDATA%\Programs\Canopy Runner\versions\<sha12>` behind a `current` junction and runs the same `setup`, which registers `\Canopy\canopy-runner` (at logon, `pythonw.exe … run --log-file`) and `\Canopy\canopy-runner-updater` for the current user. Each bundle carries its own CPython (python-build-standalone) with the runner wheels, Node.js, the CDP sidecar's `node_modules` and the ACP adapter, and on macOS the menu-bar app and `canopy-device-key`; nothing is fetched at install. The runner finds its bundle from `sys.executable` (`bundle.py`) and puts the bundle's node and adapter first. Build: `.github/workflows/runner-release.yml` and `runner/packaging/` (README lists the signing secrets). Every runner-source sha that reaches main is built, and on main uploaded to `runner-releases/<sha>/` in the attachments bucket with `manifest.json` written last; `signed`/`notarized` in the manifest say what actually happened. The upload needs `UploadRunnerReleasesOnly` on the deploy role (`deploy/aws/deploy_roles.py`, applied by hand). Spec: `docs/superpowers/specs/2026-10-19-runner-installers-design.md`.

**The two installs share one runner.** The downloaded app and `install-runner.sh` use the same `runner.json`, device key and job labels; whichever installed last runs it, and switching never re-pairs. `setup` says when it takes over from a developer install; `install-runner.sh` replaces a released app in `~/Applications` and a `canopy-device-key` link into it. `~/.canopy/hold-updates` holds a box on what it runs, for both updaters. Testing paths (second macOS account, unreleased workflow artifacts): `runner/packaging/README.md`.

**Developer install.** `runner/canopy_runner/scripts/install-runner.sh` installs and updates: it `git archive`s a ref, builds and `uv tool install`s three wheels, provisions the CDP sidecar's node deps, and renders + loads a generated launchd plist. The daemon runs from a tool venv no checkout can change. Re-running restarts the daemon (not mid-turn). `--ref <branch>` installs another ref; `--no-launchd` skips the daemon; `--workspace <ws>` pairs into a workspace. Spec: `docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md`.

**Auto-update (both kinds).** A separate job — `com.canopy.runner.updater` (launchd, every 30 min; `--no-auto-update` to skip) on a laptop, `runner/ec2/update_runner.sh` (systemd timer) on a cloud box — installs the **deployed** `expected_code_sha` for that runner kind, never `origin/main`; defers while a turn is in flight (`~/.canopy/in-flight`) — except that a cloud daemon run by its supervisor HANDS OFF: the updater installs at once and SIGHUPs the supervisor, which swaps the daemon while every turn keeps running under its relay, and the new daemon adopts them (only `pinned` turns — confined, or any on a sensitive-data box — still wait; `runner/ec2/README.md` § Turns survive an update); and is read-only against the control plane (`canopy-runner update-check` → `current|stale|busy|unknown`, never a heartbeat). A heartbeat reporting a differing sha gets a `runner.update_available` frame that kicks the updater, so the timer is only the fallback. The canopy plugin + CLI is a second version a box runs, fixed by a re-bootstrap rather than the updater: a box reporting a canopy CLI behind the canopy release gets `refresh_pending` on its heartbeat reply (`apps/harness/canopy_release.py`), and its `canopy.cli` check warns against that release, not only against its own clone. A downloaded install's updater is `canopy-runner self-update --if-stale`: it asks `GET /api/harness/runner-releases/current?platform=` with its own runner token (the route serves the release for the deployed `RUNNER_CODE_SHA`; none built = 404 = `unknown`), downloads the update artifact, checks its sha256 (and on macOS the code signature's team against the running app's), then swaps `~/Applications/Canopy Runner.app` (macOS) or runs the installer with `/UPDATE` and flips `current` (Windows), re-runs `setup --no-pair` and restarts the runner. Laptop log: `~/.canopy/updater.log`; bootout the updater before testing a branch on a box. The cloud box installs via `git show <sha>:runner/ec2/cloud_runner.py` from its on-box clone; Secrets Manager is a first-boot seed only; override with `canopy-runner-update --ref <ref>`. Spec: `docs/superpowers/specs/2026-07-30-cloud-runner-auto-update-design.md`.

**Disk no runner owns.** A runner sweeps only its own scratch, but its `disk` check measures the whole volume. So every tick of the cloud updater (root on a machine running several runners) also handles what no runner owns:
- it ages `/tmp` out after 3 days, whoever owns the files (`/etc/tmpfiles.d/tmp.conf`);
- it deletes the regenerable caches of the user from before several runners (`LEGACY_USER`, when that user is not a runner's and runs nothing);
- at ≥ 80% it records the largest of `/home/*` and `/tmp` in `/opt/canopy-runner/disk-usage.json`, which the `disk` check's detail then names (`runner/ec2/update_runner.sh`, `cloud_runner._disk_largest`).

**Code provenance.** `code_sha` is the last commit touching that runner kind's own source paths, not repo HEAD — laptop: the runner package and its scripts, the menu-bar app, the device key, brand assets and the in-repo `canopy_cron`/`canopy_transcript` packages; cloud: `cloud_runner.py`, `bootstrap_agents.sh`, `update_runner.sh`, `kms_broker.py`, `canopy_transcript` and `canopy_acp` (both lists in `.github/workflows/deploy-labs.yml`). The server holds `RUNNER_CODE_SHA` / `RUNNER_CLOUD_CODE_SHA` (computed by the deploy workflow, `fetch-depth: 0`); `RunnerOut.expected_code_sha` serves the one matching the row's `kind`, and `/supervisor` alerts only when both sides are non-empty and differ (empty = unknown).

**The cloud runner** (`runner/ec2/cloud_runner.py`) polls claim every 15s and drains the queue, executes via `claude -p` (or ACP with `RUNNER_EXECUTOR=acp`, default off; see the executors below), declares repos via `RUNNER_PROJECTS`, and runs the stream/backfill contract on a 3s clock. A cloud box learns of an agent only by bootstrapping, so registering one (or changing its repo) requests a refresh on the cloud boxes that will take its turns (`apps/agents/setup.py::request_setup`); until that box posts the agent's bootstrap report, the agent's setup checklist shows its runner step as waiting. And if a turn for an agent the box has no clone of arrives first, the box bootstraps before running it (`cloud_runner._ensure_agent_clone`, once per agent per `LAZY_BOOTSTRAP_RETRY`) rather than running it in a bare scratch dir. `bootstrap_agents.sh` does the box's half (tooling, gog's file keyring, the canopy plugin + CLI) and then runs `canopy agent prepare --runner` per agent (clones `dimagi-internal/<slug>` into `/opt/agents`, env, plugins, Gmail, a readiness report); it runs from `main()` after the credential is staged and is not `set -e`, so one agent's failure does not stop the rest. Spec: `docs/superpowers/specs/2026-07-25-cloud-agent-bootstrap-design.md`.

### How the cloud runner drives a CLI (turn executors)

The CLI is *which* agent program runs a turn (`claude-code`, `codex`, `apps/harness/clis.py`); the **executor** is *how* the cloud runner drives it. Each executor is a `TurnExecutor` subclass in `cloud_runner.py`, registered in `_EXECUTORS` by its `RUNNER_EXECUTOR` value (runner.cfn.yaml `RunnerExecutor`), and `_executor()` is the only code that turns that setting into behaviour (a branch on the name fails `tests/test_turn_executors.py`):

| Method | Does |
|---|---|
| `run_turn(prompt, turn_id, emit, *, cwd, agent_slug, resume_session_id)` → `(ok, text, cli_session_id)` | starts or resumes the turn and runs it to its end: emits live `assistant` / `tool_start` / `tool_end` events, calls `_announce_session` once it knows the CLI's session id, ends early on `stop_requested`, and leaves a usage cap's wording in `text` (exact reset in `_CAP_RESET_HINT`) so `execute_prompt` fails over down the credential cascade |
| `live(turn_id)` | the handle a stop reaches (a process, a protocol connection), or None |
| `interrupt(turn_id, handle, *, escalate, waited)` | a person's stop: polite first, escalated once ignored for `STOP_ESCALATE_SECONDS`; `request_stop` asks every executor, since one may hand a turn to another |
| `prepare()` | bootstrap installs (the ACP adapter) |
| `check_packages()` | its `packages.*` health check |
| `carries_settings` | whether it can run a locked settings document; a sensitive-data box runs only an executor that can |

Today: `cli` (`ClaudePrintExecutor`: `claude -p` + stream-json, `run_claude`) and `acp` (`AcpExecutor`: canopy_acp over `claude-agent-acp`, `run_acp`, which runs the turn as `claude -p` when canopy_acp did not import); both drive `claude-code`. A new way of driving a CLI — Codex's `codex app-server`, an Agent SDK executor — is a new subclass, its protocol driver a package off the runner's clone imported lazily as `canopy_acp` is. The classes live in `cloud_runner.py` itself because the box runs that file alone (seeded whole, replaced whole by `update_runner.sh`).

What the claim says about the turn's CLI reaches it: a turn claimed for a CLI the box's executor does not drive (`cli`) is finished `failed` with `cli_mismatch` before anything runs (`_cli_refusal`; no `cli` = Claude Code), and the model its agent names (`cli_model`, shaped like a model name or ignored) is `--model` on `claude -p` (`_turn_model`, `_TURN_ENV.model`) and `ANTHROPIC_MODEL` for the ACP adapter's Claude Code (`_model_env`).

Not on the interface, because both executors share them as `claude-code`'s: the session store (`_resume_target_exists`, `_ship_transcript_rows`, `_TurnTail` read `~/.claude/projects`), forks, the credential cascade and its probe, and reading a cap or a sign-out from a turn's text. A second CLI brings its own of each.
