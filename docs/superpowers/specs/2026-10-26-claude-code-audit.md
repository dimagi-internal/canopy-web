# Audit: how canopy drives Claude Code, and where a first-party mechanism now fits better

Checked against Claude Code **2.1.310** (installed locally), `claude -p --help`, live probes run on this machine (results quoted below), and the current docs at code.claude.com (fetched 2026-10-26). Read-only: no repo files were changed.

Ranked by value divided by effort. Each finding gives what canopy does now, the better mechanism, the benefit, the risk, and a confidence level.

---

## 1. Read the CLI's structured error and rate-limit fields, not its text. This already misses a real dead token.

**What canopy does now.** The `claude -p` stream loop (`runner/ec2/cloud_runner.py:5025-5027`) reads only `result` and `is_error`. Whether a credential is capped or dead is decided by text matching: `_is_usage_cap` / `_is_auth_required` (`cloud_runner.py:7143-7212`), with copies in `packages/canopy_transcript/canopy_transcript/usage_limit.py` and `signed_out.py`. The reset time is parsed out of prose like "resets 2:30am (America/Denver)". The cli path ignores `rate_limit_event` entirely. Only the ACP path reads `_claude/rateLimit` (`canopy_acp/updates.py:223`).

**Bug found, reproduced live.** With an invalid `CLAUDE_CODE_OAUTH_TOKEN`, CLI 2.1.310 emits:
- `system/api_retry` with `error: "authentication_failed"`, `error_status: 401`
- an assistant message with `error: "authentication_failed"`, `is_api_error_message: true`, `api_error_status: 401`
- a result with `is_error: true`, `terminal_reason: "api_error"`, `api_error_status: 401`, and text `"Failed to authenticate. API Error: 401 OAuth access token is invalid."`

Run against that text, canopy's own matchers return `cap=False dead=False`. A revoked or invalid setup-token is therefore treated as neither capped nor dead:
- No failover happens.
- No auth-required alert is sent.
- The probe reports `unknown`, which counts as possibly working.

**Better mechanism.** Classify on fields the stream already carries:
- **Assistant `error`.** One of `authentication_failed | oauth_org_not_allowed | account_on_hold | billing_error | rate_limit | overloaded | …`.
- **Result fields.** `api_error_status` and `terminal_reason`.
- **`system/api_retry`.** Its `.error` category.
- **`rate_limit_event.rate_limit_info`.** `status: allowed | allowed_warning | rejected`, plus `resetsAt` as an epoch, `rateLimitType`, and `unifiedWindows.{five_hour,seven_day}.utilization`. The live probe showed this event on every successful turn, too.
- **The `StopFailure` hook.** It carries the same `error` enum on laptop sessions.

Docs:
- https://code.claude.com/docs/en/agent-sdk/typescript (`SDKAssistantMessageError`, `SDKRateLimitEvent`, `SDKResultMessage.api_error_status` / `terminal_reason`, `StopFailureHookInput`)
- https://code.claude.com/docs/en/headless ("Handle API retries")

Keep the text matchers only as a fallback.

**Benefit.**
- Fixes the missed-401 case.
- Gives an exact reset time with no timezone parsing.
- Gives utilization, so a box can route away at around 95% instead of discovering the wall.
- Removes about 300 lines of regex and their duplicated copies.

**Risk/cost.** Low: about 50 lines in the event loop, plus `_execute_once` returning a reason enum. On the laptop, transcript records carry `isApiErrorMessage`; check whether they also carry `error` before relying on it there.

**Confidence.** High. Reproduced live.

---

## 2. Replace the paid "Reply with OK" probe with zero-cost signals

**What canopy does now.** `_probe_entry` (`cloud_runner.py:7346-7392`) spends a real model call on every staging, every reset, and every ambiguous ACP refusal. Its answer is then classified by the same text matchers. On this machine one probe cost $0.031, and it loaded 7 SessionStart hooks, because it runs with the box's full plugin set.

