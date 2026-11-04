# Local apps

A **local app** is a real program from Canopy's app store that runs on a person's own computer, started by them, with its agentic work done through normal Canopy sessions. The first is Meeting assistant: it reads a running meeting's transcript and sends batches to an agent's session. Design and reasons: `docs/superpowers/specs/2026-11-03-local-apps-design.md`. Writing an app: `local_apps/README.md`.

## What it shares with workspace apps

The catalog: an approved version is an `AppTemplateVersion` of an `AppTemplate` with `runtime="local"`. Every workspace-app query that reads `collections` or `pages` filters to `runtime="web"`. Approvals are shared too (kind `local_app.release`). Installs aren't, because a local app belongs to one person on one computer.

## Publishing

1. `canopy-runner app pack` makes a deterministic zip; `app submit` (or `POST /api/local-apps/submissions/`) sends it. The server never runs it: `package.read` checks limits, paths and the manifest, and `package.facts` lists what reaches the network or starts processes.
2. Submitting files the approval. Its snapshot names the sha256. The review page (`/w/<ws>/apps/local/review/<id>`) shows the diff from the last release. Approving needs a superuser who didn't submit it. A lone superuser can't submit, since nobody could approve, so an agent or another person does.
3. Approving creates the immutable catalog version, and the app lists for every workspace with local apps switched on.
4. Revoking (`POST …/submissions/<id>/revoke`) refuses Start and Canopy calls at once, and running copies stop at their next heartbeat.

## Updates

At each Start, the runner runs the newest good version if its **trust surface** (`trust.py`: notice, disclosure, canopy, consumes, provides, entry, platforms) is what the person accepted. If that changed, they stay on what they accepted until they accept the differences, unless it's been revoked.

## Who and what

- `Workspace.settings.local_apps`, off by default, set by `integrations`. Off also stops running copies.
- Only a person's own laptop runner (`kind=emdash`, their `owner`). `private` isn't the test, because most laptops also serve fleet work.
- A web page can install, configure and stop an app, never start one.

## How a run acts as the person

The app holds no credential. The host calls `/api/harness/runners/{runner}/local-apps/…`, under the runner protocol's existing path, so `runner_auth` gained no new door. Each call is allowed by an installation the person made while signed in. The agent comes from that row and is re-checked with `access.decide`, a `sessions.send` reaches only sessions the run opened, and only ops the manifest declares work. Runs store numeric counts and ids, never anything said.

## What it doesn't protect against

An app runs as the person and can read what they can read, including the runner's own credentials on disk. `disclosure` isn't enforced. What protects people is that only reviewed code is released. There's no signature beyond TLS, the server's sha256 and the approval record, because a compromised canopy-web can already start agent turns on these computers.
