# Box usage report and session reconcile — design

Date: 2026-10-18. Asked for by Jonathan after the token-history backfill (#1631):
"make this more streamlined. And then there could be a separate, manually
triggered, check for local sessions missing from canopy … and ask if the user
wants to send the missing sessions up" — scoped to "sessions from agents or
projects canopy knows about".

## Why

Tokens reached canopy three ways (turn transcripts' `result`, interactive turn
transcripts, chat rows carrying usage) and recovering history meant re-shipping
whole transcripts. Every box already has every Claude Code transcript on disk.

## Design

1. **Usage report (automatic).** Each box scans `~/.claude/projects/*/*.jsonl`
   whose `cwd` maps to a repo it declares (`capabilities.projects`, which holds the
   agent repos too) and posts, per Claude session, per UTC day: tokens
   (input/output/cache read/cache write), the main model, message count; plus the
   session's repo, title (its first prompt, trimmed) and first/last activity. No
   content. Incremental: a file is re-reported only when it changed; a report
   replaces that session's days (idempotent). One shared scanner in
   `canopy_transcript` for both runners.
2. **One token source.** `SessionUsage(runner, claude_session, day)`, matched on
   write to canopy's own record of that Claude session —
   `RunnerBinding.transcript_id`, `Turn.session_key` / `cli_session_id`,
   `Session.cli_session_id` — and attributed from it (workspace, agent, repository,
   person: the turn's initiator, else the session's creator). Analytics reads only
   this. `TurnUsage`, the turn-transcript and chat-row usage paths, `_fill_usage`
   and `request_usage_backfill` are removed.
3. **Reconcile (manual).** A reported session that matches nothing is *missing*:
   it has no workspace and is visible only to the runner's owner and admins. On
   Health → Runners, the runner's admins can **Check for missing sessions** (asks
   the box for a full rescan: a durable `usage_scan_requested_at` drained on its
   heartbeat, the doorbell rule), see the list (repo, title, when, tokens), pick
   which to **Send up**. Sending creates a runner Session in the repo's workspace
   with a binding keyed `claude:<uuid>` and asks for the ordinary backfill, which
   ships the transcript; the session's usage rows become matched.

## Decisions (builder's, check these)

- Scope: only repos the box declares; nothing else on the laptop is read.
- A missing session's workspace is where the report sweep puts that box's
  sessions (the same rule, reused), shown before sending.
- History: a first report covers every transcript still on disk — no backfill run.
- Existing `TurnUsage` rows are dropped; the first reports reproduce them.
