# PHI agents on ZDR runners — an agent may work over a host's production data only when its author declares it PHI-safe and it runs on a box that really is zero-data-retention

**Status:** design, agreed with Jonathan 2026-10-22. Being built in the order of
[Rollout](#rollout). connect-labs' half of the rule is live (connect-labs
`labs/canopy_panel.py`).
**Layers onto:** runner requirements (ZDR first) `2026-09-30-zdr-runners-design.md`
(built, #1037), host grant contract v1, embedded caller delegation
(`2026-09-26-embedded-caller-delegation-design.md`).

## The problem

connect-labs wants an agent on a workflow's page that a person can ask about the
workflow, its pipelines and the visit data under them, and have it change the workflow
and its pipelines in Labs (`labsdata`, §7). On a
production opportunity that data carries PII and PHI. The person who owns the data (the
host) needs to be able to say **"over real data, only an agent whose author vouches it
handles PHI safely, executing only on boxes whose model calls retain nothing"** — and
have that hold through every path the data can take, not just the first one.

What exists today holds only the first one. What the 2026-10-21 deep dive found:

1. **The `zdr` flag is self-promotable.** A box's own `RunnerAccessToken` may `PUT
   /api/harness/runners/{its id}/flags` (`runner_auth.py` allows every method under the
   runner's own routes; `set_runner_flags` checks only runner-admin), and so may a PAT
   (`tests/test_runner_flags_api.py::test_a_token_can_declare_it`). The 09-30 spec said
   "never the runner itself" and "refused for `is_machine` callers".
2. **No box could honestly carry it.** The cloud runner's credential cascade is box
   login → subscription-1 → subscription-2 → API key (`cloud_runner.py:5326`); a
   subscription is never ZDR, and nothing speaks Vertex or a dedicated key. No runner is
   flagged `zdr` today.
3. **The harness is not locked down.** Every turn runs with
   `--dangerously-skip-permissions` / `bypassPermissions`; no `--strict-mcp-config`;
   Claude Code's telemetry, error reporting and non-essential model calls are on; the
   `acp` executor (the CloudFormation default) passes no native settings at all
   (`cloud_runner.py:1218` vs `run_acp`).
4. **The data leaves the boundary afterwards.** canopy-web stores a ZDR conversation in
   plaintext — `TurnEvent.payload` (full tool results, including `site_call` output),
   `TurnTranscript.raw_jsonl_gz`, `Message` — and then re-processes or re-exposes it with
   no requirement check: share links, Slack share, export, auto-debug (an `ada` turn on
   any FAILED turn), the fleet reviewers (`agent-review`, harvest, analyzer: they fetch
   transcripts and feed them to `claude -p` on ordinary boxes), and the usage scan (a
   session's first prompt, as its title). On the box, `~/.claude/projects/*.jsonl`,
   caller envelopes and profiles are never deleted.
5. **The requirement is easy to drop.** Turns without a chat session, child turns
   (`parent_turn`), forks (`fork_session`) and auto-debug turns carry none
   (`runner_requirements.py:38`).
6. **The host cannot tell who is calling.** The delegated token names only canopy's OAuth
   client; the agent arrives in a `Canopy-Actor` header labs logs and must not trust.
   Nothing says which runner is executing, or what the agent's author declared.
7. **An agent's own host credential is a side door.** Owner and full-profile turns reach
   connect-labs with the agent's own PAT, not through the gateway, so the requirement and
   any host check never see them.

## The rule

A host's real data reaches an agent only through canopy's gateway, and the host
releases it only when canopy has told it, in a statement the host verifies, that **all**
of these hold for the call in front of it:

- the agent's author has declared this capability PHI-safe, at the commit that is
  executing;
- the runner executing the turn carries `zdr`, declared by a person, and is reporting ZDR
  mode on its heartbeat;
- the conversation carries the host's requirement, and has from its first turn.

The host adds its own allowlist of agents it accepts the claim from. Generated synthetic
data is unaffected: any agent, any runner, as today.

**Two attestations stay attestations.** canopy cannot see which key a box's Claude
traffic uses, and cannot read an agent's mind. The design makes each attestation narrow,
made by a person, recorded, refused when it is structurally false, and cross-checked
against what the box reports about itself — and then enforces the conjunction everywhere
the data goes.

## 1. A ZDR runner (the box)

**ZDR mode** — one runner setting, `RUNNER_DATA_MODE=zdr`, that the cloud runner reads
at boot:

- **One credential.** Claude runs only on a dedicated Anthropic API key from an
  organisation with zero data retention enabled (Jonathan's choice, 2026-10-22). The key
  goes in the runner's credential bundle's API-key slot; in ZDR mode the cascade is that
  one slot.
  slot are refused: the box will not start (health `claude.zdr` = fail) while any is
  present, and never falls back. A usage cap waits; it does not cascade.
- **Locked harness on every turn**, on both executors:
  - `--strict-mcp-config` with a rendered MCP config holding only canopy-web (whose
    tools reach the host through the gateway);
  - native settings modelled on connect-labs' `safe-claude/settings.json`: `defaultMode:
    dontAsk`, `disableBypassPermissionsMode` and `disableAutoMode`, allow only `Read`,
    `Grep`, `Glob`, `Skill`, `ToolSearch`, `TodoWrite` and `mcp__canopy*`; deny
    `Bash`, `Write`, `Edit`, `NotebookEdit`, `WebFetch`, `WebSearch`, `Agent`, cron and
    `RemoteTrigger`;
  - no `--dangerously-skip-permissions`, and `bypassPermissions` off on the ACP path
    (it must pass the same settings — fixing the gap in finding 3);
  - `DISABLE_TELEMETRY`, `DISABLE_ERROR_REPORTING`, `DISABLE_NON_ESSENTIAL_MODEL_CALLS`
    and `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` set;
  - no SessionStart auto-update fetch, and no credential probes against the model
    beyond the one key.
- **Nothing kept on the box.** Each turn in its own isolation unit; after it finishes,
  its transcript under `~/.claude/projects`, its caller envelope, its profile and its
  per-turn work directory are deleted. Encrypted EBS.
- **A dedicated box.** A ZDR box — declared `zdr` OR reporting ZDR mode, either alone
  (`runner_requirements.is_zdr_box`) — holds only an agent with a `data: phi` capability, whoever
  owns it (`runner_may_hold_agent` refuses the rest, so claim, pins, credentials and
  routing writes all do).
- **Egress locked.** Agent processes reach only an allowlist of hosts: canopy-web,
  `api.anthropic.com` and what the box itself needs. Egress is its own runner setting,
  set in canopy-web (runner page or MCP, an audited setting) and read by root on the box
  by its instance role, never relayed by a unit. Any box may be locked; ZDR mode locks
  every unit of its box, canopy-web refuses to unlock a ZDR box, and adding a host to
  one is a person behind a passkey.
- **Posture on every heartbeat.** The box reports `data_mode: "zdr"` and a hash of the
  config that produced it (settings, MCP config, environment allowlist, credential slot
  kinds — never the key). That is a self-report and proves nothing; what it buys is
  drift detection: a flagged box whose report is missing or disagrees cannot claim
  (§2).

## 2. Flags a person declares (canopy-web)

- `zdr` keeps its vocabulary entry and gains its teeth: it now means *"this box runs in ZDR
  mode"*. No second flag — a host that serves PHI requires `zdr`.
- `set_runner_flags` is refused for a `RunnerAccessToken` (by path in `runner_auth.py`
  and in the view), for PATs and for every `is_machine` caller, and requires a passkey
  step-up: it is a person vouching for a box. The 09-30 test that lets a token declare
  it is inverted.
- **A declaration counts only while the box agrees** (built: `runner_requirements.in_force`):
  a person's `zdr` routes ZDR work — claim, placement, the gateway — only while the box's
  latest heartbeat reports `data_mode: "zdr"`. A declared box that is not in ZDR mode takes
  no ZDR work, and the runner page says *"Not in force: this box is not reporting ZDR
  mode"*. A box reporting the mode with no declaration is not ZDR either. The report and
  its hash are written every beat and cannot be set by hand.

## 3. The requirement is never dropped

`requirements_of(turn)` reads the union of the turn's session, its ancestors
(`parent_turn`), and — for auto-debug and dispatched turns — the turn they were made
from. `fork_session` copies `runner_requirements`. A turn with requirements and no
session (a schedule a ZDR conversation set up) stores them on the turn itself
(`Turn.runner_requirements`, server-owned).

## 4. PHI stays inside the boundary in canopy-web

One predicate, `apps.harness.sensitivity.is_sensitive(session | turn)` — true when the
requirements include `zdr` — read everywhere below, with one test per surface:

- **Metadata, never content** (Jonathan, 2026-10-22). A stored event, and every
  `Message` of the conversation (the person's, the agent's, a tool call's), keeps what
  troubleshooting needs and none of the words: kind, a tool's name, the NAMES of its
  arguments and the host tool a gateway call reaches, the ids that pair a call to its
  result, error flags, and the payload's size and hash (`sensitivity.redact_payload`, an
  allowlist). The turn's prompt, which the runner reads at claim, is wiped when the turn
  ends (`closing.close_turn`); its result note is withheld (`[zdr] note withheld (N
  bytes)`); no owner-ask line, no live tail, no title (a title is the first sentence
  verbatim, and a person may type a name in it). The live frame still carries the words
  to the person watching, over the socket, and nothing writes them, so **after a reload
  the earlier words are gone**. Follow-up: history from the ZDR box itself while the
  conversation is live. No `TurnTranscript` either (a ZDR box does not even send it).
  What metadata there is stays under a retention rule enforced for sensitive sessions
  whatever `CANOPY_RETENTION_ENFORCE` says (default 30 days).
- **A tool result keeps no rows.** A `TOOL_RESULT` `Message` in a sensitive session is
  stored as `{tool_use_id, is_error}` with empty `plaintext` — the result body never
  reaches canopy-web's database, from the runner's transcript rows or from a live
  `tool_end` event, and the live frame carries the same stripped row, so a reload and a
  live view agree. The `TOOL_USE` row keeps the call's metadata (above), not its
  arguments. An MCP Apps View (`apps/tokens/mcp_apps_views.py`)
  therefore gets `tool_result: null` in a sensitive session and fetches its data **as
  the viewer**, through the host's app-only tool, from `tool_input` — the way connect-labs'
  coaching View already works (a View built on path D already has to, since the runner
  records text only). A host whose View needs its result says so by giving the View an
  app-only read tool; canopy never stores rows to spare it the call. Receipts keep no
  result excerpt (ids, tool, who, when, `is_error`).
- **The agent still has the result.** The model reads a tool's answer inside the
  turn, on the ZDR box, through the ZDR key: `site_call`'s answer goes from the gateway
  (canopy-web, in memory, never written — MCP audit rows record the tool and the
  argument summary, not the answer) straight back to the box's session. A later turn of
  the same conversation resumes that Claude session on the box while the box keeps it;
  once the box has deleted it (§1, "nothing kept"), the conversation starts again from
  what the person asks next: canopy-web's rows say that something was asked, not what.
- **Refuse re-exposure.** Share links, Slack share and mirroring, export, MCP Apps
  receipts beyond ids, and the usage scan's title.
- **Refuse re-processing.** Auto-debug files the failure with its id and error class
  only; the fleet reviewers skip sensitive turns (the transcript endpoints refuse them
  anyway, since they hold no transcript).
- **Memory, people, contributors** — already refused for ZDR turns.

## 5. The author's PHI-safe declaration

On the agent's published interface, per capability:

```yaml
capabilities:
  data:
    sites: [connect-labs]
    data: phi            # new: this capability may serve a host's PHI
```

- `data: phi` is set by the agent's owner or an admin, from the browser or a token, like
  any other interface setting: no passkey. The settings audit records who set it, through
  what, and the before and after.
- Two ways to set it, one stored setting: the YAML, or the per-capability switch on
  Settings → Interface (`PUT …/interface/capabilities/{name}/phi`), which writes or
  removes the `data: phi` line in the saved YAML and saves it down the same path.
- **Refused when it is structurally false:** a `phi` capability's tool set may hold only
  the gateway's site tools, `current_page` and read-only local tools. Drive, email,
  memory, Slack, the web or a shell in the capability → the declaration is a 422 naming
  them.
- **No commit is pinned** (amended): the agent runs its current code. An earlier draft
  had the runner check out a declared SHA; the owner chose a plain, audited setting
  instead, accepting that code merged later inherits the setting.
- The declaration is what an author claims; whether a host accepts it is the host's
  allowlist (§6).

### A `data: phi` capability is confined for everyone

Today an owner's, an admin's and canopy's own turns run the agent's FULL profile
(`access.decide` row 1), and a fleet turn runs with `--dangerously-skip-permissions`.
That cannot stand next to PHI: a declaration is about a capability's tool set, and a
full-profile turn is not in any capability.

- **The rule.** A turn whose capability is `data: phi`, and **any** turn in a session
  whose requirements include `zdr`, runs the declared capability's confined profile
  whatever the caller's relationship — owner, admin and `system` included.
  `access.decide` asks it before row 1: the capability is the one the turn names, else
  the one its site or page selects (`_site_capability`/`_page_capability`); for an
  owner, admin or `system` the caller-class check is skipped (they may use any phi
  capability), for everyone else it holds as today. A `zdr` session in which no `phi`
  capability resolves is refused (`none`, "this conversation needs a capability declared
  PHI-safe"), never run full. `Decision.basis` is `phi:<capability>`; the role, the turn
  mode and `may_request_auto` are unchanged — confinement is about what the turn can
  reach, not who asked.
- **How it is enforced.** It rides the confinement that already exists (access.md "How
  confinement is enforced"): `Turn.capability` is stamped at enqueue, so the envelope says
  `profile: confined`; only a runner reporting `profiles` may claim it; the runner runs it
  in its own `cx-` session with the profile written first, the `profile_guard` hook and
  the native deny rules; it gets a caller token, never the owner's PAT, and no agent
  `.env`. On a ZDR-mode box the runner adds a second check of its own: it refuses (fails
  the turn with `zdr_unconfined`) any turn whose envelope is not `profile: confined` with
  a capability, so a canopy-web bug cannot hand it a full turn; and the locked harness of
  §1 (no skip-permissions, `--strict-mcp-config`, allow-list settings) applies on top.
- **Red team (step 8)**: the agent's owner, from canopy's own UI and from the CLI, opens a
  conversation on a real opportunity and asks for Bash, a file write and the agent's own
  PAT; each is refused and the envelope reads `profile: confined`.

## 6. canopy vouches for the call, the host verifies

The gateway (`host_gateway.site_call`) runs in canopy-web, holds the visitor's DPoP-bound
token, and never hands it to the runner — so it is the one place that can speak for the
call. On every `site_call` to a host it adds `Canopy-Execution: <JWT>`, signed by canopy's
published key (the same JWKS the host already reads for canopy's client metadata), with a
60-second lifetime, bound to the DPoP proof (`jkt`) and single-use (`jti`):

| claim | value |
| --- | --- |
| `agent` | slug |
| `agent_sha` | the commit the turn is executing |
| `declaration` | `{data: "phi", sha, declared_by, declared_at}` or absent |
| `runner` | id, and `flags: [{flag, declared_by, declared_at}]` |
| `runner_posture` | the latest heartbeat's `data_mode` and config hash |
| `requirements` | the session's requirements |
| `turn`, `session` | ids |

The gateway refuses to call a host at all when the session's requirements are not met
(as today), and now also when a `zdr` session's capability has no declaration covering the
executing SHA. The SDK gains the host half: `canopy_sdk.host.verify_execution(request)` →
the claims, or a named refusal.

**The host's side (connect-labs).** Labs' `CANOPY_EXECUTION_STATEMENTS` turns on, and
`real_data_clearance` (today: the agent is in `CANOPY_PHI_AGENTS`, and labs requires
`zdr`) adds, per call: a valid statement; `agent` in labs' allowlist; a declaration whose
SHA equals `agent_sha`; the runner's flags in force ⊇ {`zdr`}; requirements ⊇ {`zdr`}.
Labs records the agent, SHA, runner and declarer on `AuditEvent` and `MCPAuditLog`.

*Amended:* with no commit pinned (§5), the statement carries no `agent_sha` and the
declaration no `sha`; `declaration` is the capability's `data: phi` setting with who last
published the interface and when. The gateway refuses a `zdr` session whose capability
is not `data: phi`; `phi_clearance` drops the SHA check (dimagi-canopy 0.13.0).

**The side door closes.** Every agent's own connect-labs credential is a
no-uservisit-data PAT. Real data is reachable only as a visitor, through the gateway.

## 7. `labsdata` — the data agent

A new, minimal agent, slug **`labsdata`** — a utility, not a persona — scaffolded with
`canopy:create-agent`. It recreates the Scout data-agent experience inside Connect Labs
and goes further: a person looking at (or naming) a workflow asks about it, and the
agent reads the real production data down to row level and raw form JSON, plus the
pipelines, semantic layer and workflow definition behind it, **and changes them** —
fixes a pipeline's SQL, adds an indicator, rewrites the render code — in Labs.
Not ACE: ACE's `connect` capability carries Write, Skill and Drive writes, and its
owner/admin turns ran unconfined — neither can honestly be declared PHI-safe.

**The line is where the data goes, not whether the agent writes** (Jonathan,
2026-10-22): full understanding and editability of the workflow and its pipelines
inside Labs; the data goes nowhere else.

```yaml
capabilities:
  data:
    callers: [contact, member]  # a labs visitor is usually a contact; labs allowlists the agent
    sites: [connect-labs]
    data: phi
    tools: [Read, Grep, Glob, Skill, ToolSearch,
            "mcp__*canopy-web__current_page", "mcp__*canopy-web__who_is_asking"]
```

- **Every write lands in Labs.** The agent's tools are the gateway's site tools (added
  by `sites:`), `current_page`, `who_is_asking` and read-only local tools. No Bash,
  Write, Edit, Drive, email, memory, Slack or the web — the §5 structural check refuses
  a declaration that holds any. Its edits are site calls, so they are Labs writes, made
  as the visitor, recorded by Labs (`MCPAuditLog`, the definition's version history)
  with the agent, SHA and runner of §6. canopy-web keeps an edit's metadata only (§4:
  argument names, never the SQL or code).
- **Scope.** Labs gives it two page scopes, both bound to the opportunities and the
  workflow on screen — carried in the ID-JAG as RFC 9396 `authorization_details`, so a
  token minted on one workflow cannot read or change another:
  - `data:read` (built): SQL over its visits (including `form_json`), and reads of its
    pipelines, semantic layer and workflow definition.
  - `workflow:edit` (new): change that workflow's definition and render code, its
    pipelines' SQL and schema, add or remove its pipeline sources (pipelines on the
    bound opportunities only), and its semantic registry; validate and preview before
    saving; rebuild its history. Each tool refuses a target outside the binding.
- **What `workflow:edit` leaves out — the exits.** Nothing in it moves a row, or a
  definition built from rows, to anywhere other people or other systems read it:
  sharing a pipeline (`pipeline_set_shared`), creating, publishing, exporting or
  sharing a template, cloning or handing a workflow down, changing which opportunities
  a workflow reads (that widens the binding), synthetic-data and clone tools (they copy
  real data's shape out), alert subscriptions, and deletes. Running a workflow's own
  action stays `workflow:act`: previewed, and a coaching send is the person's click.
- **Render code runs sandboxed, or is not edited.** Render code is JavaScript that runs
  in every viewer's browser. Unsandboxed, it runs on Labs' own origin under a
  report-only CSP whose `connect-src` allows any `https:` host — an edit there could
  post the rows it renders anywhere. So a render-code edit is accepted only on a
  workflow that runs in the page sandbox (connect-labs `workflow/sandbox.py`: opaque
  origin, enforced `connect-src` with no Labs origin and no wildcard), and the edit
  passes `sandbox_scan` first. A workflow not yet sandboxed has its render code edited
  by a person, or is moved into the sandbox (`config.sandbox: true`) first.
- **Edits are confirmed.** Labs' rule for a write scope (`PREVIEWED_WRITE_SCOPES`): the
  call that saves carries the single-use token its preview issued, bound to the person
  and exactly what was previewed. For an edit the preview is the diff (definition,
  SQL, render code) and, for a pipeline, its validation over the bound data; the
  person confirms it in the panel. `workflow:edit` joins that list and its test.
- **Results.** Rows reach the person through labs' MCP Apps results View (table, chart,
  CSV), which fetches them as the viewer through an app-only tool (§4: the stored tool
  result keeps no rows).

## Rollout

Each step ships on its own and fails closed until the next lands.

1. **connect-labs: no agent over real data.** Done in connect-labs (`canopy_panel.py`):
   the panel is not drawn over real data, and labs' MCP refuses canopy's calls there.
2. **canopy-web: flags.** Machine callers and the runner's own token cannot declare a
   flag; passkey step-up; requirement inheritance (§3). Done (#1804).
3. **canopy-web runner: ZDR mode** (§1), including the ACP settings fix and the posture
   report; claim requires agreement (§2). Done (#1821).
4. **Stand up the box.** Jonathan obtains the ZDR-org key; a dedicated cloud box in ZDR
   mode; Jonathan declares `zdr`. Done: `cloud-zdr-1`, declared and reporting ZDR mode.
   The box holds only `data: phi` agents (§1, `is_zdr_box`). The egress lock is built
   (a loopback CONNECT proxy per unit plus root's `CANOPY-EGRESS` chain, set in
   canopy-web and applied by root every minute; `runner/ec2/README.md` § Egress lock),
   and ZDR mode is not ready without it.
5. **canopy-web: storage rules** (§4). Done (#1847).
6. **Declarations + the execution statement** (§5, §6), with the SDK's host half and a
   `dimagi-canopy` release; connect-labs verifies it and flips
   `CANOPY_EXECUTION_STATEMENTS`. canopy-web half done: declarations, confinement for
   everyone and the declared commit (#1849); `Canopy-Execution` and
   `canopy_sdk.host.verify_execution` / `Execution.phi_clearance` (dimagi-canopy 0.12.0).
   The statement is also bound to the access token (`ath`). (The commit pin was
   dropped, §5 amended: no `agent_sha`, no `pins`.)
7. **`labsdata`, `data:read` and `workflow:edit`** (§7). `data:read` is built in
   connect-labs; `workflow:edit` and the sandbox rule for render-code edits are next.
   Then connect-labs sets
   `CANOPY_RUNNER_REQUIREMENTS=zdr` and `CANOPY_PHI_AGENTS=labsdata`.
8. **Red team, end to end:** a scripted run on a real opportunity that tries each exit —
   Bash, a file write, WebFetch, a subagent, a non-ZDR runner, a self-declared flag, a
   fork, a share link, Slack, export, auto-debug, an undeclared SHA, the agent's own PAT,
   the OWNER's own turn asking for Bash or a file write (§5: confined for everyone), an
   edit outside the bound workflow, a pipeline share, a template publish, a render-code
   edit on an unsandboxed workflow and one that fetches another host, an edit saved
   without its preview, and a
   stored `TOOL_RESULT` / `tool_end` / receipt holding a row (§4) — and asserts each is
   refused or empty. Script: `scripts/e2e_phi_redteam.py` — a preflight (declaration,
   box, dedication, self-declared flag) and, on a ZDR conversation a person opens from
   Labs, the exits above plus a canary that must appear in nothing canopy stores. The
   undeclared SHA no longer applies (no commit pin, §5); auto-debug is unit-tested only;
   the `workflow:edit` exits join when that scope lands.

## Out of scope

- Verifying ZDR or an agent's behaviour cryptographically. Not observable; stated in the
  UI beside each attestation.
- Interrupting an executing turn when a flag or declaration is withdrawn (claim-time and
  call-time checks only, as in 09-30).
- Labs' own server-side AI (pydantic-ai agents that can select OpenAI models, image
  review on an external gateway). Separate review; noted because "ZDR" in labs' user
  docs is not true of every path.
