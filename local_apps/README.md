# Local apps

A local app is a `manifest.json` plus a program, run as its own process on a person's computer. The design is `docs/superpowers/specs/2026-11-03-local-apps-design.md`. This folder is source only: an app reaches people by being submitted and approved, not by being here.

**Write one.** Read JSON lines on stdin, write JSON lines on stdout, standard library only. You get `{"type":"start","settings":{...},"run_id":"..."}` first and must stop promptly on `{"type":"stop"}`, or the host kills you after 5 seconds. Say what's happening with `status` and `event`, and call Canopy with `canopy.call` for an op your manifest declares. You hold no credential and get only `PATH`, `HOME`, `LANG` and `TMPDIR`.

**Keep logs clean.** Event and status fields are numbers or short identifiers; the host replaces anything longer with its length. The one named exception is `session_url`.

**A source** declares `provides` and writes `segment`, then `ended` or `gone`. It owns its own cursor and quirks. An app declares `consumes` and must drop anything unsent on `gone`.

**Try it.** `canopy-runner app dev-install <folder>`, then `canopy-runner app start <key>`. Nothing leaves the computer: Canopy calls go to a local stub and what would have been sent lands in `~/.canopy/apps/<key>/would-send.jsonl`.

**Publish it.** `canopy-runner app pack <folder>` shows what would ship; `canopy-runner app submit <folder>` sends it for review, with `CANOPY_WEB_PAT` set. Bump `version` for every change.

**Test it.** Put tests in `tests/` with a file name unique across `local_apps/`; the main backend suite collects them, and packing leaves them out.
