# Multi-CLI agents, Plan 1: the CLI axis in canopy-web — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** canopy-web knows which agent CLI (`claude-code`, `codex`) each turn may run on, which CLIs each runner has signed in and can confine, picks one at the claim with one rule, and records it on the turn and its session.

**Architecture:** A new module `apps/harness/clis.py` owns the vocabulary, the runner's reported CLIs (with a legacy reading for boxes that report none) and `choose(turn, runner)`. The claim, coverage and `turn_reach` all ask `choose`, so routing and "why is this waiting" cannot disagree. Every existing box reads as `claude-code` only, so this ships with no behaviour change until a runner reports Codex (Plan 2).

**Tech Stack:** Django 6, Django Ninja, Pydantic v2, pytest; React 19 + TypeScript (generated API types).

**Spec:** `docs/superpowers/specs/2026-10-26-multi-cli-agents-design.md`

## Global Constraints

- CLI values are exactly `claude-code` and `codex`; nothing else is accepted on any input.
- A runner that reports no `clis` reads as `{"claude-code": {"signed_in": not runner.signed_out, "profiles": capabilities.profiles, "envelope": capabilities.envelope}}`.
- A confined turn (`Turn.capability != ""`) runs only on a CLI reporting `profiles >= PROFILES_VERSION` and `envelope >= ENVELOPE_VERSION`. A pin never bypasses this.
- A turn requiring `sensitive_data` runs only on a CLI in `SENSITIVE_CLIS = frozenset({"claude-code"})`.
- A session never changes CLI once `Session.cli` is set.
- Route docstrings are public API docs; rationale goes in `#` comments. Regenerate and commit `frontend/src/api/generated.ts` after schema changes.
- No raw Tailwind palette literals or `dark:` variants in UI; tables, not cards.
- Every write route keeps its `@gate(...)`; agent definition changes stay admin-gated via `DEFINITION_FIELDS`.

## Review Focus

- **A runner reporting `clis` with an unknown key or junk values** (`{"gpt": true}`, `{"codex": "yes"}`) — must be dropped, never stored or trusted; the known keys still apply. Test in Task 2.
- **A box that reports `clis` but stops sending it** (an older runner reinstalled) — `clis` is a level like `profiles`, so absent falls back to the legacy reading on the next beat; a stale Codex report must never outlive the code that made it. Test in Task 2.
- **A session whose `cli` is `codex` asked on a box where codex signed out** — must wait (not fall back to claude-code), and `turn_reach` must say signed out, not unrouted. Test in Tasks 3 and 5.
- **An agent whose `clis` list is empty or has duplicates on upsert** — refused 422 at the schema. Test in Task 6.
- **A riding follow-up (`rides_turn`) into a running Codex turn** — rides only where the runner chose the same CLI as the session; covered because `choose` returns `session.cli`. Test in Task 4.

---

### Task 1: Fields, constants and migrations

**Files:**
- Create: `apps/harness/clis.py`
- Modify: `apps/agents/models.py` (Agent, beside `runtime_engine`), `apps/harness/models.py` (Turn, RunnerLogin), `apps/canopy_sessions/models.py` (Session, beside `cli_session_id`)
- Create: migrations via `uv run python manage.py makemigrations agents harness canopy_sessions`
- Test: `tests/test_clis.py`

**Interfaces:**
- Produces: `clis.CLAUDE_CODE = "claude-code"`, `clis.CODEX = "codex"`, `clis.CLIS: tuple[str, ...] = (CLAUDE_CODE, CODEX)`, `clis.CHOICES` (Django choices), `clis.DEFAULT: list[str] = [CLAUDE_CODE]`, `clis.SENSITIVE_CLIS = frozenset({CLAUDE_CODE})`.
- Produces fields: `Agent.clis: JSONField(default=clis.default_list)` (a callable returning `[CLAUDE_CODE]`), `Agent.cli_models: JSONField(default=dict, blank=True)`, `Turn.cli: CharField(max_length=32, blank=True, default="", db_default="")`, `Session.cli: CharField(max_length=32, blank=True, default="", db_default="")`, `RunnerLogin.cli: CharField(max_length=32, choices=clis.CHOICES, default=CLAUDE_CODE, db_default=CLAUDE_CODE)`.

