# A project's files

A project can keep **files**: a git repository canopy-web hosts for the project, holding the tooling it shares with whoever works on it. That means its skills, the scripts and templates those skills run, and their docs. It is the project-side twin of a [stored agent](stored-agents.md)'s definition, on the same git layer (`apps/agents/definition_git.py`, here with `owner="project_id"`). Start reading at `apps/agents/project_files.py` (module docstring); the routes are `apps/agents/api_project_files.py`, the models `apps/agents/models/project_files.py`, and the page section `frontend/src/pages/work/ProjectFiles.tsx`.

Shared tooling belongs to a project, not to one agent: an agent that needs it joins the project as a member agent, and the project's vault (`work.md` § What a project serves) gives its turns the secrets the tooling uses. A project's skills can still be `ProjectService` rows with their instructions inline. A skill that runs code keeps that code here.

## The repository

- **One repository per project, in the database.** Objects are `ProjectGitObject` rows and refs `ProjectGitRef` rows, scoped to the project; git's hashing, so the repository is byte-compatible with any git host.
- **Refs.** `refs/heads/main` is the live published files, `refs/heads/draft` the one draft, `refs/tags/r<N>` published revision N.
- **A revision is a commit.** `ProjectFilesRevision` carries `number`, `status`, `commit`, its `tree` and `root_sha256`, who wrote it (`author_user`, `author_agent` when an agent's login wrote it) and who published it. At most one draft and one published revision per project (two partial unique constraints). Rows are never deleted; a rollback is a new revision with an old one's files.
- **The layout** is a plugin's: a folder per skill under skills/, holding its SKILL.md and the files beside it, plus a README.md and docs/. A skill's `description:` is read from its SKILL.md frontmatter (`project_files.skills`).

## The file policy

`project_files.check_tree` holds every tree canopy writes to the paths in `project_files.EDITABLE_GLOBS`: text only (UTF-8, no NUL), on the editable paths, no executable bit, no symlink, the stored-agent size caps (`canopy_sdk.agent_definition.MAX_*`), and nothing a secret detector matches (`canopy_sdk.agent_definition.secret_kinds`). A skill's own files may be scripts (`project_files.SKILL_EXTS`). A project's files are never executable: a script runs through its interpreter. A refused draft changes nothing and answers 422 `policy` with every problem.

## Who changes it

What is published runs in the turns of the project's member agents, with the secrets of every vault attached to the project. So:

- **Drafting** is anyone who may change the project (`gates.may_edit_project`: an editor of its workspace or its lead agent's, a person on it, the login of an agent leading or working on it). A draft is made on the live revision named as `base` (0 while nothing is live; 409 `stale_base` otherwise) and replaces any earlier draft.
- **Publishing and rolling back** need a person acting as themselves (`harness.acting_person.refusal` is None) who may change who is on the project: its lead, an admin on it, or an owner of its workspace (`project_members.may_manage`). These are exactly the people its vaults open to, so publishing gives nobody a secret they could not already read. Refused (403 `not_a_publisher`, with a `reason`): every agent's login, a turn's token, a conversation's caller token, and anyone else. A browser steps up with a passkey; a person's PAT is allowed. `expect_root` refuses a draft that changed after it was read (409 `changed`). Both are audited settings (`project:<address>/files`).
- **Discarding** a draft is its author, or someone who may publish it.
- **Reading.** Everyone who reads the project reads its live files; a draft or a discarded revision only those who may change it.

## How the files reach a session

Through the projects connection (`apps/mcp/project_gateway.py`), and only when the work asks: a project whose only toolbox is its files is listed once something is published; its card lists the stored skills beside its `ProjectService` skills and every live file; `project_skill` reads a stored skill's SKILL.md and names its files (a `ProjectService` skill of the same name wins); `project_file(project, path)` reads one live file. An agent writes a script to a scratch folder and runs it through its interpreter. A draft reaches no session.

## Not yet

No git endpoint serves a project's repository (an agent reads files through `project_file`); there is no convert, export or history import; an agent's draft files no approval request, it waits on the project page for a publisher.
