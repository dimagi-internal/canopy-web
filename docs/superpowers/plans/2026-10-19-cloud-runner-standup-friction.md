# Cloud runner stand-up friction: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the ten problems from standing up `cloud-ec2-fork`, so that `up.sh && wire.sh` needs at most one Touch ID and never reports a broken box as healthy.

**Architecture:** Three independent PRs. PR 1 is the box-side credential truth: a probe, honest cap/dead classification, readiness and self-pause, alerts that reach a person, and wire.sh/bootstrap fixes. PR 2 makes project chats route like repo turns and adds a directed-only box with `--test`. PR 3 replaces the shared pairing PAT with a role-bound pairing invitation, approved once with a passkey.

**Tech Stack:** Django 6 + Ninja (backend), stdlib-only Python (`runner/ec2/cloud_runner.py`), bash 3.2-compatible scripts (`runner/ec2/*.sh`), React 19 + Tailwind semantic tokens (frontend), CloudFormation.

**Spec:** `docs/superpowers/specs/2026-10-19-cloud-runner-standup-friction-design.md`. Read it before any task. Each task names the spec section it implements.

## Global Constraints

- `cloud_runner.py` stays stdlib-only and a single file (`runner/ec2/tests/test_stdlib_only.py`).
- Scripts run on the operator's macOS bash 3.2: no `mapfile`, no associative arrays in `wire.sh`/`up.sh`.
- `routing.runner_tenant_slugs` stays the tenant gate. `runner_may_hold_agent` is unchanged. The claim and every mirroring reader (`claim._refined_allows`, `turn_reach`, `unclaimable_queued_turns`, `topology.py`) use the same predicates.
- Nothing outside `apps/workspaces/` names a role. Gate with `perms.can(...)`.
- Every new route is declared in `apps/api/route_gates.py`, and classified as `SETTINGS` or `NOT_SETTINGS`.
- Any change to `apps/**/schemas.py` or `api.py` regenerates and commits `frontend/src/api/generated.ts` (`cd frontend && npm run gen:api:local`).
- Frontend: semantic tokens only (`text-success`, `text-warning`, `text-destructive`, `text-muted-foreground`). No palette literals, no `dark:`.
- A new page needs a `frontend/src/guide/surfaces.ts` descriptor. A route outside `/w/` needs a line in `frontend/src/routeTenancy.test.ts`.
- Docs describe current state only: no dates, no PR references in CLAUDE.md (`tests/test_claude_md_budget.py`).
- Each PR: `gh pr merge <n> --auto` with no strategy flag. Deploying is NOT part of this work.
- Probe labels: `subscription-1`, `subscription-2`, `api-key` (from `_BUNDLE_SLOTS`), and `_login_label(key)` for full logins. Health check name: `claude.probe.<label>`.

## Review Focus

1. **A probe that hangs** (claude never answers): the box must still start serving, and the probe's 90 s timeout gives `unknown`, which counts as possibly-working. Test it in Task 1.2.
2. **A person's pause must never be lifted by the box's own unpause.** Only a pause whose note starts with the box's self-pause prefix is lifted. Test it in Task 1.3.
3. **A project chat by a contact (no `enqueued_by`) in a workspace with no runner order** waits, and says why. It must not land on an arbitrary box. Test it in Task 2.1.
4. **A directed box listed in an agent's runner list or a workspace order** must never claim routed work. The topology must say so rather than show it as serving. Test it in Task 2.2.
5. **A pair-by-role from a role whose invitation belongs to a different name, has expired, or is already consumed** gets one uniform refusal, and no Runner is created. Test it in Task 3.2.

---

# PR 1: credentials and wiring (spec #1, #2, #3, #4, #5, #8 fallback, #10)

Branch: `runner-credential-truth` from `origin/main`.

### Task 1.1: The credential probe (spec #2, "The credential probe")

**Files:**
- Modify: `runner/ec2/cloud_runner.py`, near `_apply_claude_credential`
- Test: `runner/ec2/tests/test_credential_probe.py` (new)

**Interfaces:**
- Produces: `_credential_env(index: int) -> dict[str, str]`, a copy of `_child_safe_env()` with credential `index` applied exactly as `_apply_claude_credential` applies it (clears `_CLAUDE_AUTH_VARS`, sets `CLAUDE_CONFIG_DIR` from `_BASE_CONFIG_DIR` or the login dir, sets the var). It never touches `os.environ`.
- Produces: `_probe_credential(index: int) -> dict`, which returns `{"label", "status": "ok"|"capped"|"dead"|"unknown", "detail": str, "reset": float|None, "at": float}`. It runs `[CLAUDE_BIN, "-p", "Reply with OK", "--output-format", "json", "--max-turns", "1"]` in a temp dir with a 90 s timeout (constant `PROBE_TIMEOUT_SECONDS = 90`). Classification order: `_is_usage_cap(text)` → capped (reset from `_cap_reset_epoch(text)`); `_is_auth_required(text)` → dead; returncode 0 and not `is_error` → ok; else unknown.
- Produces: `_PROBES: dict[str, dict]`, the latest result per label, and `_record_probe(result: dict) -> None`, which stores it and sets the health check `claude.probe.<label>` (ok→`ok`, capped→`warn` "at its usage cap — resets <ISO>" or "(reset unreadable)", dead→`fail` "not signed in — someone has to sign it in again", unknown→`warn` "could not check: <why>").