- [ ] **Step 1: Write the failing test** — `test_new_agent_defaults_to_claude_code` (a fresh `Agent` has `clis == ["claude-code"]`, `cli_models == {}`); `test_turn_and_session_cli_start_blank`; `test_runner_login_cli_defaults_to_claude_code`.
- [ ] **Step 2: Run** `uv run pytest tests/test_clis.py -v` — expect FAIL (module/fields missing).
- [ ] **Step 3: Implement** the module constants and fields above; each field gets one `#` comment pointing at the spec. Generate the three migrations (schema only; `db_default` so code from before the deploy can still insert).
- [ ] **Step 4: Run** `uv run pytest tests/test_clis.py tests/test_migrations_apply.py -v` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "CLI axis: fields for which agent CLI a turn may run on"`

### Task 2: What a runner reports — `clis` on the heartbeat

**Files:**
- Modify: `apps/harness/clis.py`, `apps/harness/schemas.py:594` (`HeartbeatIn`), `apps/harness/services.py:443` (`heartbeat`), `apps/harness/runner_protocol.py:97` (pass-through), `apps/harness/models.py` (`Runner.signed_out` docstring only)
- Test: `tests/test_clis.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class CliReport: signed_in: bool; profiles: int = 0; envelope: int = 0; version: str = ""`.
- Produces: `clis.reported(runner) -> dict[str, CliReport]` — from `runner.capabilities["clis"]` when it is a dict, else the legacy reading (Global Constraints).
- Produces: `clis.sanitize(raw) -> dict[str, dict] | None` — `None` for a non-dict; drops keys not in `CLIS`; each value coerced to `{"signed_in": bool, "profiles": int, "envelope": int, "version": str[:32]}`, dropping a value that is not a dict.
- Produces: `HeartbeatIn.clis: dict | None = None`; `services.heartbeat(..., clis: dict | None = None)` writes `capabilities["clis"] = sanitize(clis)` when not None, and REMOVES the key when None (a level: a box that stops reporting falls back to legacy on its next beat).

- [ ] **Step 1: Write failing tests** — `test_reported_legacy_box_is_claude_code_only` (no `clis`, `capabilities={"profiles": 3, "envelope": 2}`, `ready=True` → `{"claude-code": CliReport(True, 3, 2)}`); `test_reported_legacy_signed_out_box` (`ready=False`, `ready_note` starting with `SIGNED_OUT_NOTE` → `signed_in False`); `test_heartbeat_stores_sanitized_clis` (beat with `{"codex": {"signed_in": True, "profiles": 3, "envelope": 2}, "gpt": {"signed_in": True}, "claude-code": "yes"}` stores only `codex`); `test_heartbeat_without_clis_drops_report` (a stored report is removed by a beat with `clis` absent); `test_ws_beat_carries_clis` (the WS heartbeat path through `runner_protocol` stores it too).
- [ ] **Step 2: Run** `uv run pytest tests/test_clis.py -v` — expect FAIL.
- [ ] **Step 3: Implement** `CliReport`, `reported`, `sanitize`, the schema field and the heartbeat write (same `fields.append("capabilities")` pattern as `envelope`).
- [ ] **Step 4: Run** `uv run pytest tests/test_clis.py tests/test_realtime_runner_consumer.py -v` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Runners report which agent CLIs they have signed in and can confine"`

### Task 3: The one rule — `clis.choose`

**Files:**
- Modify: `apps/harness/clis.py`
- Test: `tests/test_clis_choose.py`

