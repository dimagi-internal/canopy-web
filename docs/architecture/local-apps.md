# Local apps

A **local app** (in the product, a **user app**, on the **Personal apps** tab of the apps page) is a real program from Canopy's app store that a person installs for themselves, not for a workspace. Every one runs on their own computer, started by them, with its agentic work done through normal Canopy sessions. Start reading at `apps/local_apps/services.py` (module docstring), `apps/local_apps/api.py` and `apps/local_apps/trust.py`; writing an app: `local_apps/README.md`.

## What it shares with workspace apps

The catalog: an approved version is an `AppTemplateVersion` of an `AppTemplate` with `runtime="local"`. Every workspace-app query that reads `collections` or `pages` filters to `runtime="web"`. Approvals are shared too (kind `local_app.release`, `apps/local_apps/release_kind.py`). Installs aren't, because a local app belongs to one person on one computer.

## Publishing

`LOCAL_APPS_REQUIRE_APPROVAL` (`config/settings/base.py`, default off) decides whether a submission waits for approval. Off, a submission from anyone who can write content in any workspace is released at once by `services.submit`, shown as unreviewed, and reaches people at their next Start. An app's key stays with the workspace that first released it (`services.submit`), so only that workspace's people can change an app others already run. Steps 2 to 4 apply with the setting on.

1. `canopy-runner app pack` makes a deterministic zip; `app submit` (or `POST /api/local-apps/submissions/`) sends it. The server never runs it: `package.read` checks limits, paths and the manifest, and `package.facts` lists what reaches the network or starts processes.
2. Submitting files the approval. Its snapshot names the sha256, so what is approved is what ships. The review page (`/w/<ws>/apps/personal/review/<id>`) shows the diff from the last release. Approving needs a superuser who didn't submit it (`LocalAppRelease.may_approve`). A lone superuser can't submit, since nobody could approve, so an agent or another person does.
3. Approving creates the immutable catalog version (`services.release`), and the app lists for everyone.
4. Revoking (`POST …/submissions/<id>/revoke`, superuser) refuses Start and Canopy calls at once, and running copies stop at their next heartbeat (`services.stop_wanted`).

## Updates

At each Start, the runner runs the newest good version if its **trust surface** (`trust.py::_FIELDS`: notice, disclosure, canopy, source, entry, platforms) is what the person accepted. If that changed, they stay on what they accepted until they accept the differences, unless it's been revoked (`services.resolve`).

## Who and what

- Any signed-in person can see and install one, for themselves. There's no workspace switch. The agent they choose decides which workspace its sessions land in, and it can be in any workspace they're in (`services._usable_agent`).
- Only a person's own laptop runner (`kind=emdash`, their `owner`) runs one. `private` isn't the test, because most laptops also serve fleet work.
- A web page can install, configure and stop an app, never start one. Install, configure and accept refuse an agent acting for the person (`api.py::_the_person_themself`).

## How a person sees it

The menu bar app (`runner/menubar/Sources/main.swift`) shows a start screen on Start and a status window while a run goes; it reads `~/.canopy/apps/<key>/run.json` every second, with no process or network. Every word on both comes from the manifest's optional `presentation` (`apps/local_apps/manifest.py::_presentation`: `uses`, `steps`, `states`). It's copy, so it isn't part of the trust surface: rewording it asks nobody to accept again. `CanopyRunner --render-app-previews <dir>` draws every screen, in light and dark, to PNG for review.

A runner only runs a manifest whose keys it knows (`runner/canopy_runner/canopy_runner/local_apps/manifest.py::KNOWN`), so a version that adds a new key is published only once runners that know the key have shipped.

## How a run acts as the person

The app holds no credential. The host calls `/api/harness/runners/{runner}/local-apps/…`, under the runner protocol's existing path, so `runner_auth` needs no route of its own. Each call is allowed by an installation the person made while signed in. The agent comes from that row, a `sessions.send` reaches only sessions the run opened and still asks the session ACL, and only ops the manifest declares work. Each heartbeat and Canopy call re-checks the version and that the person may still use the agent (`services.stop_wanted`, `turn_context.for_asker`), so losing access stops a running copy. These runner routes aren't MCP tools (`apps/mcp/api_tools.py`), and an agent acting for a person can't install, configure or accept. Runs store numeric counts and ids, never anything said (`services._counts`).

## What it doesn't protect against

An app runs as the person and can read what they can read, including the runner's own credentials on disk. `disclosure` isn't enforced. What protects people is that only reviewed code is released. There's no signature beyond TLS, the server's sha256 and the approval record, because a compromised canopy-web can already start agent turns on these computers.