**Better mechanism.**
- **`claude -p "/usage" --output-format stream-json --verbose`.** Measured: cost $0, `num_turns: 0`. It returns a structured `usage_report.rate_limits.limits[]` with `percent`, `resets_at` (ISO), `is_active` and `severity` for the session window, the weekly window and each model's weekly window. Doc: https://code.claude.com/docs/en/agent-sdk/typescript (`usage_report` / `SDKUsageReport`). **Caveat:** the field is attached only for claude.ai logins with the `user:profile` scope. A `setup-token` token carries only `user:inference`, so it gets the text version instead, and the text still names percentages and resets. It does fit `CLAUDE_CONFIG_DIR` box logins (`claude auth login`).
- **`claude auth status --json`.** It reports `loggedIn` and `authMethod` for free. Measured: it says `loggedIn: true` for a bogus env token, because it checks only that a token is present. So it detects "no login at all" on a stored-login directory or the laptop, not a revoked token.
- **The `rate_limit_event` on every real turn** (finding 1) keeps a credential's state current at no extra cost.

**Benefit.** No usage spent on health checks. Exact per-window headroom.

**Risk/cost.** Low. The probe stays only as a last resort for setup-tokens.

**Confidence.** Medium-high. Measured live; the scope caveat is documented.

---

## 3. Stop a `claude -p` turn with SIGINT, not SIGTERM

**What canopy does now.** `_signal_cli` (`cloud_runner.py:6300-6311`) calls `proc.terminate()` (SIGTERM) for a polite stop, then SIGKILL.

**Better mechanism.** The docs say SIGTERM "leaves the turn that was in progress unfinished and records no result for it… To end the turn instead, send SIGINT, or call the Agent SDK's `interrupt()`." Send `proc.send_signal(signal.SIGINT)` first, then SIGTERM, then SIGKILL. Doc: https://code.claude.com/docs/en/headless ("Stop a run with SIGTERM").

**Benefit.**
- A stopped turn ends with a real `result` event, so canopy gets its text, cost and `permission_denials`.
- The transcript closes cleanly for the next `--resume`.

**Risk/cost.** A few lines.

**Confidence.** High.

---

## 4. Load each agent's plugin from the turn's own checkout (`--plugin-dir`) instead of the auto-updated cache

**What canopy does now.**
- Agent skills load from `~/.claude/plugins/cache/<agent>/<version>/`.
- `fleet_session_start_update.py` (canopy plugin, an async SessionStart hook) git-pulls marketplaces, mirrors source into a cache directory and **patches `installed_plugins.json` by hand**.
- Because it is async, the turn that triggers an update still runs the old skill. This is the publish-then-autoUpdate race recorded in memory (ada's 404 on 2026-07-21).
- The cloud box also runs `claude plugin marketplace update` and `plugin update` only at bootstrap (`runner/ec2/bootstrap_agents.sh:411-440`).

**Better mechanism.** The cloud runner already puts each turn's working directory at a fresh detached worktree of the agent repo at `origin/main` (`cloud_runner.py:5605-5631`), and the agent repo is a plugin root (`echo/.claude-plugin/plugin.json` exists). Pass `--plugin-dir <that worktree>`. The loading reference says an `--plugin-dir` plugin "replaces a same-named installed marketplace plugin… silently". The skills then match the code at the exact SHA the turn runs, so there is no cache, no race and no registry patching.

Also:
- Check `system/init.plugins` and `plugin_errors` to fail loudly when a plugin does not load.
- Where the cache must stay, set `CLAUDE_CODE_SYNC_PLUGIN_INSTALL=1` so installs finish before the first turn.

Docs:
- https://code.claude.com/docs/en/plugins/loading ("Name conflicts")
- https://code.claude.com/docs/en/plugins/create
- https://code.claude.com/docs/en/headless ("Fail CI when a plugin… doesn't load")
- https://code.claude.com/docs/en/env-vars

**Benefit.**
- Deletes the hand-rolled updater's riskiest part: writing Claude Code's internal registry.
- Removes a whole class of "stale skill on the turn right after a merge" failures.

**Risk/cost.** Medium.
- A worktree with local edits is kept rather than reset (`cloud_runner.py:5606`), so its uncommitted skill or hook edits would go live for that agent's next turns. Load from a clean, read-only export at the SHA instead.
- The `canopy` plugin itself (guards and hooks) should stay installed or locked (finding 5), because a managed `enabledPlugins` lock blocks a `--plugin-dir` copy of the same name. That is fine, since only agent plugins move.
- The laptop/emdash path cannot pass flags. `CLAUDE_CODE_PLUGIN_DIRS` (2.1.280+) is the env equivalent if emdash forwards the environment.

**Confidence.** Medium-high.

---

## 5. On cloud boxes, put the guards and denies in `managed-settings.json`, which an agent cannot override

**What canopy does now.**
- The fail-closed confinement layer is a plugin PreToolUse hook (`profile_guard.py`, `gating_guard.py`).
- `native_permissions.py:5-6` itself notes that a plugin update, a malformed hooks.json or `disableAllHooks` can take it out.
- Native deny rules go in through `--settings` or the worktree's `.claude/settings.json`.
- Turns run under `--dangerously-skip-permissions`, which, per the sandboxing doc, also skips protected-path checks. So an agent can edit its own repo's `.claude/settings.json` (echo's already defines hooks). A `disableAllHooks: true` there turns off every user, project and plugin hook, profile_guard included.
- Nothing in `runner/` or the specs uses managed settings.