**Interfaces:**
- Consumes: `reported(runner)` (Task 2); `routing.PROFILES_VERSION`, `routing.ENVELOPE_VERSION`; `runner_requirements.requirements_of(turn)`, `runner_requirements.SENSITIVE_DATA`.
- Produces: `clis.allowed(turn) -> list[str]` — `[turn.chat_session.cli]` if the turn has a session with `cli`; else the acting agent's `clis` (`turn.acting_agent`); else `DEFAULT`. Unknown values in an agent's list are skipped; an empty result is `DEFAULT`.
- Produces: `clis.choose(turn, runner, *, reports: dict[str, CliReport] | None = None, ignore_signed_in: bool = False) -> str | None` — the spec's five steps in order. `ignore_signed_in` is the counterfactual Task 5 asks.
- Produces: `clis.confines(report: CliReport) -> bool`.

- [ ] **Step 1: Write failing tests** — `test_agent_order_wins` (agent `["codex","claude-code"]`, both signed in → `"codex"`); `test_falls_to_next_signed_in` (codex signed out → `"claude-code"`); `test_session_cli_is_sticky` (session `cli="codex"`, codex signed out, claude-code signed in → `None`); `test_confined_turn_needs_confining_cli` (`capability` set, codex `profiles=0` → `"claude-code"`; claude-code also unconfining → `None`); `test_sensitive_turn_only_on_claude_code` (requirements `{"sensitive_data"}`, agent `["codex"]` → `None`); `test_repo_turn_defaults_to_claude_code`; `test_ignore_signed_in_counterfactual` (all signed out, `ignore_signed_in=True` → first allowed).
- [ ] **Step 2: Run** `uv run pytest tests/test_clis_choose.py -v` — expect FAIL.
- [ ] **Step 3: Implement** `allowed`, `confines`, `choose`.
- [ ] **Step 4: Run** `uv run pytest tests/test_clis_choose.py -v` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "One rule picks the CLI a turn runs on"`

### Task 4: The claim asks the rule, stamps the CLI and sends it

**Files:**
- Modify: `apps/harness/claim.py:653` (signed-out early return), `:790-895` (candidate loop + claim update), `apps/harness/routing.py:1105` (`profile_q`), `apps/harness/schemas.py:911` (`TurnOut.cli`), `:1065` (`ClaimedTurnOut.cli_model`), `apps/harness/claiming.py:19` (`issue_credentials` sets `turn.cli_model`)
- Test: `tests/test_claim_clis.py`

**Interfaces:**
- Consumes: `clis.choose`, `clis.reported`, `clis.confines`.
- Produces: the early return becomes `if not any(r.signed_in for r in clis.reported(runner).values()): return None`.
- Produces: `profile_q(runner)` returns `Q()` when any reported CLI `confines`, else `Q(capability="")`.
- Produces: in the candidate loop, after the unit refusal and before the pin check, `cli = clis.choose(turn, runner, reports=reports)`; `None` → `continue`. The `Turn.objects.filter(...).update(...)` adds `cli=cli`; after it, `Session.objects.filter(pk=turn.chat_session_id, cli="").update(cli=cli)` and the same for `turn.canopy_session_id` when set. The claimed `status` event payload gains `"cli": cli`.
- Produces: `TurnOut.cli: str = ""`; `ClaimedTurnOut.cli_model: str = ""` = `agent.cli_models.get(turn.cli, "")` for an agent turn, else `""`.

- [ ] **Step 1: Write failing tests** — `test_codex_agent_claims_on_codex_box_and_stamps` (turn.cli == "codex", session.cli == "codex", payload `cli`/`cli_model`); `test_codex_agent_waits_on_claude_only_box` (claim returns None); `test_legacy_box_claims_as_before` (no `clis` report, claude agent → claimed, `cli == "claude-code"`); `test_confined_turn_skips_codex_without_profiles` (box reports claude-code unconfining and codex confining with an agent allowing only claude-code → not claimed; with codex allowed → claimed on codex); `test_pin_does_not_bypass_cli` (pinned turn, no allowed CLI signed in → not claimed); `test_box_signed_out_everywhere_claims_nothing`; `test_rider_runs_on_session_cli` (a follow-up to a running codex session is claimed as a rider with `cli == "codex"`).
- [ ] **Step 2: Run** `uv run pytest tests/test_claim_clis.py -v` — expect FAIL.
- [ ] **Step 3: Implement** the changes above; compute `reports = clis.reported(runner)` once per claim.
- [ ] **Step 4: Run** `uv run pytest tests/test_claim_clis.py tests/test_claim_schedule_parity.py tests/test_access_matrix.py -v` and then `uv run pytest -x -q` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "The claim picks a turn's CLI with the one rule and records it"`

