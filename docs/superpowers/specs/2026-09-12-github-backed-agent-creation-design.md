# GitHub-backed agent creation — "create agent" means a real repo, from a browser

**Date:** 2026-09-12
**Status:** Design — not implemented
**Companion:** `2026-09-12-opening-canopy-to-other-people-design.md` — first run, the
self-documenting app, the public explainer. That spec's agents empty state is what this
one turns into a button.
**Builds on:** `2026-09-05-agent-credentials-design.md` — provisioning an agent without
the box or 1Password. GitHub is the last thing that still needs a terminal.

## Problem

> *"It doesn't mean anything (currently) to create an agent with no repo, so yes, the
> results of creating an agent would be creating the proper GitHub repo and properly
> linking the agent."* — Jonathan, 2026-09-12

`POST /api/agents/` exists and no UI calls it, which the companion spec records as a gap.
But wiring a button to it would be worse than leaving it alone: an agent record with no
repo has no persona, no domain skills and no hooks, so it cannot take a turn. The record
is not the agent; the repo is.

This is the same sentence one dependency later than the credentials spec, which opens:

> *"Someone could plausibly create a completely new agent without direct access to the
> cloud box or 1Password. A mailbox was the last thing that could not be provisioned
> that way — minting a gog token meant a terminal, `gog auth login`, a loopback
> listener, and then a hand-written 1Password item. This turns it into a button."*

The mailbox became a button. The repo did not: the factory
(`canopy/src/orchestrator/agent_factory.py`) does `git init` and a local commit, and
getting that onto GitHub is still a human running `gh repo create` on a laptop.

**The requirement that shapes the design:** the credential is the *user's*, and the repo
goes wherever they point it — *"this should be designed so it's not just Dimagi users who
can use this in theory."* So: no shared service account, no hardcoded organisation.

## The auth decision, and the research behind it

**A GitHub App, authorized per-user through the user-to-server web flow.** Not an OAuth
App. GitHub's own guidance is explicit: *"In general, GitHub Apps are preferred to OAuth
apps because they use fine-grained permissions, give more control over which repositories
the app can access, and use short-lived tokens."*

Checked against current docs on 2026-09-12, because this area moved recently and a wrong
answer here is a credential with the wrong blast radius:

| Endpoint | Permission | Token types |
|---|---|---|
| `POST /user/repos` — a repo under the user's own account | `Administration: write` | **UAT** |
| `POST /orgs/{org}/repos` — a repo in any org | `Administration: write` | UAT + IAT |
| `POST /repos/{owner}/{repo}/generate` — from a template | `Contents: write` (+ more) | UAT + IAT |

So **one GitHub App with a user access token covers both personal-account and
organisation targets.** There is no need to combine an App with an OAuth App, and no
fallback path. Recorded because the obvious research says otherwise: GitHub's community
thread on creating repos with an App user token is unresolved and inactive since May 2024,
and its answer — "combine the functionality of a GitHub App and an OAuth App, using OAuth
`repo` scope" — is wrong for our case. That thread is about someone holding only
`Contents` permission, which was never going to create a repository.

**What changed recently, and why it doesn't change the answer.** In August 2026 OAuth
Apps gained token rotation (8-hour tokens, 6-month refresh via `offline_access`), up to
10 redirect URIs, and wildcard redirect matching — closing the token-lifetime gap that
used to be the main argument for Apps. It explicitly did **not** change the permission
model: OAuth Apps still use coarse scopes. That is decisive here. We need exactly
`Administration: write`; the OAuth route would mean `repo`, which is read and write on
every private repository that person can see. For Dimagi staff, that is every private
Dimagi repo, stored in canopy-web, to serve a button pressed once per agent.

**Register the App as public.** Since June 2025, private and EMU-owned apps restrict
sign-in to members of the owning organisation or enterprise. An App registered private
under Dimagi cannot be authorized by anyone outside it, which would silently defeat the
"not just Dimagi users" requirement. This is one checkbox at registration and it is
confusing to diagnose afterwards.

