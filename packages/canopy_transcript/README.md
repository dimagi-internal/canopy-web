# canopy_transcript

The Claude Code transcript core, shared by canopy-web's server and both runners.

A Claude Code session — whether wrapped by emdash or run headless as
`claude -p` — writes the same JSONL transcript under `~/.claude/projects/`.
Everything downstream of "who supervises the process" is therefore the same
work, and this package is that work, in one place:

| module | what it owns |
|---|---|
| `paths` | resolving a transcript file, by either convention |
| `records` | reading JSONL records, best-effort |
| `tail` | incremental byte-offset reading of an append-only transcript |
| `rows` | block → chat row, composite ordinals, payload caps, NUL scrubbing |
| `batching` | byte-bounded batching for the retained-transcript endpoint |

It exists for the reason `canopy_cron` exists: the laptop runner and the cloud
runner had independently written the *same functions* (byte-identical project
dir encoding, two byte-chunkers, two block extractors), and one of them was
quietly better than the other. See
`docs/superpowers/specs/2026-07-27-runner-convergence-and-live-observability-design.md`.

**Django-free and dependency-free by design.** The server imports it for
`BLOCK_STRIDE` (which it previously documented in a comment rather than
importing), and the cloud runner installs it on an EC2 box at boot.