### Task 5: Coverage and "why is this waiting" agree with the claim

**Files:**
- Modify: `apps/harness/claim.py:139` (`_refined_allows`), `:204` (`_coverage`), `:317` (`turn_reach`)
- Test: `tests/test_claim_clis.py`

**Interfaces:**
- Consumes: `clis.choose(..., ignore_signed_in=...)`.
- Produces: `_refined_allows(..., ignore_cli_sign_in: bool = False)` returns False when `clis.choose(t, r, ignore_signed_in=ignore_cli_sign_in)` is None — placed where the claim places it (after the unit refusal, above the pin).
- Produces: `turn_reach` builds `covering` with `ignore_cli_sign_in=True`, and `live = [r for r in online if clis.choose(turn, r) is not None]`; online-but-none-chosen is `Reach(SIGNED_OUT, online)`, whose message names the CLI: `Reach.reason = "no runner has <cli> signed in"` (the first CLI `allowed(turn)` names).

- [ ] **Step 1: Write failing tests** — `test_reach_signed_out_codex` (codex session, box online with only claude-code signed in → `SIGNED_OUT`, reason `"no runner has codex signed in"`); `test_unclaimable_report_matches_claim_for_cli` (a codex turn on a claude-only box is listed by `unclaimable_queued_turns`; after the box reports codex signed in it is claimed and no longer listed).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_claim_clis.py tests/test_alerts_unclaimable.py -v` and the full suite — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Waiting turns say which CLI they wait for"`

### Task 6: Agents declare their CLIs; credentials carry theirs

**Files:**
- Modify: `apps/agents/schemas.py:49` (`AgentIn`), the agent detail out schema (`AgentDetailOut`), `apps/agents/services.py:58` (`DEFINITION_FIELDS`) and `:87` (upsert field list); `apps/harness/schemas.py` (`RunnerCascadeItem`, `RunnerCredentialEntryOut`, `RunnerLoginOut`, the add-login input), `apps/harness/runners.py` (`_would_leave_no_fallback` per CLI; add-login takes `cli`)
- Test: `tests/test_agent_clis.py`, `tests/test_runner_logins.py`

**Interfaces:**
- Produces: `AgentIn.clis: list[Literal["claude-code","codex"]] | None = None` with a validator: non-empty, no duplicates; `AgentIn.cli_models: dict[Literal["claude-code","codex"], str(max 64)] | None = None`. Both in `DEFINITION_FIELDS` and the upsert loop; both on the agent detail response.
- Produces: `cli: str` on `RunnerCascadeItem`, `RunnerCredentialEntryOut`, `RunnerLoginOut`; add-login input `cli: Literal[...] = "claude-code"`. The cascade stays one ordered list; the box filters by `cli`.
- Produces: the last-fallback refusal counts token/API-key entries of the SAME `cli` as the entry being removed.