- [ ] **Step 1: Write failing tests**, with `subprocess.run` faked:
  - `test_env_for_a_token_sets_only_that_var`: creds `[("subscription-1","CLAUDE_CODE_OAUTH_TOKEN","tok-1"),("api-key","ANTHROPIC_API_KEY","sk")]`; `_credential_env(1)["ANTHROPIC_API_KEY"] == "sk"` and `"CLAUDE_CODE_OAUTH_TOKEN" not in env`; `os.environ` unchanged.
  - `test_env_for_a_login_points_config_dir_at_it`.
  - `test_probe_classifies_the_weekly_cap`: stdout `{"is_error": true, "result": "You've hit your weekly limit · resets 11pm (UTC)"}` → `status == "capped"` and `reset` is not None.
  - `test_probe_classifies_not_logged_in`: `"Not logged in · Please run /login"` → `dead`.
  - `test_probe_ok`: `{"is_error": false, "result": "OK"}` → `ok`.
  - `test_probe_timeout_is_unknown`: `subprocess.TimeoutExpired` → `unknown`.
  - `test_record_probe_sets_the_health_check`: after `_record_probe` for a capped result, `_HEALTH["claude.probe.subscription-1"]["status"] == "warn"` and `"resets" in detail`.
- [ ] **Step 2:** `uv run pytest runner/ec2/tests/test_credential_probe.py -v`. Expect FAIL (no such function).
- [ ] **Step 3:** Implement the three functions and `_PROBES`.
- [ ] **Step 4:** Re-run. Expect PASS. Also `uv run pytest runner/ec2/tests/test_stdlib_only.py runner/ec2/tests/test_credential_cascade.py -q`.
- [ ] **Step 5:** Commit `runner: probe each Claude credential with one short turn`.

### Task 1.2: When probes run, readiness, and self-pause (spec #2 "When it runs", "What it feeds")

**Files:**
- Modify: `runner/ec2/cloud_runner.py`: `fetch_and_stage_credential`, `_reload_claude_credentials`, `_heartbeat_body`, the main loop tick, `execute_prompt` (outcome recording)
- Test: `runner/ec2/tests/test_credential_probe.py`

**Interfaces:**
- Consumes: Task 1.1.
- Produces: `_probe_all(labels: list[str] | None = None) -> None`, which probes the named credentials (all when None) sequentially and records each result. `_credential_readiness() -> tuple[bool, str]` returns `(True, "")` unless `_PROBES` covers every staged credential and none is `ok`/`unknown`. In that case it returns `(False, note)`, where the note lists `label: state` and, when all are capped, "earliest reset <ISO>". `SELF_PAUSE_PREFIX = "[box] "`. `_sync_self_pause() -> None`: when readiness is False and the runner is not already self-paused, it POSTs `/runners/{id}/pause` `{note: SELF_PAUSE_PREFIX + note, until: <earliest reset ISO> or omitted}`. When readiness turns True and the last pause this process set is still live (read `paused_note` off the heartbeat reply), it POSTs `/runners/{id}/unpause`. It never unpauses a note without the prefix. The existing `_park_until_cap_resets` note gains the same prefix.

- [ ] **Step 1: Failing tests:**
  - `test_staging_probes_before_the_first_ready_beat`: fake `_api` for credential GET → a token; fake probe → capped; after `fetch_and_stage_credential`, `_PROBES` has `subscription-1`.
  - `test_readiness_false_when_every_probe_fails`: two creds, probes capped and dead → `_credential_readiness()[0] is False`, note names both.
  - `test_readiness_true_with_one_unknown`: probes dead and unknown → True.
  - `test_heartbeat_body_carries_not_ready`: with all dead, `_heartbeat_body([])["ready"] is False` and `ready_note` is set. With no probes yet, no `ready` key is set (the server default stands).
  - `test_self_pause_then_self_unpause`: records POST `/pause` with the prefix, then with probes ok POSTs `/unpause`.
  - `test_never_unpauses_a_persons_pause`: heartbeat reply `paused_note="maintenance"` → no `/unpause`.
  - `test_reread_probes_only_changed_entries`: reload yields a new `subscription-2` value → `_probe_all` called with `["subscription-2"]`, in a thread (assert via a fake `threading.Thread`, or join).
  - `test_a_successful_turn_marks_its_credential_ok`: `execute_prompt` with `_execute_once` faked ok → `_PROBES["subscription-1"]["status"] == "ok"`.
  - `test_probe_hang_does_not_block_serving`: probe returns unknown on timeout → readiness True (Review Focus 1).
- [ ] **Step 2:** Run. Expect FAIL.
- [ ] **Step 3:** Implement. Call `_sync_self_pause` from the main loop tick at most once per `POLL_SECONDS`, and re-probe `capped` entries whose `reset` has passed from the same tick.
- [ ] **Step 4:** Run the file plus `runner/ec2/tests/test_credential_cascade.py runner/ec2/tests/test_auth_required.py`. Expect PASS.
- [ ] **Step 5:** Commit `runner: a box with no working Claude credential says so, and steps aside`.

### Task 1.3: A bare ACP refusal is probed, not assumed dead (spec #1)

