# Stored agents

A **stored agent** is an agent with no git repository: canopy-web keeps its definition, a file tree, in revisions (`Agent.source == "stored"`, `repo_url` empty). A repo agent's definition is its repository (`apps/agents/definition.py`); everything downstream of "a git clone of the agent" still works for a stored one, because a runner or laptop **materializes** a revision into a local git repository with one deterministic commit (`canopy agent materialize`, in the canopy CLI). Start reading at `apps/agent_definitions/services.py` (module docstring) and `apps/agent_definitions/api.py`; the models are `apps/agents/models/definition.py`, the bytes `apps/agents/definition_store.py`.

## A revision

- `AgentDefinitionRevision.tree` is `{path: {sha256, size, executable}}`; each file's bytes are an `AgentDefinitionBlob` keyed by its sha256, in the attachment bucket or, with none configured, in the row (`definition_store.put_blob`). A blob is re-hashed on every read (`definition_store.read_blob`), so a revision never hands out bytes its manifest does not name.
- `root_sha256` names a revision: the sha256 of its canonical manifest (`canopy_sdk.agent_definition.manifest_root`, shared with the CLI, which checks every archive against it).
- Revisions are append-only: a draft becomes `published` (the old live one `superseded`) or `discarded`; a rollback is a new revision copying an old tree (`services.rollback`). `Agent.live_revision` is what runners run. At most one draft per agent (`one_agent_definition_draft`).
- The numbers are per agent and never reused; the generated plugin version is `0.<number>.0`.

## The file policy

`canopy_sdk.agent_definition.check_tree` is the policy, in the SDK so the CLI can ask it before uploading; `services._full_tree` enforces it on every write, server-side. A tree holds only the editable paths (`EDITABLE_GLOBS`: the top-level docs, `skills/<name>/…` text files, three `config/` files, `tests/` and `docs/` data files) plus the files canopy generates. Refused: traversal, absolute paths, backslashes, NUL, `.git/`, hooks, symlinks (the API has no way to describe one), any executable but `bin/<slug>-email`, more than 200 files, 256 KB a file or 2 MB in all, non-UTF-8 content, and secret-shaped strings (`SECRET_SHAPES`, kept in step with `apps/common/redact.py` by a test). A refused write changes nothing and answers 422 with every `problems` entry.

**Generated files** (`definition_store.generated_files`) are written by canopy on every revision and again at publish, never taken from an upload: `.claude-plugin/plugin.json` (name, version, description; never `hooks` or `mcpServers`), `.claude-plugin/marketplace.json` (`source: "./"`), `.claude/settings.json` (only `CANOPY_AGENT`), `bin/<slug>-email` from the agent factory, and `.gitignore`. A stored agent therefore runs only its skills, under canopy's own plugin.

## Who changes it

Changing a definition changes what runs with the agent's vault and credentials, so:

- **Drafting** is `agent_edit` on the agent (`agent_for_edit`), which its own login holds. Changes apply to the live revision named as `base` (409 `stale_base` otherwise) and replace any earlier draft.
- **An agent's draft asks a person.** A draft written by an agent (its own login, or any token a turn holds: `harness.acting_person.refusal`) records `by_agent` and the turn it came from, and files an `agent.definition.publish` approval (`apps/agent_definitions/publish_kind.py`) naming the draft by number and root sha256. Its approvers are the agent's admins who are people, from the request's page with a passkey (`Kind.step_up`); a draft replaced or discarded withdraws it, and one left open cannot publish anything else.
- **Publishing, rolling back and converting** are the agent's owner or an admin acting as themselves (`api._require_person`): refused to an agent's login, a turn's login, a caller or act-as token, and any request naming a turn. Publish re-renders the generated files, supersedes the old live revision and reads the skill catalog again from the new one (`definition_store.refresh_skill_catalog`).
- **Creating** one (`POST /api/agents/stored`) is the same gate as registering an agent: the factory's scaffold, less what only a repo needs (workflows, scripts, Python), becomes revision 1, published (`services.create_stored_agent`). **Converting** a repo agent (`…/definition/convert`) takes its tracked files as revision 1; any file outside the policy refuses the whole conversion, and uploaded generated files are ignored.

## How runners and laptops get it

`GET /api/agents/{slug}/definition/archive` is a gzip'd tar of one revision (regular files, 0644 or 0755, fixed times and owners, so a revision is the same bytes every time) with `X-Canopy-Revision`, `X-Canopy-Root-Sha256`, `X-Canopy-Published-At` and `X-Canopy-Workspace`. A runner's own token reaches it (`apps/harness/runner_auth.py`) for an agent its box may hold (`holding.runner_may_hold_agent`), and only a published revision: a box never runs a draft. A member reads any revision (`canopy agent use`). It carries no credential, so a confined turn's box can fetch it. It is not an MCP tool; an agent reads a file with `get_agent_definition_file`.

The materialized clone's commit is deterministic (author and committer canopy, both dates `X-Canopy-Published-At`, no parent), so the same revision is the same commit on every machine; untracked and ignored files (`.env`, state) survive a sync. That half is the canopy CLI's (`canopy agent materialize`, `canopy agent sync`).

## What a stored agent does not have

- **No repository.** `repo_url` stays empty: `set_repository` and the upsert refuse to give it one (409 `stored_agent`, `apps/agents/api.py::_refuse_repo_on_stored`), since that repo's code would run instead of what a person published. Where it comes from instead is `definition: {source, revision, root_sha256}` (`definition_store.source_of`) on the agent detail, `/runtime` and the credential resolve a box's `canopy agent prepare` reads.
- **No GitHub identity or repo step** in its setup checklist (`setup.setup_steps`), **no plugin-catalog entry** (`apps/plugins/services.py::member_agents`), and **no repo-internal ship grant** in its caller envelope (`harness/caller_context.py::_ship_grant`): it changes itself only by drafts.
- **No `canopy agent-publish` of skills** (`PUT …/skills/` is 409): its catalog is read from the live revision at each publish, and its skill history from its published revisions (`skill_history._sync_stored`), each a "commit" whose sha is the revision's root sha256. Its definition key is `canopy:<workspace>/<slug>` (`definition.agent_definition_key`), shared with no other instance.
