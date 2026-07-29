# runner/ — everything that executes agent turns

The runner family: the programs that pair with canopy-web's control plane
(`apps/harness`), claim queued `Turn`s, and execute them. None of this ships in
the web image (see `.dockerignore`); the server never imports it.

| dir | what it is |
|---|---|
| `canopy_runner/` | The **laptop runner** — the emdash-coupled daemon installed as a `uv` tool (never run from a checkout). Install/update: `runner/canopy_runner/scripts/install-runner.sh`. See `docs/superpowers/specs/2026-07-28-runner-as-installed-package-design.md`. |
| `ec2/` | The **cloud runner** — a stdlib-only single file (`cloud_runner.py`) delivered to the EC2 box via Secrets Manager (`up.sh` publishes it), plus its CloudFormation template and agent-fleet bootstrap. |
| `canopy_acp/` | Django-free [Agent Client Protocol](https://github.com/agentclientprotocol) client — the cloud runner's ACP executor (`RUNNER_EXECUTOR=acp`). |
| `menubar/` | Native macOS menu-bar control surface for the laptop runner (Swift/AppKit). |

## What deliberately does NOT live here

The shared libraries stay in `packages/`, because the **server imports them
too** — moving them under `runner/` would misstate who owns them:

- `packages/canopy_cron` — cron slot math; the server's schedule preview and the
  runner's firing call the same `next_slots()`/`due_slot()` so they can't drift.
- `packages/canopy_transcript` — Claude Code transcript core (path resolution,
  tailing, ordinals); shared verbatim by both runners and `apps/canopy_sessions`.
