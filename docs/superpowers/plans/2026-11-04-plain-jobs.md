# Plain Scheduled Jobs (Part 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A schedule can run a named command from a repo's `main` on a cloud machine, with no model, using the agent's connections.

**Architecture:** A job is an ordinary `Turn` with a job name and repo, claimed only by a cloud runner that reports it can run jobs. The runner branches at the `execute_prompt` seam: it exports the repo's `main` into a private directory owned by a separate Unix user, reads the command from the repo's own `.canopy/jobs.yaml`, and runs it as that user with a minimal environment and its own copy of the token.

**Tech Stack:** Django + django-ninja, `runner/ec2/cloud_runner.py`, CloudFormation and shell for the box, React.

**Spec:** `docs/superpowers/specs/2026-11-03-connections-server-use-calls-jobs-design.md` (Part 3).

**Worktree:** `~/emdash/worktrees/canopy-web-connections-server`, branch `feat/plain-jobs`, from `origin/main` at 2aded987.

## Global Constraints

- canopy-web can say only the job's name, the agent and a repo name. The command never comes from canopy-web, in any payload.
- Job name: `[a-z0-9][a-z0-9_-]{0,63}`. Repo name: a bare name matching `[A-Za-z0-9._-]{1,100}`, no `/`, not `.` or `..`; the box builds the URL from its own `REPO_URL_TEMPLATE`.
- `.canopy/jobs.yaml`: `version: 1`, `jobs: {name: {run: [argv...], timeout_minutes: int}}`. Unknown keys are an error. `run` is a non-empty list of strings; no shell. Timeout 1 to 120 minutes, default 15.
- A failed fetch fails the job: never run stale code.
- A job runs as Unix user `canopy-job`, with environment exactly `PATH`, `HOME`, `LANG`, `CANOPY_AGENT_SLUG`, `CANOPY_TURN_ID`, `CANOPY_JOB_TOKENS`. No model key, no MCP token, no runner token.
- At most one job at a time per box, since jobs share a Unix user.
- Non-zero exit is `FAILED` and never retried. A timeout kills everything of that user and fails.
- Sensitive-data runners never report `jobs`.
- Jobs start only from a schedule's fire or "Run now". No route takes a job from a caller's turn; a confined turn never has one.
- Plain sentences on every screen and failure. Every route `@gate` directly above def; regenerate `generated.ts` as CI does; `apps/harness`, `apps/agents` never import `apps/connections`.
- No deploy, no AWS, no changes to a running box. Deploying and rolling the fleet need Jonathan's approval.

## Review Focus

1. A job reading what it mustn't: another agent's token files, the runner's login, the key broker's socket, the instance's AWS role through the metadata address. Each is a test or a checked rule. (Task 3)
2. An older box or laptop claiming a job turn and running an empty prompt through a model. (Task 1)
3. A job that leaves a background process, ignores SIGTERM, or fills the disk or output stream. (Task 3)
4. A missing or malformed `.canopy/jobs.yaml`, an unlisted name, `run` naming something absent: a failed turn with a plain sentence, no retry, tokens removed. (Task 2)
5. A schedule set while no box can take it: visible as waiting, not silently gone; a slot that can't start is treated as other schedules' are. (Tasks 1, 4)

---

### Task 1: The schedule, the turn and the claim

**Files:** Modify `apps/harness/models/schedule.py`, `models/turn.py` (+migration), `schedule_turns.py`, `schedule_services.py`, `claim.py`, `routing_orders.py`, `claim_payload.py`, `schemas/schedules.py`, `schemas/turns.py`, `services.py` (`finish_turn`), `apps/mcp` schedule tool if it lists fields. Test: new `tests/test_plain_job_turns.py`.