**Files:**
- Modify: `runner/ec2/cloud_runner.py::execute_prompt`, the dead branch
- Test: `runner/ec2/tests/test_auth_required.py`

**Interfaces:** Consumes `_probe_credential` and `_record_probe`.

- [ ] **Step 1: Failing tests:**
  - `test_a_lone_capped_token_over_acp_is_a_cap`: one cred; `_execute_once` returns `(False, "runner error (acp): Authentication required", "")`; the probe returns capped with text "You've hit your weekly limit · resets 11pm (UTC)". Expect: `_notify_auth_required` NOT called, `_notify_out_of_usage` called, `_park_until_cap_resets` called with a non-empty `resets`, and the returned text contains "out of Claude usage" and "weekly limit".
  - `test_a_lone_dead_token_over_acp_stays_dead`: probe dead → `_notify_auth_required` called, the text says "not signed in".
  - `test_probe_unknown_keeps_todays_dead_path`.
  - `test_cli_sentence_needs_no_probe`: text "Not logged in" → probe not called.
- [ ] **Step 2:** Run. Expect FAIL.
- [ ] **Step 3:** In the `else` (dead) branch, when `_is_bare_acp_refusal(text)`, call `_probe_credential(_CLAUDE_CRED_I)`, record it, and on `capped` treat the attempt as a cap: append the label to `capped_labels`, set `cap_line` from the probe detail text, and append the reset.
- [ ] **Step 4:** Run `runner/ec2/tests/test_auth_required.py runner/ec2/tests/test_credential_cascade.py runner/ec2/tests/test_acp_turn_no_limit.py`. Expect PASS.
- [ ] **Step 5:** Commit `runner: a capped token over ACP is reported as a cap, with its reset`.

### Task 1.4: Runner-token events land, and reach a person (spec #3)

**Files:**
- Modify: `apps/events/api.py::record_events`
- Create: `apps/harness/credential_notify.py` (modelled on `apps/harness/device_key_notify.py`; reuses its `approvers(runner)`)
- Test: `tests/test_events_api.py`, `tests/test_credential_notify.py` (new)

**Interfaces:**
- Produces: `credential_notify.notify(runner, item: dict) -> None`, best-effort and never raises. It mails and Web-Pushes `device_key_notify.approvers(runner)` for `item["kind"] in {"claude_auth_required", "claude_usage_exhausted", "claude_api_key_fallback"}`, deep-linking to the runner's credentials (`wsvc.scoped_url` of the supervisor runner page; reuse whatever URL `device_key_notify` builds for Settings). It sends once per (runner, kind) per day: send only when `services.record` created the row, or the existing row's `last_seen_at` is a previous UTC day. Read the coalescing in `apps/events/services.py::record` and return enough from it to decide.

- [ ] **Step 1: Failing tests:**
  - `test_runner_token_records_in_the_runners_workspace`: an owner in two workspaces, a `RunnerAccessToken` for a runner with `workspace=ws_b`; POST `/api/events/` with that bearer → 200, `Event.workspace_id == ws_b`. Same request with the owner's PAT → 422 (unchanged).
  - `test_auth_required_event_mails_the_approvers` (`mail.outbox` has one message to the owner, and the body names the runner).
  - `test_repeat_same_day_does_not_mail_again`.
  - `test_other_kinds_do_not_mail` (e.g. an inbox event).
- [ ] **Step 2:** `uv run pytest tests/test_events_api.py tests/test_credential_notify.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement. In `record_events`: `rtok = getattr(request, "runner_token", None)`. When no workspace is pinned and `rtok` is set and `rtok.runner.workspace_id`, `home = rtok.runner.workspace`. After recording, if `rtok`, call `credential_notify.notify(rtok.runner, item)` per item.
- [ ] **Step 4:** Run. Expect PASS. Also `uv run pytest tests/test_every_route_declares_its_gate.py -q`.
- [ ] **Step 5:** Commit `events: a runner's credential alerts land and reach its owner`.

### Task 1.5: The runner page shows each credential's probe (spec #2 page, #4 page)

**Files:**
- Modify: `frontend/src/components/supervisor/RunnerCredentials.tsx`, and whatever renders it with `signedIn` (find with `grep -rn "<RunnerCredentials" frontend/src`)
- Test: `frontend/src/components/supervisor/RunnerCredentials.test.tsx`

**Interfaces:**
- Produces: `probeFor(checks: Record<string, HealthCheck> | null | undefined, label: string): {tone: 'ok'|'warn'|'fail'|null, detail: string}`. Exported prop `healthChecks?: Record<string, HealthCheck> | null` on `RunnerCredentials`. `PROBE_LABEL: Record<SlotKey, string> = {claude_token: 'subscription-1', claude_token_secondary: 'subscription-2', claude_api_key: 'api-key'}`, the one frontend source. Full logins map through `loginProbeLabel(key)`, which must equal the box's `_login_label(key)` (read it in cloud_runner.py and mirror it).

- [ ] **Step 1: Failing tests:**
  - With checks `{"claude.probe.subscription-1": {status: "warn", detail: "at its usage cap — resets 2026-10-18T23:00Z"}}`, Token 1's dot has class `text-warning` and the detail text is rendered.
  - `fail` → `text-destructive`.
  - With no checks → today's `text-success` "set" dot.
  - A cloud runner with zero logins shows "No full login on this runner. Logins don't copy between runners: add one here to use claude.ai connectors."
