# An agent acts on GitHub as its owner — a delegation, not a box credential

**Status:** shipped (fixes canopy-web#747). Decided by Jonathan, 2026-09-24.

## The problem

Cloud runners held one shared fine-grained PAT (`op://Canopy-Shared/github-token`,
owner jjackson, no `pull_requests:write`). `wire.sh` staged it into
`RunnerCredential.github_token_enc`; `cloud_runner._stage_github_token` exported it
as `GH_TOKEN` into the runner's own environment, which every turn inherits, and
wrote a global `~/.git-credentials`. So every agent on a box pushed as one person
with one token's reach, could push branches and could not open a pull request, and
committed as `Ubuntu <ubuntu@ip-172-31-17-196.ec2.internal>` because nothing set a
git identity. Live casualty: Echo's `echo/blog-style-flw-llo-portfolio-link`
(2 commits, no PR) from a partner's thread on 2026-09-23.

## The decision

**An agent's GitHub writes use its OWNER's identity**, lent to that one agent. Not
the caller's (the person who emailed the agent), and there is no token-layer rail
stopping a contact from causing a write: a self-improvement turn triggered by a
contact's email is legitimate, and stopping an agent doing something bad is the
agent's own design (gating hooks, sender triage). Who caused the change is
*recorded* instead: the turn's `requested_by`, surfaced as `CANOPY_REQUESTED_BY`
for a `Requested-by:` trailer.

## Three kinds of credential, not two

| Kind | Belongs to | Lives in |
|---|---|---|
| The agent's own | the agent (its mailbox; an account of its own) | the agent's 1Password vault |
| The tenant's | the workspace (the Google OAuth clients) | the workspace's shared vault |
| **Delegated** | **a person, lent to one agent** | **`AgentDelegation` (person, agent, service)** |

The 2026-09-05 credentials spec named the third case and put it out of scope
("`chrome-sales` acts on behalf of the dispatching human … an agent vault is the
wrong home"). GitHub is the same shape, and Salesforce is next.

Why not a vault item: the vault is the agent's own identity. A token that acts as
Jonathan, in `Agent-Echo`, would travel with Echo to its next owner, and there would
be no answer to "what have I let my agents do as me". A delegation is USED only while
its person owns the agent (`delegations.delegation_for`); transfer the agent and it
stops, and the new owner is asked for their own.

## Why a pasted fine-grained token, and why no GitHub App

Explored and rejected on the way here, in order:

1. **The owner's `canopy-agents` GitHub App user token.** Automatic, but a
   user-to-server token cannot be down-scoped: it reaches every repo in the app's
   installation that the person can reach. Minting it per turn changes *when* it
   exists, not *what* it can do. And adding repos to an org installation needed an
   org owner's approval.
2. **Per-turn installation tokens down-scoped to the agent's repos.** Enforceable,
   but they act as `canopy-agents[bot]`, and need the app's private key.
3. **A fine-grained PAT per agent (chosen).** The only credential that is BOTH "acts
   as me" AND "only these repos" — its repository selection IS the agent's scope,
   enforced by GitHub. GitHub has no API to create one, so canopy opens GitHub's form
   pre-filled (`delegations.create_url`, the 2025-08-26 template-URL parameters) and
   the owner picks the repos and pastes the result. External users need no app.

Whether two agents share one token is the owner's business; canopy stores one row
each and never compares them.

## How it runs

- **Set:** `PUT /api/agents/{slug}/github` (owner only). Checked before it is stored:
  GitHub must accept it (`/user` → login, name, id, expiry header) and it must be able
  to open a pull request on the agent's repo. Refused with GitHub's reason otherwise —
  the pre-filled form's resource owner has been reported to fall back to the personal
  account on submit (community #188111), and this is where that is caught.
- **The probe:** `POST /repos/{r}/pulls` from a head branch that cannot exist. GitHub
  checks permission before it validates the branch: 422 = allowed (nothing created),
  403 = no `pull_requests:write`, 404 = cannot see the repo.
- **Per turn:** `POST /api/harness/runners/{rid}/turns/{tid}/github-token`. Only a turn
  this runner claimed and is executing; canopy resolves the principal (turn → agent →
  owner → delegation); 409 with the reason when there is none or it expired. No
  fallback. A turn with no agent (repo chat, project turn) has no GitHub identity.
- **On the box:** the token goes into THAT turn's environment only (`GH_TOKEN`,
  `GITHUB_TOKEN`, a git credential helper via `GIT_CONFIG_COUNT/KEY/VALUE` that reads
  `$GH_TOKEN`, and `GIT_AUTHOR_*`/`GIT_COMMITTER_*` as the owner's GitHub noreply
  identity). Any GitHub variable from the runner's own environment or the agent's
  `.env` is stripped. The runner deletes the shared era's `~/.git-credentials` and
  global `credential.helper store` at every start.
- **Bootstrap clones** (the agent's private repo, and private plugin repos it installs
  from) use the same owner token, via `credentials/resolve` — so the token must also
  cover the repos an agent's plugins come from. canopy, canopy-web and the gogcli
  release are public and need nothing.
- **Readiness:** the box asks `GET /api/harness/runners/{rid}/github-readiness` after
  every bootstrap; canopy re-probes each assigned agent's token live, and each becomes
  a `github.<slug>` health check — a missing, expired or under-scoped grant is red at
  boot, not a 403 mid-turn. The readiness drill runs the same probe from inside a turn.

## Known limits

- On one box every turn is the same OS user, so a turn could read another's process
  environment. Same limit as confined turns (who-is-asking spec §7).
- The owner's token is long-lived (up to a year). Expiry is shown two weeks ahead on
  the agent's settings and turns the box's health check yellow; nothing auto-renews,
  because GitHub offers no way to.
- Skill history still reads repos with the owner's `canopy-agents` App grant; moving
  it onto the delegation and retiring the App is a follow-up.