**Interfaces:**
- `AgentSchedule.job`, `.job_repo`; `Turn.job`, `.job_repo` (blank for ordinary turns); `Turn.CLOUD_ONLY = "cloud_only"`, added to `ROUTING_CHOICES` (check the field's `max_length`).
- `schedule_services.validate_job(job, job_repo)`: both or neither; shapes per constraints. A job schedule needs no prompt; `routing` is forced to `cloud_only`; `grace_minutes` is at least 125 so a 120-minute job isn't released as missed.
- `fire_schedule` and `run_schedule_now` copy both to the turn. The turn's `prompt` stays empty.
- `claim.py`: only `kind == cloud` with `capabilities.jobs is True` gets a job turn, or any `cloud_only` turn; and not while that runner already holds an executing job turn. `kind_allows` refuses non-cloud for `cloud_only`. Other claims never get a job turn.
- `ClaimedTurnOut` gains `job`, `job_repo`.
- A sessionless FAILED job turn is not re-queued (the `MAX_SESSIONLESS_RETRIES` path).

- [ ] Tests: `test_job_schedule_validation` (bad name, bad repo with `/`, `..`, one without the other); `test_fire_copies_job_and_forces_cloud_only`; `test_run_now_copies_job`; `test_laptop_never_claims_a_job`; `test_old_cloud_box_never_claims_a_job`; `test_capable_box_claims_it`; `test_one_job_at_a_time_per_box`; `test_ordinary_turns_unchanged`; `test_failed_job_not_requeued`; `test_claim_payload_has_name_and_repo_only`, asserting no command field anywhere; `test_unclaimable_job_listed` (`unclaimable_queued_turns`); `test_supersede_and_late_slot_apply`; `test_refused_trigger_stops_a_job` (`triggers.refusal`).
- [ ] Run; they fail. Implement. Run; they pass, with `makemigrations --check`, the gate lint, and regenerated types. Commit.

### Task 2: The box reads and prepares a job

**Files:** Modify `runner/ec2/cloud_runner.py`; `runner/ec2/runner.cfn.yaml` and `update_runner.sh` only if PyYAML isn't already on the box. Test: new `runner/ec2/tests/test_plain_jobs.py`.

**Interfaces:**
- `_job_spec(text: str, name: str) -> JobSpec(run: list[str], timeout_minutes: int)`, raising `JobRefused(sentence)`, using `yaml.safe_load`. Check how the box gets packages; if PyYAML isn't there, add it the way others are added.
- `_job_export(repo: str, turn_id: str, env) -> pathlib.Path`: fetch `origin main` in the bare clone from `_repo_clone`, `git archive <sha>` into a new directory `/var/lib/canopy-jobs/<turn_id>/repo`, unpacked by `canopy-job`, with no `.git`. Returns the path; raises `JobRefused` on a failed fetch. A `repo` failing the name rule raises before any git call.
- `_job_can_run() -> bool`: user `canopy-job` exists, `sudo -n -u canopy-job true` works, not a sensitive-data box. `RUNNER_CAPS["jobs"]` reports it at start.

- [ ] Tests, with a local bare git repo and sudo stubbed at one seam: `test_spec_reads_a_job`; `test_spec_unknown_key_refused`; `test_spec_string_run_refused`; `test_spec_timeout_bounds`; `test_spec_missing_name_sentence`; `test_spec_not_yaml_sentence`; `test_export_uses_main_not_a_branch`; `test_export_has_no_git_dir`; `test_failed_fetch_refuses_never_stale`; `test_bad_repo_name_never_reaches_git`; `test_caps_report_jobs_only_when_able`; `test_sensitive_box_never_reports_jobs`.
- [ ] Run; they fail. Implement. Run; they pass. Commit.

### Task 3: The box runs a job

**Files:** Modify `runner/ec2/cloud_runner.py`, `runner.cfn.yaml`, `update_runner.sh` or `--install-runners`, `runner/ec2/README.md`. Test: `runner/ec2/tests/test_plain_jobs.py`, and a test that reads the shell/template text.

**Interfaces:**
- `_run_job(runner_id, turn, emit) -> tuple[bool, str]` is called from `_run_turn` in place of `execute_prompt` when `turn["job"]` is set, after `_stage_connections`. It returns `(ok, text)`, with `cli_session_id=""`.
- Tokens: `/var/lib/canopy-jobs/<turn_id>/tokens/<provider>.json`, written by piping to a command running as `canopy-job` (`umask 077`), same format as the staged file. Refreshed from the staged file on a 60 s tick, atomically (`mv`).
- Running: `sudo -n -u canopy-job env -i <the six vars> <argv>` with `cwd` the export and `start_new_session=True`. Output is streamed through `emit` in batches every 5 s, cut at 1 MB in total with one line saying so; exact token values are replaced by `[token]` before anything leaves the box.
- End: on any exit, kill everything of the user (`sudo -n -u canopy-job pkill -KILL -u canopy-job`; a timeout sends TERM first, then KILL after 10 s), remove `/var/lib/canopy-jobs/<turn_id>`, unstage. `result_note`: exit code plus the last lines.
- The box: `canopy-job` created at install: a system user, `/var/lib/canopy-jobs` owned by it, `0700`. A sudoers drop-in lets the runner's user run as `canopy-job` only. An `iptables` owner rule refuses that user the instance metadata address `169.254.169.254`, and any loopback service the box runs; add it wherever the egress lock's rules live. Read `runner/ec2/README.md` § Egress lock, and `kms_broker.py` / `aws_broker.py` for how they admit callers; confirm from the code that `canopy-job` is refused by each, and that egress rules still apply to it. Runner homes, token dirs and `~/.canopy` must not be readable to it: check the modes `runner.cfn.yaml` sets, and tighten any that aren't `0700`.

- [ ] Tests: `test_job_env_is_exactly_the_six`; `test_no_runner_token_or_model_key_in_env`; `test_argv_passed_with_no_shell`, with a value containing `; rm -rf`; `test_job_runs_in_its_export`; `test_exit_zero_done`; `test_non_zero_failed_with_last_lines`; `test_timeout_kills_and_fails`; `test_background_process_doesnt_outlive_job`; `test_output_capped_at_1mb_with_a_line`; `test_token_value_scrubbed_from_output`; `test_token_copy_refreshed_on_tick`; `test_cleanup_on_every_path`, including a raise in the middle; `test_transcript_ship_skipped`; `test_template_creates_user_rule_and_metadata_block`; `test_broker_refuses_other_users`, or a report saying what proves it.
- [ ] Run; they fail. Implement. A real `canopy-job` isn't available in tests: one stub seam stands for `sudo`. Run `runner/ec2` tests; pass. Commit.

### Task 4: The screen, the docs, and everything

**Files:** Modify `frontend/src/pages/agents/TriggersSection.tsx` and its test, `apps/harness/api_schedules.py` if needed; `docs/architecture/runner-flags-and-schedules.md`, `runners.md`, `api-surface.md`, `deploy.md`, `vault.md`.

Copy: a schedule's "What it does" has two choices: "Run {agent}" and "Run a job (no AI)". The job choice asks "Which repo" and "Which job", with the sentence: "Runs the job of that name from the repo's `.canopy/jobs.yaml` on `main`, on a cloud machine, using {agent}'s connections." The list shows "Job: {name} from {repo}". A job waiting because no box can take it says "Waiting for a cloud machine that can run jobs." The turn's page shows output and result as a plain log, no chat box.

Docs: how to write a job, with an example reading `$CANOPY_JOB_TOKENS/salesforce.json`, and that repo dependencies are the job's to install; what a job can't reach; the box rollout (`update_runner.sh`, boxes without the user don't take jobs).

- [ ] Tests: choosing a job sends `job` and `job_repo` and no prompt; the sentence appears; the list row; a waiting job's sentence; a bad name's sentence from the server reaches the form; an ordinary schedule form is unchanged.
- [ ] Run; they fail. Implement. Then the full backend, runner, frontend and e2e suites as CI does. Everything passes.
- [ ] Commit.