**Better mechanism.** canopy owns the EC2 box, so it can write `/etc/claude-code/managed-settings.json` (the Linux managed-settings path; confirm it against the managed-settings page). In it:
- Force-enable `canopy@canopy` in managed `enabledPlugins`. Hooks from plugins that managed settings force-enable keep running even when another settings file sets `disableAllHooks`.
- Optionally set `allowManagedHooksOnly` for cx-/sensitive boxes.
- Put the box-wide credential-directory denies (`_sensitive_data_read_denies`) there.
- Set `permissions.disableBypassPermissionsMode` where bypass must never apply.

"Nothing in your own settings files or `--settings` overrides a managed key."

Docs:
- https://code.claude.com/docs/en/settings-reference (`disableAllHooks`, `allowManagedHooksOnly`, `allowManagedPermissionRulesOnly`)
- https://code.claude.com/docs/en/settings ("Settings precedence")

**Benefit.** The confinement layer stops depending on files the confined agent can write.

**Risk/cost.** Medium.
- Managed settings apply to every Claude process on the box, including credential probes and setup-token mints. Test the per-unit (`UNIT`) layout.
- Do not lock agent plugins there (it conflicts with finding 4).

**Confidence.** Medium-high. The precedence is documented; the exact interaction with a project-level `disableAllHooks` on plugin hooks was read in the docs, not measured.

---

## 6. Use Claude Code's OS sandbox for confined turns that grant Bash

**What canopy does now.**
- For confined (cx-) turns that allow some Bash, the boundary is profile_guard's argv matching plus `Bash(prog …)` deny rules.
- `native_permissions.py` says plainly that a Bash deny "isn't a security boundary around the program".
- Sensitive-data mode sidesteps the problem by denying Bash outright and locking egress with a proxy and iptables (`cloud_runner.py:3828-3900`).

**Better mechanism.** Set `sandbox.enabled: true`, `failIfUnavailable: true` and `allowUnsandboxedCommands: false` in the turn's settings (or in managed settings), then add:
- `filesystem.allowWrite` built from the capability's `write_paths`
- `filesystem.denyRead` for `~/.canopy`, `~/.claude/projects` and `~/.ssh`
- `sandbox.credentials` with deny or mask entries for env-var secrets
- `network.allowedDomains` plus `strictAllowlist`

It is OS-enforced (bubblewrap on Linux) on Bash and every child process, and it still applies under `--dangerously-skip-permissions`. Doc: https://code.claude.com/docs/en/sandboxing

**Benefit.** A caller's turn that is allowed `python3 script.py` can no longer read the box's tokens through that script. Today only the argv matcher stands in the way.

**Risk/cost.** Medium.
- It needs `bubblewrap` and `socat` in the AMI.
- Ubuntu 24.04 AppArmor may block user namespaces.
- It covers Bash only, not Read/Edit/WebFetch or MCP, so the permission rules stay.
- Tools that need network (`gh`, `git push`) need their domains allowed.
- It does not replace the egress lock, which also covers non-Bash traffic and is stronger. Keep that.

**Confidence.** Medium.

---

## 7. Background tasks in one-shot turns: drop the deny-hook

**What canopy does now.** `one_shot_turn_guard.py` (canopy plugin) refuses `run_in_background` Bash and Agent calls, Monitor, ScheduleWakeup and CronCreate on every cloud turn. The default executor is `cli` (`cloud_runner.py:3628`).