- [ ] **Step 1: Write failing tests** — `test_upsert_sets_clis_and_models`; `test_upsert_without_clis_keeps_them` (re-upsert omitting `clis` leaves `["codex"]`); `test_clis_empty_or_duplicate_refused` (422); `test_unknown_cli_refused` (422 for `["gpt"]`); `test_non_admin_cannot_change_clis` (definition-field gate); `test_cascade_items_carry_cli`; `test_last_fallback_is_per_cli` (removing the only codex API key is refused even with claude tokens present).
- [ ] **Step 2: Run** — expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest tests/test_agent_clis.py tests/test_runner_logins.py tests/test_runner_credential.py tests/test_every_route_declares_its_gate.py -v` — expect PASS.
- [ ] **Step 5: Regenerate** `cd frontend && npm run gen:api:local` and commit `generated.ts` with the change: `git commit -m "Agents declare which CLIs they run on; runner credentials name theirs"`

### Task 7: Show it — agent, runner, session

**Files:**
- Modify: `frontend/src/components/agents/AgentRouting.tsx` (an "Agent CLIs" row: ordered list, editable by admins with the existing save path), `frontend/src/pages/runner/RunnerOverview.tsx` (a table of reported CLIs: CLI, signed in, can confine, version), `frontend/src/components/chat/SessionInfoPanel.tsx` (the session's CLI), `frontend/src/components/supervisor/RunnerCredentials.tsx` (a CLI column)
- Test: colocated `*.test.tsx` beside each changed component

**Interfaces:**
- Consumes: generated types from Task 6 (`clis`, `cli_models`, `cli`), runner `capabilities.clis` as served today on the runner detail.
- Labels: `claude-code` → "Claude Code", `codex` → "Codex" (one map in `frontend/src/lib/clis.ts`, `cliLabel(cli: string): string`, unknown values shown raw).

- [ ] **Step 1: Write failing tests** — AgentRouting renders "Claude Code" for a default agent and the order for `["codex","claude-code"]`; RunnerOverview lists a reported codex row with "signed in" and "cannot confine yet"; SessionInfoPanel shows "Codex" for a codex session and nothing extra for a blank `cli`.
- [ ] **Step 2: Run** `cd frontend && npx vitest run <files>` — expect FAIL.
- [ ] **Step 3: Implement** with semantic tokens only.
- [ ] **Step 4: Run** the tests and `cd frontend && npm run build` — expect PASS.
- [ ] **Step 5: Commit** `git commit -m "Show which CLI an agent, runner and session use"`

### Task 8: Docs

**Files:**
- Modify: `docs/architecture/runners.md` (new section "Which CLI runs a turn" after "Which agents a box runs — one rule"), `docs/architecture/api-surface.md` (heartbeat `clis`, claim `cli`/`cli_model`, agent `clis`/`cli_models`, credential `cli`), `docs/architecture/chat-sessions.md` (a session's CLI is sticky), `CLAUDE.md` (one Runners invariant line: "A turn's CLI is chosen once, by `apps/harness/clis.py::choose` — claim, coverage and reach all ask it; a confined turn runs only on a CLI that confines; a session never changes CLI. → `docs/architecture/runners.md`")
- Test: `tests/test_claude_md_budget.py`, `tests/test_old_address_in_docs.py`

- [ ] **Step 1: Write the docs** (current state only; no dates or PR numbers in CLAUDE.md).
- [ ] **Step 2: Run** `uv run pytest tests/test_claude_md_budget.py tests/test_old_address_in_docs.py -v` — expect PASS.
- [ ] **Step 3: Commit** `git commit -m "Docs: the CLI axis"`

---

## Later plans (each its own file, written when the previous one lands)

2. **Cloud runner on Codex** (`runner/ec2`, new `runner/canopy_codex`): live probe (a completed turn, a `PreToolUse` deny, `$skill` invocation syntax, `project_doc_fallback_filenames`, canopy MCP with a turn token); app-server driver; prompt translation; per-turn `-c mcp_servers.*`; native permission profile; Codex logins and API keys through the mint relay; `canopy_transcript.codex_rows` + ordinals; usage and rate limits; the `clis` heartbeat report.
3. **canopy plugin on Codex** (canopy repo): guards read Codex payloads; startup self-test that gates `profiles`/`envelope` for `codex`; factory writes `skills/turn` and a persona fallback.
4. **Laptop headless runner on Codex.**
