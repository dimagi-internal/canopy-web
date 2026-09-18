# An agent's skill history, and an assistant that knows which skill you mean

**Status:** built on branch emdash/agent-skill-history-spec (PR pending).
**Prototype:** the approved design mockup — https://claude.ai/artifact/81X2S36cpLRqgkJTATRQ7c
(private; built from ACE's real git history on 2026-09-18).

## What this is for

ACE has 149 skills and 3,846 commits, and nobody can explain it by reading it. Its
git history, though, already records how every skill changed and — because commit
subjects here are written as the reason for the change — why: *"PDD must be a
native Google Doc, with a QA check that can see the format (#1061)"*, *"self-heal
seven defects found in hh-poverty-targeting/20260728-0705"*. Read in order they are
the account of how a skill got to its current shape. None of it is visible in
canopy.

So: a **History** section on every agent's workspace that shows that record, and
lets you drill from the whole agent to one phase, one skill, one commit. And
because a history is a thing you ask questions about, the in-app assistant knows
what you have selected — select `idea-to-pdd`, ask "explain what types of
improvements we've been making to this skill", and it answers about that skill.

**The page makes no claims.** Every label is a count, a date or a commit message.
It does not say the agent "learned" or "got better", and it does not name eras
("build", "harden") the data does not name. Interpretation is what the assistant
is for, when asked.

## Decisions

1. **canopy-web pulls from GitHub.** Not published by the agent, not pushed by a
   GitHub Action — nothing to install or run in any agent repo.
2. **It reads as the agent, through its owner's GitHub grant** — the viewer never
   needs to have connected GitHub. See *Whose credential* below; it is the one
   decision with a security edge.
3. **The in-app assistant answers**, not the agent being viewed. The existing
   Canopy AI widget reads the selection through the page contract and calls two
   new MCP tools. ACE's own turns and runner are untouched.

## Whose credential

canopy-web holds no GitHub credential that belongs to an *agent*. It holds two
that could be used:

| credential | scope | used here? |
|---|---|---|
| `Agent.owner`'s `GitHubConnection` (the canopy-agents GitHub App, user-to-server) | the repositories that one person chose on GitHub's installation screen | **yes** |
| `RunnerCredential.github_token` (a cloud runner's read-only token) | in practice the shared fleet token — every repo it can read | **no** |

The reason for refusing the second is a confused deputy. `repo_url` is editable by
any workspace **editor** (`upsert_agent`), and the sync runs with a credential the
triggering user does not hold. Whatever that credential can read, an editor can
aim canopy at by changing `repo_url` — and read its commit messages through this
page. With the owner's grant the reach is bounded by repositories the owner
explicitly handed to the app; with the fleet token it is every repository the
fleet can see. The 2026-09-12 spec already marks the shared token for replacement;
this feature should not deepen dependence on it.

Consequences, stated plainly on the page when they apply:

- **No owner, or the owner has not connected GitHub** → "History reads through the
  agent owner's GitHub connection. <owner> hasn't connected GitHub." Owner sees a
  Connect button.
- **The owner's installation does not include this repo** → name the repo and link
  the installation screen (`github_app.install_url()`). An empty installation list
  means access to nothing — the lesson from the 09-12 spec.
- **Residual, accepted:** an editor can still point `repo_url` at *another repo the
  owner granted*. That repo's owner chose to share it with canopy; the exposure is
  its `skills/*/SKILL.md` commit messages to members of this workspace.

The sync records which grant it used (`synced_with` = the owner's GitHub login), so
the page can say whose access produced what you are looking at.

**What the build added beyond this.** `repo_url` is restricted to exactly
`https://github.com/<owner>/<repo>[.git]` — validated before the sync claim, before
`access_token_for`, before any subprocess — because the confused-deputy risk above is
not just "which repo" but "which host": an editor names `repo_url`, and without this
check the owner's token would ride `http.extraheader` to whatever host that string
named. The token itself rides that header scoped to the clone alone (`-c
http.extraheader=Authorization: Bearer <token>`), never a URL, an exception message,
or `last_error`. `skill_revision_diff` applies the same discipline on the read side:
`sha` and `skill` are validated before either reaches GitHub's API, so a malformed
value can't be used to probe with the owner's credential.

## Shape

### 1. Sync — `apps/agents/skill_history.py`

Framework tier (`agents` is framework): nothing here is ACE-specific.

1. Resolve the credential: `github_app.access_token_for(agent.owner)`.
2. `git clone --bare --single-branch --branch <repo_ref>` into a `TemporaryDirectory`.
   The token rides `-c http.extraheader=Authorization: Bearer …`, never the URL, so
   it cannot land in a process list, an exception message or a log line. **Full
   clone, not `--filter=blob:none`:** a blobless clone makes `--numstat` fetch every
   blob lazily, one round trip each — measured, it did not finish in 5 minutes on
   ACE. A full clone of ACE is 23 MB and **1.6 s**.
3. `git log --reverse --format=<sha, iso date, subject, body> --numstat -- 'skills/*/SKILL.md'`
   — **0.2 s** on ACE. Line count after each revision is the running sum of
   added − deleted, so no historical file is ever materialised. A revision whose
   running count returns to 0 on a delete is a removal.
4. Grouping: read `agents/*.md` at HEAD and parse the YAML frontmatter. A file with
   `skills:` defines a group; `phase_ordinal` / `phase_display` order and name it;
   each entry's `qa_skill` / `eval_skill` attach a checking skill to the skill it
   checks. A skill named `X-qa` / `X-eval` with a sibling `X` attaches by
   convention. Everything no agent file claims goes in one **Not assigned to an
   agent** group, sorted by name. An agent repo with no `agents/` directory gets
   that single group — correct, just flat.
5. Replace this agent's rows wholesale in one transaction (the `AgentSkill`
   precedent: a cache of the repo, never edited in place), then `mark_dirty` the
   resource (§4).

Bounds: 60 s total timeout (clone + log), and a repo whose clone exceeds 200 MB is
refused with a message rather than filling the task's ephemeral disk. Both numbers
are an order of magnitude above ACE.

**Runs in the request.** No background worker exists and none is needed at 2 s. A
failed sync writes `last_error` and leaves the previous history intact.

**When it runs:** a **Sync from GitHub** button (editor+), and on page open when the
last successful sync is older than an hour (debounced server-side so ten open tabs
do not clone ten times: `sync_started_at` is claimed under `select_for_update`,
and a sync started less than 90 s ago for this agent returns the stored history
instead of cloning again — 90 s being above the 60 s timeout, so a crashed sync
cannot hold the claim forever).

### Models (`apps/agents`)

- `SkillHistorySync` — one per agent: `head_sha`, `synced_at`, `synced_with`
  (GitHub login), `last_error`, `sync_started_at`.
- `SkillHistoryCommit` — `agent` FK, `sha`, `committed_at`, `subject`, `body`.
- `SkillRevision` — `commit` FK, `skill`, `lines_after`, `added`, `deleted`.
- Group membership and checking-skill pairings are stored on the sync row as JSON:
  they are a HEAD-only snapshot, read whole, never queried by field.

Tenancy is inherited through `agent` (NOT NULL workspace); no new tenant column.

### 2. API

- `GET /api/agents/{slug}/skill-history/` — the compact payload the prototype
  already proved (groups, per-skill revision tuples, a deduplicated commit table):
  ~150 KB for ACE. Readable by anyone who can read the agent. Includes
  `synced_at`, `synced_with`, `head_sha`, `last_error`, and a `credential_state`
  (`ok` / `no_owner` / `owner_not_connected` / `repo_not_granted` / `no_repo`) the
  page renders the empty states from.
- `POST /api/agents/{slug}/skill-history/sync` — editor+. Returns the same payload.

Regenerate `generated.ts` (required check).

### 3. The page — `/w/:workspace/agents/:slug/history`

A new rail section, **History**, built on `canopy-ui` and semantic tokens (the
prototype's palette does not carry over). The prototype is the spec for layout and
interaction:

- Header counts as of the selected date: skills, revisions, skills with a QA or
  eval skill, skills removed.
- Timeline: play, ±1 week, slider, **Latest**; clicking the chart moves the date.
- Chart: skills in the repo over time, plus SKILL.md revisions per week, with the
  current selection's revisions highlighted.
- One row per group; one tile per skill (revision bar + checking-skill bar;
  states: not yet created, revised in the last 7 days, removed, selected).
- A panel that drills **All skills → group → skill → commit**, with a breadcrumb.
  Skill view: created / last revised / removed dates, SKILL.md length over time,
  checking skills (linked both ways), every revision newest first with its line
  change and commit subject; revisions after the selected date shown faded.
  Commit view: full subject, every skill it changed with the line change, and
  **Move timeline to this date**.

**Selection lives in the URL** — `?group=`, `?skill=`, `?commit=`, `?at=YYYY-MM-DD` —
so every view is linkable and the route-level page context already carries it.

### 4. The assistant knows the selection

This is the existing page contract (`docs/architecture/embedding-a-canopy-agent.md`
§5), applied; nothing about it is new machinery.

**Declared state.**

```ts
usePageState(() => describeSelection({
  backingTool: 'skill_history',
  resource: `skill-history://${slug}`,
  ids: commit ? [commit] : skill ? [skill] : group ? [group] : [],
  filters: { agent: slug, group, skill, commit, as_of: at },
}), [slug, group, skill, commit, at])
```

It rides the first message (so the answer works before MCP connects) and
`current_page` re-reads it on later turns.

**MCP tools** — `apps/mcp/tools/skill_history.py`, calling the same service layer
as the REST view, tested against the mounted server:

- `skill_history(agent, skill=None, group=None, since=None, until=None)` — revisions
  with date, subject, **body**, lines after, line change, and the skill's checking
  skills. Bodies matter: they are where the reason for a change is spelled out
  when the subject is terse. This is what "what kinds of improvements" is
  answered from. Capped at 300 revisions with a `truncated` flag.
- `skill_revision_diff(agent, sha, skill)` — the SKILL.md patch for one commit,
  fetched **live** from GitHub (`GET /repos/{o}/{r}/commits/{sha}`, that file's
  `patch`) through the same owner grant, truncated to 20 KB. Not stored: diffs are
  large, rarely wanted, and one call away.

Both run as the calling user and refuse an agent that user cannot read — the
authorizer rule, not a new check.

**Page actions** — what exists only in this tab, so the assistant can point:

- `selectSkill({ skill })`, `showCommit({ sha })`, `setTimeline({ date })`.
  "Show me the commit where it started reading reviewer comments" is a
  `skill_history` read followed by `showCommit`. Throw on an unknown skill or sha
  so the refusal reaches the agent.

**Invalidation.** Sync ends with `mark_dirty(f"skill-history://{slug}")`; the page
registers `useResource` for it, so a sync triggered from another tab, or by the
assistant, repaints this one.

**Page-context rule.** Add `history` to the agent-workspace section pattern in
`frontend/src/widget/pageContext.ts`.

### 5. Deploy

`git` is not in the runtime image. Add it to the `apt-get install` line in the
`Dockerfile`'s runtime stage (~30 MB).

## Testing

- **Parser against a real repository**, built in a temp dir inside the test with
  real `git` commits: creation, edits, a removal, a checking skill by frontmatter
  and by suffix, an unassigned skill, a repo with no `agents/`. No fake of git's
  output format — that format is the contract.
- **Credential resolution**: each `credential_state`, and that the viewer's own
  connection is never consulted (the test that pins decision 2).
- **Token never leaves the header**: a failing clone's error text and the stored
  `last_error` contain no token.
- **API**: access gate (non-member 404, viewer can read, viewer cannot sync).
- **MCP**: both tools asserted against the mounted server; `skill_revision_diff`
  against a stubbed GitHub response.
- **Frontend**: drill-down from all skills → commit and back; URL round-trip; the
  exact `usePageState` payload per selection level; each page action, including
  its refusal.
- **Live**: one sync of ACE on labs, then the question from the top of this doc
  asked in the widget with `idea-to-pdd` selected — asserting the answer cites that
  skill's commits, not merely that a reply arrived (the `e2e_session_chat.py`
  lesson).

## Out of scope

- **Skill renames** read as a removal plus a creation. `--follow` works on one path,
  not a glob.
- **Anything outside `skills/*/SKILL.md`** — a skill's scripts, references, agent
  files' own history.
- **Cross-agent comparison** (ACE vs echo on one page). The payload is per agent,
  so it composes later.
- **Scheduled syncs.** Opening the page is the trigger; a stale history costs one
  click.
- **Per-agent GitHub credentials** — the "on whose behalf" design the 09-12 spec
  left open. When it lands, the resolver in step 1 is the one place that changes.