**Better mechanism.**
- **On the cli executor the premise no longer holds.** `claude -p` "can stay open to wait for background work that Claude started": background commands, subagents, Monitor watches and `/loop` wakeups. It then takes one more turn with the outcome, and its idle ceiling is set by `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`.
- **Where background must stay off (ACP),** set `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`. That removes the `run_in_background` parameter from the tools, so the model never tries it and never gets refused.

Docs:
- https://code.claude.com/docs/en/headless ("Background tasks at exit")
- https://code.claude.com/docs/en/env-vars

**Benefit.** cli turns get parallelism back, and the model stops burning turns on refused calls.

**Risk/cost.** Low.
- The turn's lease and stop handling must tolerate the longer process lifetime.
- The env var may not cover Monitor, ScheduleWakeup or CronCreate (the doc row is truncated where it discusses Monitor), so keep the hook for those.

**Confidence.** High on the documented behavior; medium on how completely the env var covers them.

---

## 8. `claude agents --json` is the supported way to list live sessions and their state

**What canopy does now.**
- canopy's duplicate-turn check (`live-turns.sh`) lists sessions from process argv (`_claude_cmd` docstring, `cloud_runner.py:1187-1205`).
- The laptop works out "working, blocked or idle" from a loopback hook listener (`HookListener._in_turn`), Notification markers and reading the emdash screen over CDP.

**Better mechanism.** `claude agents --json [--all]`. Tested:
- It lists both emdash-launched interactive sessions and a `claude -p` session, each with `pid`, `cwd`, `sessionId` and `status: busy | idle`.
- Per the docs it also gives `waitingFor` (`permission prompt`, `input needed`, `sandbox request`, `dialog open`) and `state` (`working | blocked | done | failed | stopped`).
- `blocked` includes "an error only you can clear such as an expired login".

Doc: https://code.claude.com/docs/en/agent-view ("Read session state from a script")

**Benefit.**
- Replaces process-table scraping.
- Gives a first-party "blocked on a permission prompt" signal without CDP focus-stealing.
- Gives a signed-out signal for laptop sessions.

**Risk/cost.** Low to medium. It is a CLI call per poll, and how stable `waitingFor` is for emdash-hosted TUIs needs checking.

**Confidence.** Medium. Listing was verified; `waitingFor` and `state` come from the docs.

---

## 9. Ask a human without screen-scraping or keystrokes

**What canopy does now.**
- **Cloud.** Asking a person works only on the ACP executor (`elicitation/create`, `session/request_permission`). The cli executor, which is the default and is mandatory for sensitive-data mode, can never ask: under `-p` with no permission host, AskUserQuestion is not even offered.
- **Laptop.** Answers are typed into the TUI over CDP, after re-reading the screen (`menu.py`, 605 lines; `blocked-agent-menus.md`).

**Better mechanism.**
- **Cloud cli.** Pass `--permission-prompt-tool mcp__plugin_canopy_canopy-web__<ask_tool>`, a canopy-web MCP tool that posts to `/session-menu` and long-polls `/menu-answers`. This brings AskUserQuestion and permission asks to cli turns, sensitive-data turns included, since canopy-web's MCP is already their one server. Use `--permission-prompts none` on turns nobody watches.
- **Laptop.** A blocking `PreToolUse` hook on `AskUserQuestion` can return `permissionDecision: "allow"` with `updatedInput.answers`. That answers the question with no keystroke. `PermissionRequest` hooks can allow or deny on the person's behalf. An HTTP hook (`type: "http"`) can point straight at the existing loopback listener.

Docs:
- https://code.claude.com/docs/en/hooks (PreToolUse "allow with updatedInput", "Defer a tool call for later", PermissionRequest, HTTP hooks)
- https://code.claude.com/docs/en/headless ("Turn off permission prompts in unattended runs")

**Benefit.**
- Cloud: one ask path for both executors.
- Laptop: no screen parser, no stale-number-into-the-prompt hazard, no composer lock.

**Risk/cost.** Medium.
- A blocking hook ties up the session while the person decides; command hooks default to a 600 s timeout, so raise it.
- On the laptop, a human at the TUI can no longer answer locally while the hook holds it. Enable it only for runner-driven sessions.

**Confidence.** Medium.

---

## 10. Strategic: one Agent SDK executor instead of `claude -p` parsing plus an ACP adapter

