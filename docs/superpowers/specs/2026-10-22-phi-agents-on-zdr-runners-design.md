# PHI agents on ZDR runners — an agent may work over a host's production data only when its author declares it PHI-safe and it runs on a box that really is zero-data-retention

**Status:** design, agreed with Jonathan 2026-10-22. Being built in the order of
[Rollout](#rollout). connect-labs' half of the rule is live (connect-labs
`labs/canopy_panel.py`).
**Layers onto:** runner requirements (ZDR first) `2026-09-30-zdr-runners-design.md`
(built, #1037), host grant contract v1, embedded caller delegation
(`2026-09-26-embedded-caller-delegation-design.md`).

## The problem

connect-labs wants an agent on a workflow's page that a person can work with fully:
read and change the workflow, its pipelines and the visit data under them. On a
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
- the runner executing the turn carries `zdr` and `phi`, each declared by a person;
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
  goes in the runner's credential bundle as a new typed slot, `zdr-api-key`; in ZDR mode
  the cascade is that one slot. Box logins, `subscription-*` and the ordinary `api-key`
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
- **A dedicated box.** A ZDR box serves only agents that hold a PHI declaration
  (`runner_may_hold_agent` refuses the rest), and its security group allows egress only
  to `api.anthropic.com` and canopy-web.
- **Posture on every heartbeat.** The box reports `data_mode: "zdr"` and a hash of the
  config that produced it (settings, MCP config, environment allowlist, credential slot
  kinds — never the key). That is a self-report and proves nothing; what it buys is
  drift detection: a flagged box whose report is missing or disagrees cannot claim
  (§3).

## 2. Flags a person declares (canopy-web)

- `RUNNER_FLAGS` gains `phi`: *"This box runs in ZDR mode: one zero-data-retention key, a
  locked harness, and nothing kept after a turn."* `zdr` keeps its 09-30 meaning. A host
  that serves PHI requires both.
- `set_runner_flags` is refused for a `RunnerAccessToken` (by path in `runner_auth.py`
  and in the view), for PATs and for every `is_machine` caller, and requires a passkey
  step-up: it is a person vouching for a box. The 09-30 test that lets a token declare
  it is inverted.
- **Claim requires agreement:** a runner carrying `phi` claims a turn only while its
  latest heartbeat reports `data_mode: "zdr"`. Otherwise turn status says *"…'s ZDR
  runner is not reporting ZDR mode"*.

## 3. The requirement is never dropped

`requirements_of(turn)` reads the union of the turn's session, its ancestors
(`parent_turn`), and — for auto-debug and dispatched turns — the turn they were made
from. `fork_session` copies `runner_requirements`. A turn with requirements and no
session (a schedule a ZDR conversation set up) stores them on the turn itself
(`Turn.runner_requirements`, server-owned).

## 4. PHI stays inside the boundary in canopy-web

One predicate, `apps.harness.sensitivity.is_sensitive(session | turn)` — true when the
requirements include `phi` — read everywhere below, with one test per surface:

- **Store less.** No `TurnTranscript`; `tool_end` events keep the tool name and
  status, never the result; `Message` rows are kept (they are the conversation), under a
  retention rule that is enforced for sensitive sessions whatever
  `CANOPY_RETENTION_ENFORCE` says (default 30 days).
- **Refuse re-exposure.** Share links, Slack share and mirroring, export, MCP Apps
  receipts beyond ids, and the usage scan's title.
- **Refuse re-processing.** Auto-debug files the failure with its id and error class
  only; the fleet reviewers skip sensitive turns (the transcript endpoints refuse them
  anyway, since they hold no transcript).
- **Memory, people, contributors** — already refused for ZDR turns; extended to `phi`.

## 5. The author's PHI-safe declaration

On the agent's published interface, per capability:

```yaml
capabilities:
  workflow:
    sites: [connect-labs]
    data: phi            # new: this capability may serve a host's PHI
```

- `data: phi` is accepted only from a person (passkey step-up), and records
  `declared_by`, `declared_at` and the agent repo **commit SHA** it covers
  (`AgentDeclaration`, one row per declaration, history kept as Events).
- **Refused when it is structurally false:** a `phi` capability's tool set may hold only
  the gateway's site tools, `current_page` and read-only local tools. Drive, email,
  memory, Slack, the web or a shell in the capability → the declaration is a 422 naming
  them.
- **The declared commit is what runs.** For a turn in a `phi` capability the runner
  checks out the declared SHA instead of `git pull main`; a repo that has moved on runs
  the declared SHA until the author re-declares.
- The declaration is what an author claims; whether a host accepts it is the host's
  allowlist (§6).

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
(as today), and now also when a `phi` session's capability has no declaration covering the
executing SHA. The SDK gains the host half: `canopy_sdk.host.verify_execution(request)` →
the claims, or a named refusal.

**The host's side (connect-labs).** Labs' `CANOPY_EXECUTION_STATEMENTS` turns on, and
`real_data_clearance` (today: the agent is in `CANOPY_PHI_AGENTS`, and labs requires
`zdr`) adds, per call: a valid statement; `agent` in labs' allowlist; a declaration whose
SHA equals `agent_sha`; runner flags ⊇ {`zdr`, `phi`}; requirements ⊇ {`zdr`, `phi`}.
Labs records the agent, SHA, runner and declarer on `AuditEvent` and `MCPAuditLog`.

**The side door closes.** Every agent's own connect-labs credential is a
no-uservisit-data PAT. Real data is reachable only as a visitor, through the gateway.

## 7. The workflow agent

A new, minimal agent (not ACE: ACE has Drive, email and memory, which cannot honestly be
declared PHI-safe), scaffolded with `canopy:create-agent`, whose only capability is
`workflow` (`sites: [connect-labs]`, `data: phi`) and whose only tools are labs' site
tools. Labs gives it a new scope, `workflow:full`, bound to the workflow on screen (its
definition and its opportunities, carried in the ID-JAG as RFC 9396
`authorization_details`, so a token minted on one workflow cannot read another): read
and preview its pipelines and their rows, its definition and render code, its runs; and
change them through preview → confirm, as `PREVIEWED_WRITE_SCOPES` already requires of
every write scope.

## Rollout

Each step ships on its own and fails closed until the next lands.

1. **connect-labs: no agent over real data.** Done in connect-labs (`canopy_panel.py`):
   the panel is not drawn over real data, and labs' MCP refuses canopy's calls there.
2. **canopy-web: flags.** Machine callers and the runner's own token cannot declare a
   flag; passkey step-up; `phi` in `RUNNER_FLAGS`; requirement inheritance (§3).
3. **canopy-web runner: ZDR mode** (§1), including the ACP settings fix and the posture
   report; claim requires agreement (§2).
4. **Stand up the box.** Jonathan obtains the ZDR-org key; a dedicated cloud box in ZDR
   mode; Jonathan declares `zdr` + `phi`.
5. **canopy-web: storage rules** (§4).
6. **Declarations + the execution statement** (§5, §6), with the SDK's host half and a
   `dimagi-canopy` release; connect-labs verifies it and flips
   `CANOPY_EXECUTION_STATEMENTS`.
7. **The workflow agent and `workflow:full`** (§7). Then connect-labs sets
   `CANOPY_RUNNER_REQUIREMENTS=zdr,phi` and `CANOPY_PHI_AGENTS=<the agent>`.
8. **Red team, end to end:** a scripted run on a real opportunity that tries each exit —
   Bash, a file write, WebFetch, a subagent, a non-ZDR runner, a self-declared flag, a
   fork, a share link, Slack, export, auto-debug, an undeclared SHA, the agent's own PAT —
   and asserts each is refused.

## Out of scope

- Verifying ZDR or an agent's behaviour cryptographically. Not observable; stated in the
  UI beside each attestation.
- Interrupting an executing turn when a flag or declaration is withdrawn (claim-time and
  call-time checks only, as in 09-30).
- Labs' own server-side AI (pydantic-ai agents that can select OpenAI models, image
  review on an external gateway). Separate review; noted because "ZDR" in labs' user
  docs is not true of every path.