- [ ] **Step 2:** `cd frontend && npx vitest run src/components/supervisor/RunnerCredentials.test.tsx`. Expect FAIL.
- [ ] **Step 3:** Implement. Pass `runner.health_checks` from the parent.
- [ ] **Step 4:** Run the test, then `cd frontend && npm run build`. Expect PASS.
- [ ] **Step 5:** Add a pytest that pins `PROBE_LABEL`'s values equal to `cloud_runner._BUNDLE_SLOTS` labels (read the .tsx as text, the way other cross-language pin tests in `tests/` do; find one with `grep -rln "generated.ts\|\.tsx" tests | head`). Commit `runner page: each credential's dot is what the box last proved`.

### Task 1.6: wire.sh picks the live box, says what did not copy, waits for a working credential, finds the unstaged box (spec #4, #5, #8 fallback)

**Files:**
- Modify: `runner/ec2/wire.sh`
- Test: `runner/ec2/tests/test_wire_credential_source.py`, `runner/ec2/tests/test_wire_discovery.py` (new), `runner/ec2/tests/test_wire_probe_wait.py` (new). Reuse the fake-curl harness in `test_wire_credential_source.py`.

**Interfaces:** Consumes the box's `claude.probe.*` health checks on `GET /api/harness/runners/` rows (`health_checks`). New flag `--probe-minutes <n>` (default 5). `--probe-minutes 0` skips the wait.

- [ ] **Step 1: Failing tests:**
  - `test_default_source_is_the_live_ready_box`: runners = `cloud-ec2-1` (offline, old heartbeat), `cloud-ec2-2` (online, ready, recent), and the new box → the GET hits `cloud-ec2-2`'s `/credential`, and the output contains `from: runner cloud-ec2-2`.
  - `test_same_name_predecessor_still_wins`.
  - `test_paused_or_not_ready_box_is_skipped`.
  - `test_prints_logins_that_did_not_copy`: the source's status has `logins: [{key:"default", account:"ace@dimagi-ai.com", signed_in:true}]` → output contains `ace@dimagi-ai.com` and `stay on the box` and the new runner's credentials URL.
  - `test_discovers_a_heartbeating_unstaged_box`: new box row has `last_heartbeat_at` set, and `/credential/status` returns all false → discovered. A same-name row with a staged token is not picked.
  - `test_probe_wait_fails_when_nothing_works`: after staging, the runner row's `health_checks` has only `fail`/`warn`-capped probes → exit non-zero with "+ Add a login" in the output, and NO `PUT /api/agents/` calls.
  - `test_probe_wait_passes_on_one_ok`.
- [ ] **Step 2:** `uv run pytest runner/ec2/tests/test_wire_*.py -v`. Expect the new ones FAIL.
- [ ] **Step 3:** Implement in wire.sh. Discovery: per candidate name-match, non-retired, cloud row, GET `/credential/status`, and pick rows where no token, API key or signed-in login is set, ordered by `last_heartbeat_at` desc. The probe wait runs right after "credential staged", before the standby/retire/swap steps. A runner page URL is `${BASE_URL}/supervisor` (check `docs/architecture/pages.md` for the runner detail address and use it).
- [ ] **Step 4:** Run all `runner/ec2/tests/test_wire_*.py`. Expect PASS.
- [ ] **Step 5:** Commit `wire.sh: copy from the live box, say what stays behind, prove a credential works`.

### Task 1.7: Bootstrap treats an empty served list as the answer (spec #10)

**Files:**
- Modify: `runner/ec2/bootstrap_agents.sh` (`served_agents`, `resolve_roster`)
- Test: `runner/ec2/tests/test_agent_registry_roster.py`

**Interfaces:** `served_agents` prints the slugs and returns exit 0 when answered (HTTP 200 + JSON list, empty included), and exit 3 when unknown. `resolve_roster` uses the served list whenever `served_agents` exits 0.

- [ ] **Step 1: Failing tests:**
  - `test_empty_served_answer_provisions_nothing`: the fake endpoint returns `[]` → `resolve_roster` prints empty, and the registry fake is never called.
  - `test_unreachable_served_falls_back_to_registry` (existing behaviour, keep).
- [ ] **Step 2:** Run. Expect FAIL.
- [ ] **Step 3:** Implement. Make the main loop and its final "no agent bootstrapped cleanly" check treat an empty roster from an answered list as success, logging `this box serves no agents yet`.
- [ ] **Step 4:** `uv run pytest runner/ec2/tests/test_agent_registry_roster.py runner/ec2/tests/test_agents_prepared_by_canopy.py -v`. Expect PASS.
- [ ] **Step 5:** Commit `bootstrap: a box that serves no agents prepares none`.

### Task 1.8: Docs and PR

- [ ] Update `docs/architecture/runners.md` "A cloud runner's Claude login": the probe, `claude.probe.<label>`, readiness and self-pause/unpause, and that full logins never copy. In the "Which agents a box runs" table, the box's refresh row: an empty answer is final. Update `runner/ec2/README.md`: wire.sh source order, the probe wait, `--probe-minutes`, and discovery. Update `docs/architecture/api-surface.md` for `POST /api/events/` with a runner token.
- [ ] `uv run pytest -q` and `cd frontend && npm run build`. Expect all green.
- [ ] Push and open the PR, then `gh pr merge <n> --auto`.

---

# PR 2: project chats route like repo turns, plus a directed-only box (spec #6)

Branch: `project-chat-routing` from `origin/main`.

### Task 2.1: Unbound project-chat turns follow rules 1–3 (spec #6, "Project chats follow the rules repo turns follow")

**Files:**
- Modify: `apps/harness/routing.py` (generalize `repo_order_for`), `apps/harness/claim.py` (`claim_next_turn` candidate loop, `_refined_allows`, `turn_reach`/`unclaimable_queued_turns` reasons)
- Test: `tests/test_project_chat_routing.py` (new)

**Interfaces:**
- Produces: `routing.is_project_session_turn(turn) -> bool`, true for `chat_session` set, `chat_session.agent_id` None, and no binding (or the binding's runner is None). `routing.project_session_allows(runner, turn, orders, now) -> bool`:
  1. `turn.chat_session.project in runner.project_names()`; else False.
  2. If `orders` has the session's workspace (nearest up the tree, the same resolution `load_workspace_orders` + `repo_order_for` use; generalize `repo_order_for(workspace_id, project, orders, requires)` and keep a thin wrapper for repo turns): the same cascade and grace as `_repo_order_allows`.
  3. Else `turn.enqueued_by_id` is set and `harness.services.can_administer_runner(turn.enqueued_by, runner)`.
- The claim calls it for `is_project_session_turn` turns when not pinned. `_refined_allows`'s "agentless session" `return True` calls the same function. `turn_reach`/unclaimable report a reason, `no_project_box`, with text "no runner order in <ws>, and <person> administers no box with <repo>" (or "a contact's project chat needs a runner order in <ws>").

- [ ] **Step 1: Failing tests** (build runners and sessions with the factories used by `tests/test_workspace_runner_order.py` and `tests/test_harness_project_session_api.py`):
  - `test_box_without_the_repo_does_not_take_it`.
  - `test_follows_the_workspace_order`: two boxes with the repo, the order lists B then A → A does not claim within the grace, B does.
  - `test_no_order_only_a_box_the_chatter_administers`: alice's chat; bob's box with the repo → None; alice's box → claims.
  - `test_contact_chat_with_no_order_waits_and_says_why` (Review Focus 3): unclaimable lists it with reason `no_project_box`.
  - `test_bound_session_unchanged`: bound to bob's box → claims on bob's box.
  - `test_pin_still_wins`.
  - `test_reach_and_claim_agree`: parametrize over the cases above, asserting `_refined_allows(...) == (claim_next_turn(runner) is not None)`.
- [ ] **Step 2:** `uv run pytest tests/test_project_chat_routing.py -v`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it plus `tests/test_claim_schedule_parity.py tests/test_turn_status.py tests/test_workspace_runner_order.py tests/test_harness_project_session_api.py tests/test_runner_serves_agents.py`. Expect PASS. Fix any existing test that encoded the old "any box" behaviour, and say so in the commit body.
- [ ] **Step 5:** Commit `routing: a project chat runs on a box with its repo, that the workspace shares or you run`.

### Task 2.2: Choosing a box for a project chat needs you to administer it, plus the directed-only box (spec #6 rule 4, "Directed-only box")

**Files:**
- Modify: `apps/canopy_sessions/api.py` (create: the `runner_id` check also when `agent is None`), `apps/canopy_sessions/services.py::_resolve_placement` (the explicit placement check also for agentless), `apps/harness/models.py` (`Runner.directed_only()`), `apps/harness/routing.py::runner_target_q`, `apps/harness/claim.py`, `apps/harness/topology.py`, `runner/ec2/cloud_runner.py` (`RUNNER_DIRECTED` → `RUNNER_CAPS["directed"]`), `runner/ec2/runner.cfn.yaml` (parameter `RunnerDirected` `'0'|'1'` default `'0'`, written to runner.env)
- Test: `tests/test_directed_runner.py` (new), `tests/test_harness_project_session_api.py`, `runner/ec2/tests/test_cloud_runner.py`

**Interfaces:**
- Produces: `Runner.directed_only(self) -> bool`, which is `bool(self.capabilities.get("directed", False))`. A directed runner's `runner_target_q` is only the bound-session arm (`chat_session__runner_binding__runner=runner`). `claim_next_turn` keeps the pin arm. The early-return guard counts a directed box as having work only when it has pins or bindings. `topology`/`InheritedOrder` mark a directed box as `directed` (never takes routed work).

- [ ] **Step 1: Failing tests:**
  - `test_directed_box_takes_only_pins_and_bindings` (Review Focus 4): a directed box with an ENABLED assignment row for an agent, a workspace order naming it, a repo turn for its project and an unbound session → claims none of them. A pinned turn → claims. A session bound to it → claims.
  - `test_topology_marks_a_directed_box`.
  - `test_project_chat_runner_id_needs_runner_admin`: bob POSTs a session with `project`, no agent, and `runner_id` = alice's box → 403. Alice → 201.
  - `test_explicit_placement_of_project_chat_needs_runner_admin`.
  - Runner side: `test_runner_directed_env_declares_capability` (`RUNNER_DIRECTED=1` → `RUNNER_CAPS["directed"] is True`; unset → key absent).
- [ ] **Step 2:** Run. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run it plus the Task 2.1 suite and `runner/ec2/tests/test_cloud_runner.py`. Expect PASS.
- [ ] **Step 5:** Commit `runner: a directed-only box takes only what is put on it`.

### Task 2.3: The chat picker and runner page label a directed box

**Files:**
- Modify: `frontend/src/components/chat/runnerEligibility.ts` (`isDirectedOnly`), the picker component that renders runners (find it with `grep -rln "eligibleRunners\|isSessionCapable" frontend/src/components`), and the supervisor runner row
- Test: the matching `.test.ts(x)` files

- [ ] **Step 1: Failing tests:** a runner with `capabilities.directed: true` is still offered by the picker, labelled "directed only". The supervisor row shows "directed only".
- [ ] **Step 2–4:** Implement, run vitest, then `npm run build`.
- [ ] **Step 5:** Commit `chat: a directed-only box is offered and labelled`.

### Task 2.4: `up.sh --test` / `wire.sh --test` (spec #6 "--test on the scripts")

**Files:**
- Modify: `runner/ec2/up.sh`, `runner/ec2/wire.sh`
- Test: `runner/ec2/tests/test_up_sh.py`, `runner/ec2/tests/test_wire_test_mode.py` (new)

**Interfaces:** `up.sh --test [--projects a,b]` adds params `RunnerAgents= RunnerProjects=<projects, default canopy-web> RunnerSessions=1 RunnerDirected=1`. It refuses the default name `cloud-ec2-1`, and refuses without `--workspace` (until PR 3 lands, `--workspace <slug>` maps to `RunnerWorkspace=<slug>`. If PR 3 is already on main, reuse its flag). `wire.sh --test`: discovery → staging → probe wait (PR 1's, if present) → print "point a chat at it: <chat URL>, choose <name>" → exit 0. It makes no PUT to `/api/agents/*` or `/api/workspaces/*/runner-order`, and needs no `--agents`.

- [ ] **Step 1: Failing tests:**
  - `test_up_test_mode_params`: captured `cloudformation deploy` args contain `RunnerDirected=1 RunnerSessions=1 RunnerAgents=`.
  - `test_up_test_refuses_live_name`.
  - `test_wire_test_touches_no_routing`: the fake-curl call log has no `PUT`.
- [ ] **Step 2–4:** Implement and run `runner/ec2/tests/test_up_sh.py runner/ec2/tests/test_wire_*.py`.
- [ ] **Step 5:** Commit `scripts: --test stands up a box that serves nothing until a chat is put on it`.

### Task 2.5: Docs and PR

- [ ] Update `docs/architecture/runners.md`: a new "Project chats" subsection under "The workspace default order", and directed-only in "The claim" step 3. Update `docs/architecture/chat-sessions.md` (project chat routing, picking a box), `docs/architecture/access.md` (who may place a project chat), and `runner/ec2/README.md` (`--test`).
- [ ] `uv run pytest -q`; `cd frontend && npm run build`.
- [ ] Push, open the PR, `gh pr merge <n> --auto`.

---

# PR 3: pairing by invitation (spec #9, #7, #8 by invitation)

Branch: `runner-pairing-invitations` from `origin/main`.

### Task 3.1: The invitation model and its owner-facing routes

**Files:**
- Modify: `apps/harness/models.py` (`RunnerInvitation`), plus a migration
- Create: `apps/harness/api_runner_invitations.py` (router mounted where `api_device_key_requests` is mounted; find with `grep -rn "api_device_key_requests" apps/api/api.py`)
- Modify: `apps/harness/schemas.py`, `apps/api/route_gates.py`
- Test: `tests/test_runner_invitations.py` (new)

**Interfaces:**
- `RunnerInvitation`: `id` UUID pk; `name` (`runner_name_ok` charset, max 63); `workspace` FK Workspace; `created_by` FK User; `role_arn` (max 300); `role_jkt` (sha256 of the ARN, the same function `api_device_keys._verified_role` uses for `jkt`); `ecdh_public_jwk` JSON; `created_at`; `expires_at` (`created_at + 1h`, constant `INVITATION_TTL = timedelta(hours=1)`); `approved_at`/`approved_by`; `consumed_at`; `runner` FK null. Unique constraint: one row with `consumed_at IS NULL` per (`role_jkt`, `name`).
- `POST /api/harness/runner-invitations/` `{name, workspace, role_arn, ecdh_public_jwk?}` → 201 `{id, name, workspace, role_arn, status: "pending", approve_url, expires_at, runner_id: null}`. Gate: `wsvc.is_member(user, workspace)` (else 404) and `perms.can(user, ws, perms.AGENT_WORK)` (else 403). A live one for the same role+name is replaced (the old row is deleted). Mails and pushes `approve_url` to the creator (reuse `device_key_notify`'s mail/push helpers).
- `GET /api/harness/runner-invitations/{id}` (creator only, else 404) → same shape; `status` is `pending|approved|consumed|expired`.
- `POST /api/harness/runner-invitations/{id}/approve`: creator only, browser only (`stepup.require_recent_step_up(request)`, so a token gets 403 `step_up_required`), not expired. Sets `approved_*`. Emits `apps.events.activity.emit` for create and approve.
- Route gates: all three `NOT_SETTINGS`.

- [ ] **Step 1: Failing tests:**
  - `test_create_needs_agent_work_in_the_workspace`.
  - `test_non_member_gets_404`.
  - `test_approve_needs_step_up_and_a_token_cannot` (a PAT gets 403 `step_up_required`).
  - `test_only_the_creator_may_approve`.
  - `test_expired_cannot_be_approved`.
  - `test_recreate_replaces_the_live_one`.
- [ ] **Step 2:** Run. Expect FAIL.
- [ ] **Step 3:** Implement, with `uv run python manage.py makemigrations harness`.
- [ ] **Step 4:** Run, plus `tests/test_every_route_declares_its_gate.py tests/test_roles_named_only_in_workspaces.py`.
- [ ] **Step 5:** Regenerate `generated.ts`. Commit `harness: pairing invitations for a new cloud box`.

### Task 3.2: The box pairs by its instance role (spec #9 flow step 4, "same role, a new instance")

**Files:**
- Modify: `apps/harness/api_runner_invitations.py` (`POST /api/harness/runners/pair-by-role`, `auth=None`), `apps/harness/runner_auth.py` (no change needed: the route is unauthenticated, like `api_device_key_requests`)
- Test: `tests/test_runner_invitations.py`

**Interfaces:**
- Request: `{name, kind: "cloud", capabilities: dict, host: str, proof: str, ecdh_public_jwk?: dict, ecdh_store?: str}`. The proof is verified exactly as `api_device_keys._verified_role(request, payload)` does for `kind: aws-iam` (reuse it; htu = this route's URL).
- Responses:
  - 201 `{runner_id, status: "paired"}`: the approved, unexpired, unconsumed invitation for `role_jkt` + `name`. One `transaction.atomic()` creates the Runner (owner=`created_by`, `workspace`, capabilities filtered as `pair_runner` filters them), the enrollment as `pair_runner` creates it (find where pairing enrolls: `grep -n "enroll" apps/harness/services.py apps/harness/signals.py`), and the ACTIVE `RunnerDeviceKey` (`kind=aws-iam`, `role_arn`, `jkt`, `ecdh_public_jwk` = the invitation's, `approved_by` = `invitation.approved_by`). It marks the invitation consumed with the runner.
  - 200 `{runner_id, status: "reattached"}`: an ACTIVE aws-iam key with this `jkt` on a non-retired runner whose `name` matches → that runner, nothing created.
  - 202 `{status: "waiting"}`: a pending (unapproved, unexpired) invitation.
  - 401 uniform `Pairing refused` otherwise: no invitation, expired, consumed, name mismatch, ECDH mismatch (when the invitation has one and the request's differs), bad proof.
  - A revoked key / retired runner with that `jkt` and an approved invitation: the old key row is deleted and the new one created (unique `jkt` holds).

- [ ] **Step 1: Failing tests** (fake STS the way `tests/test_runner_device_key_requests.py` / `test_aws_iam_proof*` do):
  - `test_approved_invitation_pairs_once`: 201, Runner owner = creator, workspace = the invitation's, an active key, enrolled in the workspace. A second call → 200 reattached (same runner id).
  - `test_pending_invitation_waits` (202).
  - `test_refusals_are_uniform_and_create_nothing` (Review Focus 5): parametrize no invitation / expired / consumed-by-other-name / name mismatch / ECDH mismatch → 401, the same body, `Runner.objects.count()` unchanged.
  - `test_replaced_instance_reattaches_without_an_invitation`.
  - `test_retired_runner_role_needs_a_new_invitation_and_replaces_the_key_row`.
  - `test_the_new_runner_can_get_a_runner_session`: then `POST /api/runner-sessions/` with the same proof → 200.
- [ ] **Step 2–4:** Implement and run, plus `tests/test_keyless_runner_auth.py tests/test_runner_enrollment.py`.
- [ ] **Step 5:** Regenerate `generated.ts`. Commit `harness: a cloud box pairs with its instance role, no PAT`.

### Task 3.3: The approve page

**Files:**
- Create: `frontend/src/pages/RunnerInvitationApprovePage.tsx` (match how the device-key approve page is built; find it via `grep -rn "approve_url\|device-key" frontend/src/router.tsx`)
- Modify: `frontend/src/router.tsx`, `frontend/src/guide/surfaces.ts`, `frontend/src/routeTenancy.test.ts` (if it is placed at root, e.g. `/settings/runner-invitations/:id`, next to where device-key approval lives, with the reason "your machines are personal, like device keys"), `frontend/src/api/` client, `docs/architecture/pages.md`
- Test: `frontend/src/pages/RunnerInvitationApprovePage.test.tsx`

- [ ] **Step 1: Failing test:** it renders name, workspace, role ARN, who asked and when it expires. Clicking Approve calls the approve endpoint. The API client's step-up handling does the passkey, so no page code is needed for it. An expired invitation shows "expired" and no button.
- [ ] **Step 2–4:** Implement, then vitest and `npm run build`. Make the `approve_url` from Task 3.1 point at this route.
- [ ] **Step 5:** Commit `pages: approve a new cloud box with one passkey touch`.

### Task 3.4: The box pairs by invitation, and the stack holds no PAT

**Files:**
- Modify: `runner/ec2/cloud_runner.py` (the pairing function that today POSTs `/runners/` at ~line 952), `runner/ec2/runner.cfn.yaml` (`CanopyTokenSecret` default `''`; write `CANOPY_TOKEN=` only when the param is non-empty; `secret` must not run on an empty id)
- Test: `runner/ec2/tests/test_pair_by_role.py` (new), `runner/ec2/tests/test_cloud_runner.py`

**Interfaces:** `pair_by_role() -> str | None`. With no state file and no `TOKEN`, it loops: get instance creds via `_aws_instance_credentials()`, build `aws_proof(BASE_URL + "/api/harness/runners/pair-by-role", creds=creds)`, and POST with `ecdh_public_jwk` from `_ecdh_public_key()`. 201/200 → write `STATE_FILE`, return the id. 202 → log "waiting for the pairing invitation to be approved" and sleep `POLL_SECONDS`. 401 → log "no approved pairing invitation for <role> / <name> — run up.sh" and sleep. The existing PAT path stays for a box that has a `TOKEN` and no state (an old stack).

- [ ] **Step 1: Failing tests:** 202 then 201 → returns the id and writes state. With a state file present → not called. With `TOKEN` set → the old path. CFN: `grep` assertion in a test that `CANOPY_TOKEN=$(secret ${CanopyTokenSecret})` is guarded by a non-empty check.
- [ ] **Step 2–4:** Implement and run `runner/ec2/tests/ -q`.
- [ ] **Step 5:** Commit `runner: a new cloud box pairs by invitation`.

### Task 3.5: up.sh creates the invitation and requires --workspace; wire.sh reads it (spec #7, #8, #9)

**Files:**
- Modify: `runner/ec2/up.sh`, `runner/ec2/wire.sh`
- Test: `runner/ec2/tests/test_up_sh.py`, `runner/ec2/tests/test_wire_discovery.py`

**Interfaces:**
- `up.sh`:
  - Check the workbench token first (`GET /api/harness/runners/` → 200, else die "your workbench token at ~/.claude/canopy/workbench-token is dead or missing — run /canopy:canopy-web-pat-mint").
  - Drop the `canopy-pat` existence check and the PAT probe.
  - For a NEW stack, `--workspace <slug>` is required (else die, listing `GET /api/workspaces/` slugs) and is passed as `RunnerWorkspace=<slug>`. An update reads the stack's current `RunnerWorkspace` with `describe-stacks` and passes it back.
  - After the stack (role) and the `-ecdh` stack exist: read the ECDH public key (`aws kms get-public-key` on the `-ecdh` stack's key output, converted to a JWK in python3; if the stack exposes no key, omit it), then `POST /api/harness/runner-invitations/`.
  - Print `approve: <approve_url>`, poll `GET …/{id}` for up to `--approve-minutes` (default 30) until `consumed`, and print the runner id. Write `"${KEY_DIR:-.}/${STACK}-invitation.json"` `{id, runner_id}`.
- `wire.sh --name X` with no `--runner-id`: if `./canopy-cloud-runner-*-invitation.json` for that stack has a `runner_id`, use it (after a GET confirming it is consumed). Else the PR 1 fallback discovery.

- [ ] **Step 1: Failing tests:**
  - `test_new_stack_without_workspace_refuses`.
  - `test_dead_workbench_token_stops_before_aws_writes`.
  - `test_creates_invitation_and_prints_approve_url`.
  - `test_update_keeps_existing_workspace`.
  - `test_wire_uses_invitation_runner_id`.
- [ ] **Step 2–4:** Implement and run `runner/ec2/tests/test_up_sh.py runner/ec2/tests/test_wire_*.py`.
- [ ] **Step 5:** Commit `up.sh: a new box is invited into a named workspace and approved once`.

### Task 3.6: Docs and PR

- [ ] Update `docs/architecture/runners.md` "How a runner signs in (keyless)": pairing by invitation, who approves, the replaced-instance reattach, and that a new box holds no PAT. Update `docs/architecture/api-surface.md` (three routes plus `pair-by-role`), `docs/architecture/pages.md` (the approve page), and `runner/ec2/README.md`: the flow, `--workspace`, `--approve-minutes`, and that `canopy/cloud-runner/canopy-pat` can be deleted once no stack reads it, as a human AWS step and not done here. Update CLAUDE.md's Runners invariant line about the PAT ("a PAT is for pairing a new box") to "a laptop pairs with a PAT; a cloud box pairs by invitation".
- [ ] `uv run pytest -q`; `cd frontend && npm run build`.
- [ ] Push, open the PR, `gh pr merge <n> --auto`.

---

## Not in this plan

The multi-tenant box isolation work from the Ada session (a Unix user per workspace tree, secrets off disk, owner-PAT fallback removal, read rails, session filing, the session-identity doc, and the four setup-checklist fixes) gets its own design, approved by Jonathan before it is planned. PR 3 removes the PAT from new cloud boxes. Removing `_bearer`'s PAT fallback from existing boxes belongs to that design.