**What canopy does now.** Two executors:
- A hand-rolled `claude -p` stream parser (about 400 lines around `cloud_runner.py:4798-5100`), whose own comment calls it "canopy's own protocol, hand-rolled" (`:3616`).
- `canopy_acp` over the Node `@agentclientprotocol/claude-agent-acp` adapter, which itself wraps the Agent SDK.

Sensitive-data mode is forced onto cli because the ACP adapter "takes no settings document" (`:3688-3691`).

**Better mechanism.** The Python Agent SDK's `ClaudeAgentOptions` takes all of these:
- configuration: `settings`, `setting_sources`, `sandbox`, `plugins`
- permissions: `permission_mode`, `disallowed_tools`, `can_use_tool` (the ask-a-human callback)
- session control: `resume`, `fork_session`, `max_budget_usd`, `include_partial_messages`
- environment: `env`, `cli_path`

It also gives typed `RateLimitEvent` and `AssistantMessage.error`, `interrupt()`, streaming input for follow-ups mid-turn, and session APIs (`listSessions`, `getSessionMessages`, `forkSession`). Docs:
- https://code.claude.com/docs/en/agent-sdk/python
- https://code.claude.com/docs/en/agent-sdk/sessions

**Benefit.**
- One executor in place of two.
- Sensitive-data turns could ask a person.
- The Node adapter dependency goes away.

**Risk/cost.** High.
- `cloud_runner.py` is deliberately stdlib-only and shipped as a bare directory, so this means a packaging change.
- It is a rewrite of a battle-tested path.

Do findings 1, 3 and 9 first; they capture most of the value through the CLI.

**Confidence.** Medium.

---

## 11. Strategic, laptop: `claude --bg` + `claude attach` versus driving emdash over CDP

**What canopy does now.** The laptop runner types into emdash's xterm over CDP (`execute.py` at 1431 lines, `cdp/`, `delivery.py`, `emdash.py`) and tails `~/.claude/projects/*.jsonl`. The ACP spec kept the laptop on emdash because a canopy-spawned session would lose the human's ability to jump in.

**Better mechanism.** First-party background sessions under Claude Code's own supervisor:
- `claude --bg "<prompt>"` starts one.
- `claude --resume <id> --bg "<prompt>"` sends a follow-up to a session that is not running.
- `claude attach <id>` and agent view restore the jump-in.
- `claude agents --json` gives state.

Doc: https://code.claude.com/docs/en/agent-view

**Benefit.** It could delete CDP injection, the screen parser and the 17-xterm selection problem.

**Risk/cost.** High, and there is a real gap. Sending a follow-up to a session while it is mid-turn is not scriptable: `--resume --bg` on a running session "starts a copy", and peek-reply exists only in the TUI. It also gives up emdash's UI.

Worth a spike, not a commitment.

**Confidence.** Low-medium.

---

## Where canopy's approach is already the best available

- **Caller context through a `UserPromptSubmit` hook's `additionalContext`** (`caller_context.py`) is right. `--append-system-prompt` would be wrong here: `--system-prompt-snapshot` is on by default and records the system prompt once per conversation, "even when a later launch passes different text". A resumed chat would keep the first caller's identity. Keep the hook. The cloud runner could add `--include-hook-events` to verify that it fired.
- **Credentials.** `claude setup-token` with `CLAUDE_CODE_OAUTH_TOKEN`, and `CLAUDE_CONFIG_DIR` per stored login, is exactly the documented headless path (https://code.claude.com/docs/en/authentication). Driving the pty-based `setup-token` mint has no non-interactive first-party alternative. One optional addition: `apiKeyHelper` could replace staging an API key into the environment for vault-held keys.
- **The egress lock** (proxy plus iptables) is stronger than the sandbox's network filter, which covers only Bash. Keep it.
- **Deny rules.** canopy already applies them carefully: `Edit(path)` not `Write(path)`, `deniedMcpServers`, `//` absolute paths, and denies holding under bypass. This matches current docs.
- **Laptop transcript tailing** is forced by emdash owning the process. The cloud runner already uses the stream rather than files.

## Suggested order

1. Findings 1 and 3: an afternoon each, and they fix a live failover bug.
2. Finding 2.
3. Findings 7 and 8.
4. Findings 4 and 5 together: decide the plugin-locking split.
5. Findings 6 and 9.
6. Findings 10 and 11 as spikes.
