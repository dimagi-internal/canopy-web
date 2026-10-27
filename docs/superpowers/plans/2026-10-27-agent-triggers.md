# Agent triggers — plan

Spec: `docs/superpowers/specs/2026-10-27-agent-triggers-design.md`. One PR: the
migration converts system accounts before it drops them, so no deploy leaves alarm
mail without a trigger.

1. **Model** — `AgentSchedule`: `kind`, `sender_address`, `subject_pattern`,
   `turn_mode`; `cron` blank for `inbound_mail`; a check constraint on the shape.
2. **`apps/harness/triggers.py`** — validation, `accountable`, `refusal`,
   `for_inbound(agent, address, subject)`, `TriggerRefused`.
3. **Firing** — `schedule_turns.fire_schedule` / `run_schedule_now` refuse a stopped
   trigger (slot consumed, event recorded); every cron consumer (clock, runner
   sync/fire, week, `/schedules/`) reads `kind=cron` only.
4. **Mail** — `channels/email.arrive`: after the member check, an aligned message
   from a trigger's sender fires it (system initiator, accountable creator, contact
   along; `origin_ref.trigger_id`; trigger prompt leads). `member_behind_email` loses
   its system-account leg.
5. **Turn mode** — a trigger rung in `turn_mode._own_posture`.
6. **Provenance** — `initiator.describe` / caller envelope: `trigger` replaces
   `system_account`.
7. **Alerts** — `alerts_trigger.py` (`trigger_stopped`), registered.
8. **API** — `/agents/{slug}/triggers/` CRUD + run-now; services take `kind`; gates
   per spec C3.
9. **Migrations** — harness schema; harness data (convert system accounts, rewrite
   their turns); workspaces (delete users, drop models).
10. **Removal** — `system_accounts.py`, models, schemas, routes, every guard leg
    (grep), refusal hint, tests (`test_system_accounts.py` → `test_agent_triggers.py`).
11. **Frontend** — Triggers tab + editor; remove `SystemAccountsSection`, `system`
    member flags; `surfaces.ts`, `pages.md`; regenerate `generated.ts`.
12. **Docs** — `access.md` (Triggers replaces System accounts), `api-surface.md`,
    `roles.md`, `tenancy.md`, `vault.md`, `hcp-service.md`, `runners.md`.
13. **Verify** — app tests + guard tests, full `uv run pytest`, `npm run build`,
    vitest; PR, `--auto`, deploy, live checks.