Sources: [GitHub Apps vs OAuth Apps](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/differences-between-github-apps-and-oauth-apps) ·
[Permissions required for GitHub Apps](https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps) ·
[User access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app) ·
[OAuth token refresh and multiple redirect URIs, Aug 2026](https://github.blog/changelog/2026-08-14-multiple-redirect-uris-and-token-refresh-for-oauth-apps/) ·
[Security updates for apps and API access, Jun 2025](https://github.blog/changelog/2025-06-24-security-updates-for-apps-and-api-access/)

## Decisions

1. **A public GitHub App with `Administration: write`, `Contents: write`, `Metadata: read`.**
   Nothing broader. `Contents: write` is what pushes the scaffold; `Administration: write`
   is what creates the repo.
2. **The grant is per-user, and it does not go in the agent vault.** The credentials spec
   puts per-user secrets explicitly out of scope — *"`chrome-sales` acts on behalf of the
   dispatching human, not the agent… an agent vault is the wrong home and this does not
   become one."* A human's GitHub token is exactly that case, so it gets its own per-user
   model rather than an `AgentCredential` row. It reuses the Fernet helper
   (`apps/common/encryption.py::encrypt_secret`) and none of the agent-vault semantics.
   This is consistent with the amendment's rule for what canopy-web may hold at all:
   only secrets it mints itself, which a token from its own OAuth flow is.
3. **Store the refresh token and treat the access token as disposable.** User access
   tokens expire in 8 hours with a 6-month refresh token. Refresh on use; a failed
   refresh surfaces as "reconnect GitHub" in settings rather than an opaque 401 during
   agent creation.
4. **The owner is chosen by the user, from where they have actually installed the App.**
   `GET /user/installations` after authorization gives the real list of accounts and orgs;
   the create form renders it as a dropdown with an "install somewhere else" link. No
   hardcoded org, and no asking the user to type an owner name that may not work.
5. **The factory becomes a shared package, not a second implementation.** Extract
   `agent_factory.py` and its templates into a library both the canopy plugin and
   canopy-web depend on. This is the fifth instance of an established pattern here —
   `packages/canopy_agent_runs`, `canopy_cron`, `canopy_runtime`, `canopy_transcript` are
   already editable path deps wired through `[tool.uv.sources]`. Rejected alternatives: a
   GitHub template repo (the scaffold would then exist twice and drift), and dispatching a
   turn to a runner (zero new credentials, but a brand-new user has no runner, so the
   first agent could never be created — which is the entire point of the web path).
6. **Create the repo empty, then push the scaffold.** Keeps the GitHub call trivial and
   keeps the scaffold's content entirely inside the factory package.

## Shape

1. `/settings` gains **Connect GitHub** — the user-to-server web flow, alongside the
   existing Claude-subscription connect block, which is the same shape of thing.
2. The callback stores the encrypted refresh token against the user and records their
   GitHub login.
3. `/w/:ws/agents` **Create agent** asks for: slug, display name, owner (from
   installations), and visibility.
4. Server side: run the factory → create the repo → push → `POST`-equivalent write of the
   `Agent` row with its `repo_url` → return the agent's workspace URL.
5. Failure is partial by nature (a repo can exist while the push fails). The operation
   reports which steps completed and is safe to retry: creating an agent whose repo
   already exists adopts it rather than erroring, matching `POST /api/agents/`'s existing
   upsert-by-slug semantics.

## What is still not a button after this

The scaffold is a skeleton — the `create-agent` skill's own words. Persona and domain
skills are written by a human with an agent, in a session, afterwards. This spec makes the
*repo* a button, not the agent's judgment. Worth stating plainly on the success screen so
nobody concludes their new agent is finished.

## Testing

- **The factory package keeps its own test suite** on extraction, plain pytest, no Django
  — the precedent `packages/canopy_agent_runs` already sets.
- **The GitHub calls are tested against a fake**, with one live end-to-end run recorded
  by hand against a throwaway repo. Per `2026-08-01-clicking-does-nothing-incident.md`:
  in-process tests with both ends faked are the right shape for seams and are exactly
  what passes while the live chain is broken.
- **A 30-minute live check before building the credential layer**: a throwaway App on a
  personal account, `POST /user/repos` with a user access token. The docs say it works and
  the permissions table above is authoritative, but this is cheap and it is the one
  assumption the whole design rests on.
- **Scope assertion**: a test that fails if the App's requested permission set grows
  beyond the three declared above. Permission creep on an App thousands of repos could be
  installed on is worth a tripwire.

## Out of scope

- **Anything the agent itself pushes.** After creation, the agent's own commits go through
  its runner's `gh` auth. This credential is for provisioning only, used once.
- **Repo settings beyond creation** — rulesets, merge queue, branch protection. The
  factory can gain them later; `Administration: write` already permits it.
- **Deleting or transferring repos.** Not requested, and a destructive capability on a
  public App is worth not having.
- **Per-user secrets generally.** This adds one per-user credential for one purpose. It is
  not the beginning of a per-user vault, and the credentials spec's reasoning against that
  still holds.
