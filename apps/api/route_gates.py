"""Every REST route, and the gate it enforces — declared, so a new one cannot arrive without one.

WHY THIS EXISTS. The ACL audit of 2026-10-02 did not find one bad gate; it found
the same mistake made route by route. Product surfaces let a `viewer` approve a
DDD gate and wipe a whole feed with `{}`; schedules and turns let a viewer
write a prompt and fire it at the fleet; six tenancy predicates had each grown a
NULL-means-allow leg. Each was a route whose author never wrote down who may
call it — the default was simply whatever the helper they reached for happened
to check, and "any member" is the cheapest helper there is. And since
2026-09-29 every route is also an MCP tool, so a route's gate is the gate on
every client holding a token.

So the rule is the one a reviewer cannot skip: **a new route fails CI until it
declares its gate here, and a write a VIEWER can make must also be listed in
`VIEWER_MAY_MUTATE` with its reason.** The point is not the manifest — it is
that the question gets asked at the moment the route is written, by the person
writing it, and the answer is visible in the diff.

What `tests/test_every_route_declares_its_gate.py` checks: every operationId in
the schema is here and nothing stale is; every label is in `VOCABULARY`;
and every
member-level POST/PUT/PATCH/DELETE is in `VIEWER_MAY_MUTATE`. What it cannot
check is that a label is TRUE — record what the view ENFORCES TODAY, not what it
should, having read the view and the helper it calls. A gate that looks wrong
is recorded as it is and fixed in the code; the entry changes with the fix.

A gate is a tuple. Several labels mean alternatives or conditions on one route
(an agent turn needs `agent.work`, a project turn only membership; a walkthrough
is changed by its uploader while an editor, or by a workspace owner). The
route's own code is the authority on how they combine; a short comment says so
where it is not obvious.

Vocabulary (closed — add to it deliberately, never in passing):

* ``anonymous`` — no auth at all (``auth=None``) and no token.
* ``token-link`` — anonymous allowed with a share/capability token (``?t=``),
  else member.
* ``authenticated`` — any signed-in user, not tenant-scoped (``/me``, requestable).
* ``self`` — signed in, and touches only the caller's OWN rows (PATs, push
  subscriptions, presence preference, shared transcripts).
* ``member`` — workspace membership (``permissions.READ``): a viewer passes.
* the capabilities of ``apps/workspaces/permissions.py``: ``content.write``,
  ``agent.work``, ``session.drive``, ``events.write``, ``logs.read``,
  ``members.manage``, ``integrations``, ``runners.route``, ``retention.manage``,
  ``admission.manage``, ``projects.decide``, ``own``.
* ``agent-admin`` — ``Agent.is_admin`` (its owner, a workspace owner, an
  ``AgentAdmin``).
* ``agent-owner`` — the agent's owner or a workspace owner (transfer, admin
  grants).
* ``agent-login`` — the agent's OWN canopy login (``Agent.user``), i.e. the
  agent calling with its own PAT, whatever its workspace role.
* ``session-acl`` — ``apps/canopy_sessions/access.py`` (read/write/share per chat).
* ``turn-content`` — ``apps/harness/turn_access.can_read_turn_content``.
* ``runner`` — the runner protocol: the runner's owner, or the box
  that claimed the turn.
* ``runner-admin`` — ``can_administer_runner``.
* ``runner-holds-agent`` — ``services.caller_runs_agent`` /
  ``runner_may_hold_agent``.
* ``license`` — an ACTIVE ``AgentLicense`` (``apps/agents/licenses.py``) and the
  caller's own live turn on that agent named in ``X-Canopy-Turn-Id``
  (``licenses.credential_principal``).
* ``contact`` — the ``/api/contact/`` surface, for contact principals.
* ``device-proof`` — no session or token: a proof from an ACTIVE runner device key
  (DPoP, or an EC2 instance role's signed STS call) bound to this request is the
  authority (``apps/harness/api_runner_sessions.py``).
* ``owner-token-and-key`` — no session: a token the runner's OWNER minted (revoked
  within 30 days is accepted) plus a proof of the key being registered; it only ever
  creates a PENDING key (``apps/harness/api_device_key_requests.py``).
* ``invited-role`` — no session or token: an EC2 instance role's signed STS call
  bound to this request, matched to a pairing invitation its creator APPROVED
  behind a passkey (or to that role's existing, approved runner of the same name)
  (``apps/harness/api_runner_invitations.py``).
* ``paired-key`` — no session or token: a DPoP proof by the box's own device key,
  bound to this request; opening a pairing makes nothing but the pairing, and
  claiming one also needs the one-time secret a person's passkey-gated approval
  handed to that box's loopback (``apps/harness/api_runner_pairings.py``).
* ``signed-link`` — a signed token in the URL is the authority (drill report,
  invite token, OAuth state).
* ``beta-reviewer`` — ``apps/beta_requests/services.may_review``: a superuser or
  the ``CANOPY_BETA_REQUESTS_TO`` address (a beta request names no workspace).
* ``superuser`` — ``User.is_superuser``: fleet-wide switches that span every tenant
  (the fleet hold).
* ``fleet-holder`` — an address in ``CANOPY_FLEET_HOLDERS`` (Ada): may SET the fleet
  hold, never release it (``apps/harness/api_runners.py::_may_hold_fleet``).
* ``host`` — a connected-site / machine protocol endpoint (assertion,
  jwt-bearer, Pub/Sub push).
* ``hcp-grant`` — the Human Context Protocol subject rule
  (``apps/contacts/hcp_api.py::_principal``): an agent reaches ONLY the person
  who started the turn it names, and only under that client's (agent, channel,
  host) grant, which the person can revoke. An OAuth client of the HCP service
  (``apps/contacts/hcp_oauth.py``) is held the same way: its ``hcpat_`` access
  token reaches ONLY the person whose grant it was issued under, within that
  grant's scopes and sources, and opens ``/api/hcp/v1/`` and nothing else.
* ``page-viewer`` — an agent's OWN login that a person let see their memory page
  (``apps/contacts/viewers.py::page_of``): reads that person's page and nothing
  else; never a caller token or an HCP client. Anyone else is 404.
* ``secret-request`` — ``apps/agents/secret_requests.may_answer``: someone who may
  answer an agent's OPEN request for one of its declared secrets — an agent admin,
  or a person who can write to the chat it was asked from (filling a MISSING value
  only); never an agent's own login. Seals ciphertext; never reads.
* ``caller-token`` — a confined turn's ``cct_`` caller token
  (``apps/harness/caller_tokens.py``): the turn is read from the token, never an
  argument. Used only by ``HAND_TOOL_GATES``.
* ``host-grant`` — a person's own DPoP-bound host-grant token, the only credential
  an MCP Apps View's button call carries (``apps/mcp/delegation.py``). Used only by
  ``HAND_TOOL_GATES``.

``HAND_TOOL_GATES`` (bottom of this file) is the same declaration for the MCP tools
that are not routes (``apps/mcp/tools/``): every hand-written tool names its gate
and whether it writes, and ``tests/test_every_route_declares_its_gate.py`` fails a
tool that is missing or stale. A hand-written tool that is a plain write should be
a route instead — it then gets the settings audit and the acting cap for free.
"""
from __future__ import annotations

VOCABULARY: frozenset[str] = frozenset({
    "anonymous", "token-link", "authenticated", "self", "member",
    "content.write", "agent.work", "session.drive", "events.write", "logs.read",
    "members.manage", "integrations", "runners.route", "retention.manage",
    "admission.manage", "projects.decide", "own",
    "agent-admin", "agent-owner", "agent-login", "session-acl", "turn-content",
    "runner", "runner-admin", "runner-holds-agent", "license",
    "contact", "signed-link", "host", "beta-reviewer", "superuser", "fleet-holder",
    "hcp-grant", "device-proof", "owner-token-and-key", "invited-role", "paired-key", "page-viewer",
    "secret-request", "caller-token", "host-grant",
})

GATES: dict[str, tuple[str, ...]] = {
    # --- apps/agents/api.py and its api_*.py siblings (routing, skills, board, credentials, readiness)
    "list_agents": ("member",),
    "upsert_agent": ("agent.work", "agent-admin"),  # editor in target ws; a MOVE also needs agent admin
    "move_agent": ("agent-admin", "agent.work"),  # admin of the agent where it is + editor in the destination
    "check_scaffold_repo": ("agent.work",),  # editor in the target ws; reads GitHub with the caller's own token
    "scaffold_agent": ("agent.work", "agent-admin"),  # editor in target ws; re-pushing a taken slug needs its admin
    "agent_setup": ("member",),
    "get_agent": ("member",),
    "link_canopy_user": ("agent-admin",),
    # + members.manage to CREATE the agent's login, own for one that has signed in
    # (services.mint_agent_canopy_token / agents/agent_login.py)
    "mint_agent_canopy_token": ("agent-admin",),
    # The same mint, stored sealed and never returned. Browser: + passkey step-up.
    "give_agent_canopy_login": ("agent-admin",),
    "transfer_owner": ("agent-owner",),  # leaving it ownerless needs OWN
    "get_interface": ("member",),
    "publish_interface": ("agent-admin",),
    "unpublish_interface": ("agent-admin",),
    "list_admins": ("member",),
    "agent_access": ("member",),
    # licenses to use the agent locally (apps/agents/credentials/licenses.py, canopy-web#1704)
    "list_agent_licenses": ("member",),
    "revoke_agent_license": ("agent-admin",),
    "approve_agent_license": ("agent-admin",),  # + access.decide must give them the whole agent (409)
    "grant_admin": ("agent-owner",),
    "revoke_admin": ("agent-owner",),
    "delete_agent": ("agent.work",),
    "set_turn_mode": ("agent.work",),
    "set_credential_source": ("agent-admin",),
    "set_repository": ("agent-admin",),  # what its boxes run: a DEFINITION_FIELDS change
    "set_device_binding": ("agent-admin",),
    "agent_credential_reads": ("agent-admin",),  # the read log; never a value
    "agent_device_keys": ("agent-admin",),  # can_manage per key = runners.may_trust_device_keys
    "agent_credential_alerts": ("agent-admin",),  # read-anomaly alerts; never a value
    "get_pat_rotation": ("agent-admin",),
    "set_pat_rotation": ("agent-admin",),  # turning it on also asks mint_agent_canopy_token's guard
    "rotate_pat_now": ("agent-admin",),  # browser: + passkey step-up; token allowed
    # zero-knowledge vault (apps/agents/credentials/zk.py): browser writes need a passkey step-up
    "get_agent_zk_state": ("agent-admin",),  # wraps are public; ?values= ciphertext, read-logged
    "enable_agent_zk": ("agent-admin",),  # browser: + step-up; token allowed
    "set_agent_zk_values": ("agent-admin",),  # browser: + step-up; token allowed
    "add_agent_zk_wraps": ("agent-admin",),  # browser: + step-up; token allowed
    "rotate_agent_zk": ("agent-admin",),  # browser: + step-up; token allowed
    # give an agent a secret (apps/agents/credentials/secret_requests.py): no route returns a value
    "request_agent_secret": ("agent-login", "agent-admin"),  # declared names only
    "get_agent_secret_request": ("agent-login", "agent-admin", "session-acl"),  # status, never a value
    "fulfil_agent_secret_request": ("agent-admin", "secret-request"),  # zks1 only; browser: + step-up
    "get_agent_zk_recipients": ("agent-admin", "secret-request"),  # public keys + endorsements
    "add_agent_zk_endorsements": ("agent-admin",),  # own vault key's signatures; browser: + step-up
    "set_slack_enabled": ("agent-admin",),
    "get_agent_runtime": ("member",),
    "list_agent_runners": ("member",),
    "get_agent_default_order": ("member",),
    "replace_agent_runners": ("agent.work", "runner-admin"),  # every ADDED runner administered + holdable
    "list_agent_runner_rules": ("member",),
    "replace_agent_runner_rules": ("agent.work", "runner-admin"),  # every ADDED runner administered
    "list_agent_actor_routes": ("member",),
    "set_agent_actor_route": ("agent.work", "runner-admin"),  # every ADDED runner administered by caller
    "set_agent_runner_rule": ("agent.work", "runner-admin"),  # every ADDED runner administered by caller
    "delete_agent_runner_rule": ("agent.work",),
    "delete_agent_actor_route": ("agent.work",),
    "list_syncs": ("member",),
    "create_sync": ("agent.work",),
    "delete_sync": ("agent.work",),
    "agents_list_turns": ("member", "turn-content"),  # content redacted per turn_access
    "create_turn": ("agent.work",),
    "list_skills": ("member",),
    "replace_skills": ("agent.work",),
    "get_skill_history": ("member",),  # may auto-sync (clone) on read
    "sync_skill_history": ("agent.work",),
    "skill_history": ("member",),
    "skill_revision_diff": ("member",),
    "list_projects": ("member",),
    "create_project": ("agent.work",),
    "get_project": ("member",),
    "patch_project": ("agent.work",),
    # --- apps/agents/contributors_api.py (hal T69): ideas + contributor pages
    "project_ideas": ("member",),  # page content withheld from an agent without the person's `use`
    # hal T82: the team view, and adding your OWN idea (a kind=idea task with
    # your words). Someone else's words go through create_tasks (agent.work).
    "project_team": ("member",),
    "create_idea": ("member",),
    "contributor_page": ("member",),  # the person must be known_in the workspace; read logged
    # An agent's login (a member) under the PERSON's record switch + its grant, or the person.
    "put_contributor_summary": ("member", "self"),
    "delete_contributor_summary": ("self", "agent-admin"),  # or the agent that wrote it
    "list_tasks": ("member",),
    "create_tasks": ("agent.work",),  # or the agent's own login
    "get_task": ("member",),
    "patch_task": ("agent.work",),
    "act_on_task": ("member", "agent.work"),  # approve/decline/reply = member; dispatch/done = editor, or the idea's own person marking a kind=idea task done (shipped)
    "list_task_actions": ("member",),
    "mark_task_action_applied": ("agent.work",),  # or the agent's own login
    # --- apps/agents/fleet_api.py  (fleet-wide lists, scoped to visible agents)
    "list_fleet_tasks": ("member",),
    "agent_roster": ("member",),  # counts over the same agents list_agents shows; turns through visible_turns_qs
    # --- apps/attention/api.py  (what is waiting on the caller; what they hide is their OWN list)
    "list_attention": ("self",),  # the caller's own list; each source applies its own gate: tasks by the board filter, chats by the session ACL, access requests by members.manage
    "hide_attention": ("self",),
    "unhide_attention": ("self",),
    "list_fleet_projects": ("member",),
    # User-level views (`/api/me/…`): the same list once per organisation
    # (`wsvc.each_organisation`), each run under that list's own gate.
    "list_my_projects": ("self",),  # the caller's own projects
    "list_my_tasks": ("member",),  # list_fleet_tasks per organisation
    "list_my_agents": ("member",),  # list_agents per organisation
    "list_workspace_projects": ("member",),
    "list_mcp_servers": ("member",),
    "get_workspace_project": ("member",),
    "create_workspace_project": ("agent.work",),  # the steward's write gate
    "patch_workspace_project": ("agent.work",),  # the steward's (and a new steward's) write gate
    "kb_upload_target": ("agent.work",),  # the steward's write gate: its curator publishes
    "kb_upload_originals": ("agent.work",),
    "publish_kb": ("agent.work",),
    "set_agent_credentials": ("agent-admin",),
    "agent_credential_status": ("member",),
    "agent_credential_access": ("member",),  # no values; reports the resolve gate for the caller
    "resolve_agent_credentials": ("runner-holds-agent", "agent-admin", "license"),  # bearer only
    "reveal_agent_credential": ("agent-admin",),  # browser session + passkey step-up only (apps/passkeys)
    "get_agent_vault": ("member",),
    "set_agent_vault": ("agent-admin",),
    "list_sender_trust": ("member",),
    "set_sender_trust": ("agent-admin",),
    "delete_sender_trust": ("agent-admin",),
    "get_agent_github": ("member",),
    "set_agent_github": ("agent-owner",),  # strictly the agent's own owner (delegations.set_github)
    "check_agent_github": ("member",),
    "delete_agent_github": ("member", "self"),  # removes only the caller's own delegation
    "get_agent_salesforce": ("member",),
    "set_agent_salesforce": ("agent-owner",),  # owner of BOTH borrower and lender (delegations.set_salesforce)
    "check_agent_salesforce": ("member",),
    "delete_agent_salesforce": ("member", "self"),  # removes only the caller's own delegation
    "delete_agent_credential": ("agent-admin",),
    "agent_readiness": ("member",),
    "post_bootstrap_report": ("runner-holds-agent",),
    # --- apps/agents/credentials/oauth_api.py
    "start_google_mint": ("agent-admin",),
    "google_callback": ("signed-link", "agent-admin"),  # signed state must name the caller
    # --- apps/agents/a2a_api.py
    "public_agent_card": ("anonymous",),  # only for agents with an outsider capability
    "extended_agent_card": ("authenticated",),  # per-caller card, any agent slug
    # --- apps/agent_runs/api.py
    "list_runs": ("member",),
    "create_run": ("agent.work",),
    "agent_runs_get_run": ("member",),
    "list_steps": ("member",),
    "record_gate": ("agent.work",),
    "record_verdict": ("agent.work",),
    "fork_run": ("agent.work",),
    # --- apps/harness/api_runners.py + api_session_sync.py: runner registry (the runner protocol speaks AS the owner)
    "pair_runner": ("member",),  # member of the explicit/default workspace; any role
    "set_runner_credential": ("runner-admin",),
    "swap_runner_logins": ("runner-admin",),
    "add_runner_login": ("runner-admin",),
    "remove_runner_login": ("runner-admin",),
    "order_runner_logins": ("runner-admin",),
    "report_runner_logins": ("runner",),
    "get_runner_credential_status": ("runner-admin",),
    "get_runner_credential": ("runner",),  # plaintext to the owner's token
    # --- apps/harness/api_device_keys.py (Canopy Vault device keys)
    "register_runner_device_key": ("runner-admin",),  # lands pending; on a private runner, owner only
    "create_runner_session": ("device-proof",),  # anonymous door; an ACTIVE key's proof is the authority
    "request_runner_device_key": ("owner-token-and-key",),  # stranded box: lands pending, owner told
    "list_runner_device_keys": ("runner-admin",),
    "approve_runner_device_key": ("runner-admin",),  # browser + passkey step-up; private runner: owner only
    "revoke_runner_device_key": ("runner-admin",),
    # --- apps/harness/api_runner_invitations.py (a new cloud box pairs by invitation)
    "create_runner_invitation": ("agent.work",),  # member (else 404) with agent.work in the named workspace
    "check_runner_invitation": ("agent.work",),  # the same gates as create, changing nothing (up.sh's pre-check)
    "get_runner_invitation": ("self",),  # the invitation's creator only
    "approve_runner_invitation": ("self",),  # its creator, browser session + passkey step-up; a token is refused
    # anonymous door: the box's instance-role proof, plus an invitation its creator
    # approved (or that role's existing, approved runner); one uniform 401 otherwise
    "pair_runner_by_role": ("invited-role",),
    # --- apps/harness/api_runner_pairings.py (a laptop box pairs by browser)
    # anonymous door: a proof of the box's key; makes only the pairing (or answers
    # `reattached` for a key that is already an ACTIVE runner's). Uniform 401.
    "create_runner_pairing": ("paired-key",),
    # anonymous door: the pairing's key + the approval's one-time secret; the
    # runner is the approver's, and only while they still hold agent.work there
    "claim_runner_pairing": ("paired-key",),
    # the code is the capability; workspaces listed are the caller's with agent.work
    "get_runner_pairing": ("authenticated",),
    # browser session + passkey step-up (a token is refused); member (else 404)
    # with agent.work in the chosen workspace; the runner will be the caller's
    "approve_runner_pairing": ("agent.work",),
    # anyone holding the code may refuse it; it grants nothing
    "decline_runner_pairing": ("authenticated",),
    # --- apps/harness/api_runner_releases.py (the deployed version's installers)
    "current_runner_release": ("authenticated",),  # a person, or a runner's own token (runner_auth)
    "runner_downloads": ("authenticated",),
    "turn_github_token": ("runner", "runner-holds-agent"),  # turn must be claimed by this box
    # only the box's own runner token (a PAT is 403), turn claimed by it and executing
    "turn_agent_token": ("runner", "runner-holds-agent"),
    "turn_attachment_content": ("runner", "runner-holds-agent"),  # an attachment of the chat a turn this box holds belongs to
    "runner_github_readiness": ("runner",),
    "runner_agents": ("runner",),
    "start_runner_mint": ("runner-admin",),
    "get_runner_mint": ("runner-admin",),
    "claim_runner_mint": ("runner",),
    "post_runner_mint_url": ("runner",),
    "post_runner_mint_code": ("runner-admin",),
    "post_runner_mint_result": ("runner",),
    "post_sign_in_request": ("runner",),  # pushes only to the runner's owner
    "post_sign_in_request_result": ("runner",),
    "list_my_sign_in_requests": ("self",),  # only requests put in front of the caller
    "list_runner_admins": ("runner-admin",),
    "grant_runner_admin": ("runner",),  # owner only (not RunnerAdmins)
    "revoke_runner_admin": ("runner",),
    "list_runner_workspaces": ("runner-admin",),  # an admin sees only workspaces it is a member of
    "enroll_runner_workspace": ("runner-admin",),  # + the runner's OWNER holds agent.work there (422)
    "unenroll_runner_workspace": ("runner-admin",),
    "set_runner_flags": ("runner-admin",),
    "set_runner_engine": ("runner-admin",),
    "set_runner_private": ("runner",),  # owner only (not RunnerAdmins)
    "list_runners": ("member",),  # runner_read_q: the tenant's fleet
    "update_runner_capabilities": ("runner",),
    "retire_runner": ("runner",),
    "unretire_runner": ("runner",),
    "pause_runner": ("runner",),
    "unpause_runner": ("runner",),
    "get_fleet_hold": ("authenticated",),
    "hold_fleet": ("superuser", "fleet-holder"),
    "release_fleet_hold": ("superuser",),
    "runner_heartbeat": ("runner",),
    "refresh_runner": ("runner-admin",),
    "runner_usage_scope": ("runner",),
    "post_runner_usage": ("runner",),
    "request_usage_scan": ("runner-admin",),
    "list_missing_sessions": ("runner-admin",),
    "send_up_missing_sessions": ("runner-admin",),
    "claim_turn": ("runner",),
    "resolve_session": ("runner", "member"),  # + membership of the agent / project workspace
    "record_session": ("runner", "member"),
    "report_sessions": ("runner",),
    "list_streams": ("runner",),
    "post_session_stream": ("runner",),  # session must be bound to this runner
    "list_backfills": ("runner",),
    "list_closes": ("runner",),
    "post_close_results": ("runner",),  # only this runner's bindings
    "list_menu_answers": ("runner",),
    "post_menu_answer_result": ("runner",),
    "post_session_menu": ("runner",),  # only a session this runner holds
    "claim_admission_notices": ("runner",),  # + runner_may_hold_agent per notice
    "report_admission_notice": ("runner",),  # only a notice this runner holds
    "post_session_backfill": ("runner",),  # session must be bound to this runner
    # --- apps/harness/api_turns.py: turns
    "list_unclaimable_turns": ("member", "session-acl", "turn-content"),  # prompt blanked unless turn-content
    "enqueue_turn": ("agent.work",),  # agent and project turns alike
    "harness_list_turns": ("member", "session-acl", "turn-content"),  # session turns by chat ACL; redacted
    "harness_list_sessions": ("member", "session-acl"),
    # --- apps/huddles/api.py: a derived view over the same visible turns as harness_list_turns
    "list_huddles": ("member", "session-acl"),  # anchors via visible_turns_qs; summary carries no content
    "get_huddle": ("member", "session-acl", "turn-content"),  # prompt/reply/digest only with turn-content
    # --- apps/agents/engagement_api.py: what people do with projects + huddles
    "record_engagement_view": ("member",),  # the project's agent workspace / the huddle's visible anchor
    "engagement_summary": ("member",),  # events of the caller's listed workspaces (the read rule)
    # --- apps/agents/proposals_api.py: proposed projects + backings (layer 1, 2026-10-16)
    "propose_project": ("agent.work", "agent-login"),  # an editor, or the agent itself
    "list_project_backings": ("member",),
    "back_project": ("member",),  # as yourself; an agent's login is refused (it backs as itself)
    "unback_project": ("member", "self"),
    "back_project_as_agent": ("agent-login",),  # the backer's own login, or the project's agent's
    # proposed TASKS in a project (check-ins, 2026-10-17): the same gates one level down
    "propose_task": ("agent.work", "agent-login"),  # an editor, or the project's agent itself
    "list_task_backings": ("member",),
    "back_task": ("member",),  # as yourself; an agent's login is refused (it backs as itself)
    "unback_task": ("member", "self"),
    "back_task_as_agent": ("agent-login",),  # the backer's own login, or the project's agent's
    # --- apps/huddles/checkins_api.py: project check-ins. None of these can start a channel read.
    # A write is the project's owner (a member who owns it) or a workspace owner/admin
    # (integrations); a NEW channel on an agent's read list is integrations only.
    "list_project_checkins": ("member",),
    "create_project_checkin": ("integrations", "member"),
    "get_project_checkin": ("member",),
    "update_project_checkin": ("integrations", "member"),
    "delete_project_checkin": ("integrations", "member"),
    "run_project_checkin_now": ("integrations", "member"),  # announces (or queues the turn); never reads
    "list_project_checkin_runs": ("member",),
    "list_checkins": ("member",),
    "get_checkin_run": ("member", "agent-login"),  # material included: members, and the project agent's login
    "finish_checkin_run": ("agent-login",),  # the project agent's own login only
    # --- apps/threads/api.py: bounded agent->agent conversations; messages are tagged turns
    "open_thread": ("agent.work",),  # editor in the moderator agent's workspace
    "list_threads": ("member",),  # threads in the caller's workspaces; no message content
    "get_thread": ("member", "session-acl", "turn-content"),  # messages via visible turns; prompt/reply only with turn-content
    "close_thread": ("agent.work",),  # editor in the thread's workspace
    "get_turn": ("member", "session-acl", "turn-content"),  # redacted unless turn-content
    "get_turn_caller_context": ("turn-content",),
    "append_turn_events": ("runner", "agent.work", "session-acl"),  # claimed: owner; unclaimed: agent.work / chat write
    "read_turn_messages": ("turn-content",),
    "read_turn_events": ("turn-content",),
    "append_turn_transcript": ("runner", "agent.work", "session-acl"),
    "read_turn_transcript": ("turn-content",),
    "start_turn": ("runner", "agent.work", "session-acl"),
    "finish_turn": ("runner", "agent.work", "session-acl"),  # a LOCAL turn: its initiator only
    # --- apps/harness/api_local.py: local use (a person's private runner + its local turns)
    "register_private_runner": ("self",),
    "list_private_runners": ("self",),
    "retire_private_runner": ("self",),
    "list_workspace_private_runners": ("member",),  # those local turns on the workspace's agents ran on
    # member of the agent's workspace (else 404), AND access.decide must give the
    # WHOLE agent (owner/admin, a `full:` rule, or a workspace editor) — else 403
    "open_local_turn": ("member",),
    "renew_local_turn": ("self",),  # the local turn's own initiator
    "cancel_turn": ("agent.work", "session-acl", "self"),  # agent/project: agent.work; chat: write access, or your own send
    # --- apps/harness/api.py: runner-fired schedules + drills
    "sync_schedules": ("runner",),
    "fire_schedule_route": ("runner",),
    "start_runner_drill": ("runner-admin", "agent-admin"),  # agent admin of each drilled agent unless runner owner
    "list_runner_drills": ("runner-admin", "logs.read"),
    "report_drill": ("signed-link", "runner"),  # ?t= link, the drilled agent's own login, or the owner
    # --- apps/harness/api_investigations.py — auto_debug.can_handle: the workspace's
    # log readers, the debugger agent's admins, or the debugger's own login (Agent.user)
    "list_failure_investigations": ("logs.read", "agent-admin"),
    "get_failure_investigation": ("logs.read", "agent-admin"),
    "resolve_failure_investigation": ("logs.read", "agent-admin"),
    # --- apps/harness/api_schedules.py
    "schedule_week": ("member",),
    "list_schedules": ("member",),
    "create_schedule": ("agent.work",),  # enforced in schedule_services._resolve_agent
    "preview_cron": ("member",),
    "update_schedule": ("agent.work",),
    "delete_schedule": ("agent.work",),
    "run_schedule_now": ("agent.work",),
    # --- apps/canopy_sessions/api.py  (access.py: tenant, then four legs; write = owner/editor role)
    "create_session": ("member",),  # wsvc.current_workspace; any member may start a chat
    "canopy_sessions_list_sessions": ("session-acl",),  # readable_sessions
    "list_my_sessions": ("session-acl",),  # the same list per organisation (wsvc.each_organisation)
    "reset_sessions": ("session-acl",),  # readable rows filtered to can_write
    "canopy_sessions_get_session": ("session-acl",),
    "list_messages": ("session-acl",),
    "search_sessions": ("session-acl",),  # readable_sessions, via the list's _filtered_sessions
    "list_human_inputs": ("session-acl",),  # _session_or_404 / _session_by_key_or_404, as get_session
    "archive_session": ("session-acl",),  # write
    "reset_session": ("session-acl",),  # write
    "unarchive_session": ("session-acl",),  # write
    "set_session_notify": ("session-acl",),  # write
    "set_session_title": ("session-acl",),  # write
    # filing.move: read gate, then the session's own person (creator / runner owner),
    # then content.write in the TARGET workspace; an agent's session is a 422.
    "move_session": ("session-acl", "content.write"),
    "list_transfer_requests": ("self", "runner-admin"),  # ones you asked for, or for boxes you administer
    "approve_transfer_request": ("runner-admin",),  # the target box's admins, re-checked at decision
    "decline_transfer_request": ("runner-admin",),
    "cancel_transfer_request": ("self",),  # only whoever asked
    "list_participants": ("session-acl",),
    "get_session_info": ("session-acl",),
    "add_participant": ("session-acl",),  # can_share (chat owner)
    "remove_participant": ("session-acl",),  # can_share, or self-removal
    "canopy_sessions_send": ("session-acl",),  # write
    "place": ("session-acl",),  # write
    "transfer": ("session-acl",),  # write
    "fork_session": ("session-acl",),  # write
    "export_session": ("session-acl", "self"),  # visible AND you started it
    "answer_menu": ("session-acl",),  # write
    "close_session": ("session-acl",),  # write
    "detach_session_from_slack": ("session-acl", "agent-login"),  # write; or the agent's own live turn
    "share_session_to_slack": ("session-acl", "agent-login"),  # write; or the agent's own live turn
    "audit_session_noise": ("session-acl",),  # rows of sessions the caller can read
    "purge_session_noise": ("session-acl",),  # write; readable sessions, write scope when applied
    "start_new_slack_thread": ("session-acl", "agent-login"),  # write; or the agent's own live turn
    "stop_session_turn": ("session-acl",),  # write
    "attach_session": ("session-acl",),  # READ gate on a POST (viewer signal)
    "detach_session": ("session-acl",),  # READ gate on a POST
    "request_backfill": ("session-acl",),  # READ gate on a POST
    "upload_attachment": ("session-acl",),  # write
    "attachment_content": ("session-acl",),  # can_read
    "delete_attachment": ("session-acl",),  # can_write
    "canopy_sessions_declare_page_actions": ("session-acl",),  # write
    "list_page_actions": ("session-acl",),
    "canopy_sessions_declare_page_state": ("session-acl",),  # write
    # MCP Apps View plumbing (apps/mcp_apps_host/mcp_apps_views.py): a session reader may
    # load a View read-only; every act (call/read/context/message) needs a WRITE
    # role in the session and the viewer's OWN host grant, and a PAT is refused.
    "app_view_resource": ("session-acl",),
    "app_view_call": ("session-acl",),  # write + the viewer's own host grant
    "app_view_read": ("session-acl",),  # the viewer's own host grant
    "app_view_context": ("session-acl",),  # write
    "app_view_message": ("session-acl",),  # write
    "declare_run_input": ("session-acl",),  # write
    "read_page_state": ("session-acl",),
    "invoke_page_action": ("session-acl",),  # write
    "canopy_sessions_resolve_page_action": ("session-acl",),  # write
    "share_secret": ("session-acl",),  # write
    "list_secrets": ("session-acl",),
    "delete_secret": ("session-acl",),  # write

    # --- apps/canopy_sessions/secrets_api.py  (bearer + chat key issued to the claiming runner)
    "list_for_key": ("runner",),  # X-Canopy-Chat-Key
    "value_for_key": ("runner",),  # X-Canopy-Chat-Key; plaintext

    # --- apps/session_sharing/api.py  (Claude Code transcripts; per-person, not tenant-scoped)
    "upload_session": ("self",),
    "session_sharing_list_sessions": ("authenticated",),  # own + every link-visibility row, all tenants
    "create_arc": ("self",),  # only own sessions
    "list_arcs": ("authenticated",),  # own + every link-visibility arc
    "get_arc": ("self",),
    "patch_arc": ("self",),
    "delete_arc": ("self",),
    "rotate_arc_token": ("self",),
    "session_sharing_get_session": ("self",),
    "patch_session": ("self",),
    "delete_session": ("self",),
    "session_sharing_rotate_token": ("self",),
    "public_share_view": ("token-link",),  # auth=None; share token in path is the only key

    # --- apps/tokens/api.py  (the caller's own tokens, grants, GitHub connection)
    "list_tokens": ("self",),
    "create_token": ("self",),
    "revoke_token": ("self",),
    "tokens_list_connected_apps": ("self",),
    "tokens_disconnect_app": ("self",),
    "github_connection": ("self",),
    "github_disconnect": ("self",),
    "github_installations": ("self",),

    # --- apps/tokens/connected_apps_api.py  (embed_apps.require: read/test = integrations, change = own)
    "tokens_connected_apps_list_connected_apps": ("integrations",),
    "connect_app": ("own",),
    "update_connected_app": ("own",),
    "test_connected_app": ("integrations",),
    "tokens_connected_apps_disconnect_app": ("own",),

    # --- apps/contacts/contact_api.py  (/api/contact/: ContactAuth; contact_session_q)
    "contact_token": ("host",),  # auth=None; site-signed assertion (+ optional ID-JAG)
    "contact_me": ("contact",),
    "contact_app_view_resource": ("contact",),
    "contact_app_view_call": ("contact",),  # own session + own host grant
    "contact_app_view_read": ("contact",),
    "contact_app_view_context": ("contact",),
    "contact_app_view_message": ("contact",),
    "contact_ws_ticket": ("contact",),  # a one-time socket ticket standing for the caller's own contact token
    # --- apps/contacts/contact_hcp_api.py  (/api/contact/hcp/: a contact's own HCP, trusted sites only)
    # Every route but `proof` also spends a single-use frame proof — the contact's act in
    # canopy's own frame, which the site holding the token cannot produce.
    "contact_hcp_proof": ("contact",),  # canopy's frame cookie + Sec-Fetch-Site same-origin
    "contact_hcp_state": ("contact",),
    "contact_hcp_opt_in": ("contact",),
    "contact_hcp_grant": ("contact",),
    "contact_hcp_session_memory": ("contact",),
    "contact_hcp_policy": ("contact",),
    "contact_hcp_revoke": ("contact",),
    "contact_hcp_export": ("contact",),
    "contact_hcp_delete_entry": ("contact",),
    "start_session": ("contact",),
    "tokens_contact_list_sessions": ("contact",),
    "tokens_contact_get_session": ("contact",),
    "contact_archive_session": ("contact",),
    "tokens_contact_send": ("contact",),
    "messages": ("contact",),
    "attach": ("contact",),
    "detach": ("contact",),
    "tokens_contact_declare_page_state": ("contact",),
    "tokens_contact_declare_page_actions": ("contact",),
    "tokens_contact_resolve_page_action": ("contact",),
    "stop": ("contact",),
    "my_unclaimable": ("contact",),
    "my_turn": ("contact",),
    "my_turn_transcript": ("contact",),

    # --- apps/tokens/embed_api.py
    "list_embeddable_agents": ("member",),  # delegated token required; app allowlist ∩ caller's workspaces
    "embed_self": ("authenticated",),
    "embed_self_token": ("self",),
    "embed_session_memory": ("self",),  # read-only: the caller's OWN session with an agent this app offers; writes stay person-only on /api/people/me/
    "embed_ws_ticket": ("self",),  # a one-time socket ticket standing for the caller's own delegated token  # mints a 15-min DelegatedToken for the caller
    # --- apps/agent_runs/documents.py  (run docs; mounted with agent_runs)
    "list_run_docs": ("member",),
    "get_run_doc": ("member",),
    "create_run_doc": ("agent.work",),
    "put_run_doc_state": ("agent.work",),
    # --- apps/reviews/api.py
    "list_reviews": ("member",),
    "create_review": ("content.write",),  # creation_workspace + CONTENT_WRITE
    "get_review": ("token-link",),  # auth=None; link review readable TOKENLESS by anyone, else member
    "get_review_destination": ("token-link", "member"),  # auth=None; ?t= review token (board token handed on) or a member; may create the review's board
    "submit_review": ("content.write",),  # auth=None but handler requires editor + CSRF
    "suggest_review": ("token-link",),  # auth=None; ?t= share token on a link review; never resolves the gate
    "delete_review": ("content.write",),
    # --- apps/runs/api.py  (mounted /api/ddd)
    "list_narratives": ("member",),
    "get_narrative": ("member",),
    "runs_get_run": ("member",),
    "get_run_release": ("token-link",),  # auth=None; member or ?t= on the primary artifact
    "set_narrative_visibility": ("content.write",),  # scoped by editor slugs
    "delete_run": ("content.write",),
    "delete_version": ("content.write",),
    "delete_narrative": ("content.write",),
    "move_narrative": ("content.write",),  # editor on source AND destination
    "upload_narrative_source": ("content.write",),  # editor of the pinned version's workspace
    "list_narrative_sources": ("member",),
    "get_narrative_source_content": ("member",),
    # --- apps/shareouts/api.py
    "list_shareouts": ("member",),
    "create_shareouts": ("content.write",),  # replaces only the caller's own rows
    "clear_shareouts": ("content.write", "own"),  # editor clears own rows; owner clears all
    # --- apps/storyboards/api.py
    "list_storyboards": ("member",),
    "create_storyboard": ("content.write",),
    "get_storyboard": ("token-link",),  # auth=None; member or ?t=
    "patch_storyboard": ("content.write",),
    "storyboards_rotate_token": ("content.write",),
    "open_storyboard_round": ("content.write",),
    "close_storyboard_round": ("content.write",),
    "delete_storyboard_round_note": ("content.write",),
    "get_storyboard_round": ("member",),
    "consume_storyboard_round": ("content.write",),
    "edit_storyboard_decision": ("token-link", "member"),  # inside an open round every edit is staged (members too); outside one a member applies immediately and a link holder is refused (409)
    "ensure_token": ("content.write",),
    "leave_feedback": ("token-link", "member"),  # ?t= with comment/suggest grant; ANY member bypasses the grant
    "list_notes": ("member",),  # members only; token holder 404s
    "get_board_narrative": ("token-link",),  # auth=None; member or ?t=
    # --- apps/walkthroughs/api.py  (writes: uploader while still editor, or workspace owner)
    "upload_walkthrough": ("content.write",),  # creation_workspace + CONTENT_WRITE
    "list_walkthroughs": ("member",),
    "get_walkthrough": ("token-link",),  # auth=None; readable_by = member or ?t=
    "patch_walkthrough": ("content.write", "own"),  # uploader (editor) or workspace owner
    "rotate_walkthrough_token": ("content.write", "own"),  # uploader (editor) or workspace owner
    "delete_walkthrough": ("content.write", "own"),  # uploader (editor) or workspace owner
    # --- apps/issues/api.py
    "upsert_issue": ("content.write",),
    "list_issues": ("member",),
    "get_issue": ("member",),
    "delete_issue": ("content.write",),
    # --- apps/feedback/api.py
    "ingest_feedback": ("content.write",),  # creation_workspace only — no role check
    "list_feedback": ("member",),
    "resolve_feedback": ("content.write",),  # scoped by CONTENT_WRITE slugs; 403 if merely visible
    # --- apps/contacts/api.py
    "list_contacts": ("member",),
    "get_contact": ("member",),
    "patch_contact": ("content.write",),
    # --- apps/contacts/people_api.py  (fleet brain v1, canopy#804; apps/contacts/people.py)
    # Every per-person route is ALSO gated on `people.known_in` — a person the
    # workspace does not deal with is 404, so ids and addresses confirm nothing.
    "people_me": ("self",),  # only ever the caller's own Person
    # The person's agent-memory switch: their own Person only, and refused to an agent's
    # login, a caller token and a system account — nobody may flip it for them.
    "set_my_agent_memory": ("self",),
    # One of the caller's OWN sessions (`created_by`), as that person only — same refusals.
    "get_my_session_memory": ("self",),
    "set_my_session_memory": ("self",),
    # The person granting their session's agent (HCP 4.1.6) — only the person, never an agent.
    "grant_my_session_agent": ("self",),
    # The person's own memory collections, and which one an agent they allowed uses
    # (HCP 4.1.5.1) — only the person; a widening also needs their explicit confirm.
    "list_my_memory_collections": ("self",),
    "create_my_memory_collection": ("self",),
    "update_my_memory_collection": ("self",),
    "delete_my_memory_collection": ("self",),
    # Trying a search over your own memory (scores only, records nothing) — only
    # the person, and only inside the memory rollout.
    "preview_my_memory_search": ("self",),
    # Every search agents ran over your memory, as it ran — only the person.
    "list_my_memory_searches": ("self",),
    # Letting an agent see your memory page, read-only (apps/contacts/viewers.py):
    # choosing who is the person's own act; the agent's own login then reads it.
    "list_my_viewers": ("self",),
    "add_my_viewer": ("self",),
    "remove_my_viewer": ("self",),
    "list_shared_pages": ("authenticated",),  # empty unless the caller is an agent's own login
    "get_memory_page": ("page-viewer",),
    "get_memory_page_audit": ("page-viewer",),
    "get_memory_page_searches": ("page-viewer",),
    "set_my_grant_collection": ("self",),
    # --- apps/contacts/hcp_api.py  (HCP v1 over the people brain; apps/contacts/hcp/)
    # `self` = the person's own instance; `hcp-grant` = an agent, for the person
    # who started its turn, under that client's grant. A missing entry is 403.
    "hcp_configuration": ("anonymous",),  # the discovery document; names no person
    "hcp_searchPreferences": ("self", "hcp-grant"),
    "hcp_addPreference": ("self", "hcp-grant"),
    "hcp_createEntry": ("self", "hcp-grant"),
    "hcp_getPreference": ("self", "hcp-grant"),
    "hcp_updatePreference": ("self", "hcp-grant"),  # a category change: the person only
    "hcp_deletePreference": ("self", "hcp-grant"),  # hardDelete: the person only
    "hcp_listAudit": ("self",),
    "hcp_listGrants": ("self", "hcp-grant"),  # an agent sees only the grant it reads under
    "hcp_revokeGrant": ("self",),
    "hcp_export": ("self",),
    # --- apps/contacts/hcp_clients_api.py  (the HCP service's client registry)
    "hcp_listClients": ("superuser",),
    "hcp_registerClient": ("superuser",),  # puts its name before every person it asks
    "hcp_updateClient": ("superuser",),
    "lookup_person": ("member",),  # 404 unless known in one of the caller's workspaces
    "get_person": ("member",),  # read logged as PersonAccess(via=api)
    "list_person_projects": ("member",),  # same gate as get_person (known_in); read logged
    "people_coverage": ("member", "agent-admin"),  # counts only; a non-member agent admin sees just their agents
    "add_person_fact": ("member",),
    "retract_person_fact": ("self", "members.manage"),  # the subject, the asserter, or a workspace admin
    "list_person_conversations": ("agent-admin",),  # or the agent's OWN login; an admin gets only turns turn_access already shows them
    # --- apps/api/api.py
    "_auth_smoke": ("authenticated",),  # internal smoke route
    # --- apps/common/api.py
    "health": ("anonymous",),
    "me": ("authenticated",),
    "get_presence_preference": ("self",),
    "set_presence_preference": ("self",),
    "get_runner_app_preference": ("self",),
    "set_runner_app_preference": ("self",),
    "get_session_workspace_preference": ("self",),
    "set_session_workspace_preference": ("self",),  # target needs content.write (filing.set_default)
    # --- apps/events/api.py
    "record_events": ("events.write",),  # in the pinned or default workspace
    "list_mcp_calls": ("logs.read", "self"),  # the workspace's calls for admins; your own always
    "list_events": ("logs.read",),  # non-admin gets no rows, not 403
    "list_setting_changes": ("logs.read", "self"),  # the workspace's changes for admins; your own always
    # --- apps/inbound/api.py
    "gmail_push": ("host",),  # auth=None; Pub/Sub push verified by verify_push (OIDC/audience), 404 otherwise
    "get_push_config": ("member",),
    "set_push_config": ("integrations",),
    "list_mailboxes": ("member",),
    "create_mailbox": ("integrations",),
    "update_mailbox": ("integrations",),
    "delete_mailbox": ("integrations",),
    "runner_mailboxes": ("member",),  # every workspace the caller is in
    "report_watch": ("events.write",),  # mailbox's agent's workspace membership only
    # --- apps/mail/api.py
    "list_mail_domains": ("member",),
    "add_mail_domain": ("integrations",),
    "refresh_mail_domain": ("member",),  # re-reads DNS/SES; may restart a FAILED SES verification
    "update_mail_domain": ("integrations",),
    "remove_mail_domain": ("integrations",),
    "save_mail_route": ("integrations",),
    "delete_mail_route": ("integrations",),
    "share_mail_domain": ("superuser",),
    "unshare_mail_domain": ("superuser",),
    "resolve_mail_address": ("authenticated",),  # 404 unless the caller is a member of the address's workspace
    "list_mail_threads": ("member",),
    "get_mail_thread": ("member",),
    "reply_mail_thread": ("agent.work",),
    "send_mail": ("agent.work",),
    "set_mail_thread_state": ("agent.work",),
    "get_agent_mailbox": ("member",),
    "set_agent_mailbox": ("agent-admin",),  # the route itself also needs integrations (domains.ensure_agent_route)
    "ses_inbound": ("host",),  # auth=None; SNS signature + pinned cert host + pinned topic, 404 otherwise
    "ses_events": ("host",),  # auth=None; SNS signature + topic ARN verified, 404 otherwise
    # --- apps/push/api.py
    "vapid_public_key": ("authenticated",),
    "subscribe": ("self",),
    "unsubscribe": ("self",),
    "get_preferences": ("self",),
    "set_preferences": ("self",),
    # --- apps/slack/api.py
    "get_config": ("member",),
    "set_config_token": ("own",),
    "clear_config_token": ("own",),
    "set_history": ("integrations",),
    "sync": ("integrations",),
    "declare_agent": ("own",),
    "set_channel_ingest": ("own",),
    "allow_channel_ingest": ("integrations",),
    "disallow_channel_ingest": ("integrations",),  # also revokes the reads the entry allowed
    # --- apps/slack/ingest_api.py (the permission itself is given in Slack, on the
    # approval post: a channel member who holds integrations, or the channel's creator)
    "create_channel_ingest": ("agent-login", "agent-admin"),
    "list_channel_ingests": ("agent-login", "agent-admin", "integrations"),
    "get_channel_ingest": ("agent-login", "agent-admin", "integrations"),
    "get_channel_ingest_transcript": ("agent-login",),  # nobody else, not even the owner
    "mark_channel_ingest_read": ("agent-login",),  # the agent's own read position
    "revoke_channel_ingest": ("agent-login", "agent-admin", "integrations", "self"),  # self = its approver
    # --- apps/beta_requests/api.py
    "submit_beta_request": ("anonymous",),  # the public site's request-access form; grants nothing
    "list_beta_requests": ("beta-reviewer",),
    "get_beta_request": ("beta-reviewer",),
    "invite_beta_request": ("beta-reviewer", "members.manage"),  # reviewer, AND may invite at that role there
    "decline_beta_request": ("beta-reviewer",),
    # --- apps/system/api.py
    "overview": ("authenticated",),  # canopy plugin catalog, not tenant data
    "public_stats": ("anonymous",),
    "detail": ("authenticated",),
    # --- apps/timeline/api.py
    "list_timeline": ("member",),
    # --- apps/workspaces/api.py
    "create_workspace": ("authenticated", "own"),  # can_create_workspace; OWN on `parent` when nesting
    "list_workspaces": ("authenticated",),  # the caller's own memberships
    "get_workspace": ("member",),
    "set_workspace_parent": ("own",),  # own both ends
    "list_requestable_workspaces": ("authenticated",),  # capability list: access_request_domains match
    "request_workspace_access": ("authenticated",),  # access_request_domains match, same 404 otherwise; joins nothing unless auto_approve_role
    "list_access_requests": ("members.manage",),
    "get_access_request": ("members.manage",),
    "approve_access_request": ("members.manage",),  # + may_manage_member on the granted role
    "deny_access_request": ("members.manage",),
    "set_access_settings": ("own",),
    "delete_workspace": ("own",),
    "list_members": ("member",),
    "remove_member": ("members.manage",),  # + may_manage_member
    "set_member_role": ("members.manage",),  # + may_manage_member
    "create_invite": ("members.manage",),
    # System accounts (apps/workspaces/system_accounts.py): listed to members
    # like the member list; managed like an editor is (members.manage +
    # may_manage_member at the account's role).
    "list_system_accounts": ("member",),
    "get_system_account": ("member",),
    "create_system_account": ("members.manage",),  # + may_manage_member
    "update_system_account": ("members.manage",),  # + may_manage_member
    "delete_system_account": ("members.manage",),  # + may_manage_member
    "add_system_sender": ("members.manage",),  # + may_manage_member
    "remove_system_sender": ("members.manage",),  # + may_manage_member
    "list_invites": ("member",),  # tokens only to members.manage
    "revoke_invite": ("members.manage",),
    "reissue_invite": ("members.manage",),
    "preview_invite": ("signed-link",),  # auth=None; the invite token is the capability
    "accept_invite": ("authenticated", "signed-link"),
    "get_shared_vault": ("own",),
    "set_shared_vault": ("own",),
    "get_runner_order": ("member",),
    "get_retention": ("member",),  # read-only: everyone may know how long their chats are kept
    "retention_preview": ("retention.manage",),
    "create_retention_rule": ("retention.manage",),
    "update_retention_rule": ("retention.manage",),
    "delete_retention_rule": ("retention.manage",),
    "list_admission_rules": ("member",),  # read-only: anyone may know which turns are refused
    "create_admission_rule": ("admission.manage",),
    "update_admission_rule": ("admission.manage",),
    "delete_admission_rule": ("admission.manage",),
    "set_admission_settings": ("admission.manage",),
    "set_runner_order": ("runners.route",),  # admin+; routes agents in every division below too; each runner's owner must be a member (422 otherwise)
    # --- apps/capabilities/api.py (cloud capabilities: declare, grant, lease)
    "get_agent_capabilities": ("member", "agent-login"),
    "put_agent_capabilities": ("agent-admin", "agent-login"),  # the agent's deploy publishes
    "list_capability_grants": ("member",),
    "set_capability_grant": ("integrations",),
    "delete_capability_grant": ("integrations",),
    # Leasing spends money: editor+ in the agent's workspace, or the agent itself.
    "acquire_capability_lease": ("agent.work", "agent-login"),
    "get_capability_lease": ("agent.work", "agent-login"),  # also the heartbeat
    "release_capability_lease": ("agent.work", "agent-login"),
    "exec_capability_lease": ("agent.work", "agent-login"),
    "get_capability_exec": ("agent.work", "agent-login"),
    "create_capability_uploads": ("agent.work", "agent-login"),
    "list_capability_artifacts": ("agent.work", "agent-login"),
    "list_capability_leases": ("member", "agent-login"),  # a read, scoped to the caller's workspaces
    "capability_spend": ("member", "agent-login"),
    "force_release_capability_lease": ("integrations", "agent-admin"),
    "refresh_agent_capability": ("agent-admin", "agent-login"),
    "terminate_capability_orphans": ("integrations", "agent-admin"),
    # pool slot data: read masked by members; secret values leave only via the lease read
    "list_capability_slot_data": ("member", "agent-login"),
    "set_capability_slot_data": ("integrations", "agent-login"),
    "delete_capability_slot_data": ("integrations", "agent-login"),
    # --- apps/passkeys/api.py  (the caller's OWN passkeys; ceremonies need a browser session)
    "list_passkeys": ("self",),
    "begin_passkey_registration": ("self",),  # a SECOND passkey also needs a fresh step-up
    "finish_passkey_registration": ("self",),
    "delete_passkey": ("self",),  # + a fresh step-up
    "passkey_step_up_status": ("self",),
    "begin_passkey_step_up": ("self",),
    "finish_passkey_step_up": ("self",),
    # --- apps/passkeys/api_vault.py  (the caller's OWN zero-knowledge vault key)
    "get_vault_keyset": ("self",),
    "put_vault_keyset": ("self",),  # browser session + passkey step-up only
    "runner_topology": ("logs.read",),  # root + each descendant where the caller holds it
    "agent_topology": ("logs.read",),  # same; grant/revoke flags mirror _may_manage_admins
    "workspace_health": ("logs.read",),  # same tree read; can_fix mirrors Agent.is_admin
    "workspace_analytics": ("logs.read",),  # same tree read
    "workspace_network": ("logs.read",),  # same tree read; thread titles only where the session ACL lets the viewer open them
}

#: Writes a VIEWER can make, each a decision rather than a default. A write
#: whose gate includes plain "member" must be here, with the reason a viewer may
#: do it — or the route must be gated and its entry above changed.
VIEWER_MAY_MUTATE: dict[str, str] = {
    "act_on_task": "approve/decline/reply answer what is already on the board (interaction tier); dispatch/done require agent.work — except that the person whose words an idea task carries may mark it done (shipped): it is their idea",
    "check_agent_github": "re-probes the stored token against GitHub and records the result; grants and changes nothing",
    "delete_agent_github": "withdraws only the caller's OWN GitHub delegation",
    "check_agent_salesforce": "re-probes the lender's credential against Salesforce and records the result; grants and changes nothing",
    "delete_agent_salesforce": "withdraws only the caller's OWN Salesforce delegation",
    "pair_runner": "pairing grants nothing by itself: a box serves only workspaces it is enrolled in (or below one) AND where its owner holds agent.work (runner_tenant_slugs); pairing enrolls it in the pairing workspace only",
    "resolve_session": "runner protocol; the runner gate (owner) is the real check, membership only scopes the agent",
    "record_session": "runner protocol; the runner gate (owner) is the real check, membership only scopes the agent",
    "preview_cron": "POST but read-only: computes next fire times, writes nothing",
    "open_local_turn": "records the caller's OWN session on their own private runner; access.decide must give them the whole agent (403 otherwise), so a viewer is refused in the route",
    "create_session": "starting a chat with an agent is the interaction tier a viewer holds",
    "create_idea": "any member may put their OWN want on a project, in their own words, as themself — a suggested kind=idea task carrying only their quote, with no on_approve; it names nobody else, and recording someone else's words is create_tasks (agent.work)",
    "put_contributor_summary": "an agent's login is a member, and that is who writes a contributor page — only while the person lets agents learn about them and has granted that agent; otherwise only the person themself",
    "add_person_fact": "an agent's login is a member, and that is who writes facts (contract canopy#804); a fact is append-only, attributed, visible to its subject and retractable by them",
    "refresh_mail_domain": "re-checks DNS and SES verification and caches the answer; changes no configuration — restarting a verification SES gave up on re-puts the same Easy DKIM origin and MAIL FROM attributes, only once the records check out, at most every 10 min, and is audited",
    "edit_storyboard_decision": "a member's ruling on a demo-set default is the reader-tier act the decision log exists for; it is attributed and undoable (owner decision 2026-10-15)",
    "back_project": "any member says where they stand on a project, as themself, in one line — a signal that starts nothing (decision 5, 2026-10-16); deciding is the decision card's, gated to projects.decide or the project's owner",
    "unback_project": "withdraws only the caller's OWN backing",
    "back_task": "any member says where they stand on a check-in's proposed task, as themself, in one line — a signal that starts nothing; starting it is the task's approve, gated to the project's owner, the check-in's lead or projects.decide",
    "unback_task": "withdraws only the caller's OWN backing",
    "create_project_checkin": "only the project's OWNER passes as a member (checkins.may_write); everyone else needs integrations, and putting a new channel on an agent's read list always does",
    "update_project_checkin": "only the project's OWNER passes as a member (checkins.may_write); a new channel needs integrations",
    "delete_project_checkin": "only the project's OWNER passes as a member (checkins.may_write); it removes only an allow-list entry the check-in itself added",
    "run_project_checkin_now": "only the project's OWNER passes as a member (checkins.may_write); it announces in Slack (or queues the turn) and never reads a channel",
    "record_engagement_view": "records only that the caller opened a page they can already read, once a day; changes nothing",
    "leave_feedback": "leaving a note on a board you can read is reader-tier, the same act a token holder with a comment grant may do",
}


# ---- the settings audit (`apps/events/audit.py`) ----
#
# Every write route is one of two things, and says which here. A route in
# SETTINGS changes how something is configured — who may do what, where work
# runs, which credential or integration is used, what an agent or workspace is
# set to do — and every call to it, from any entry point (web UI, CLI, script,
# MCP tool), writes a `SettingChange` in the same transaction as the change.
# Every other write is in NOT_SETTINGS with the kind of write it is instead.
# `tests/test_every_route_declares_its_gate.py` fails a write route that is in
# neither, or both: a new route cannot ship until someone decides whether it is
# a setting. When unsure, it is a setting — an extra row costs nothing, a
# missing one is the gap this exists to close.

SETTINGS: frozenset[str] = frozenset({
    # mail: workspace email domains and their routes
    "add_mail_domain", "refresh_mail_domain", "update_mail_domain", "remove_mail_domain", "save_mail_route", "delete_mail_route",
    "share_mail_domain", "unshare_mail_domain", "set_agent_mailbox",
    # agents: identity, ownership, access, integrations, credentials
    "upsert_agent", "move_agent", "delete_agent", "transfer_owner", "link_canopy_user", "mint_agent_canopy_token",
    "give_agent_canopy_login", "grant_admin", "revoke_admin",
    "revoke_agent_license", "approve_agent_license",
    "publish_interface", "unpublish_interface",
    "set_credential_source", "set_repository", "set_agent_credentials", "delete_agent_credential", "set_agent_vault",
    "set_device_binding", "approve_runner_device_key", "revoke_runner_device_key",
    "set_pat_rotation", "rotate_pat_now",
    "enable_agent_zk", "set_agent_zk_values", "add_agent_zk_wraps", "rotate_agent_zk",
    "fulfil_agent_secret_request", "add_agent_zk_endorsements",
    "set_agent_github", "delete_agent_github", "set_agent_salesforce", "delete_agent_salesforce",
    "set_slack_enabled", "set_turn_mode", "set_sender_trust", "delete_sender_trust",
    # agents: routing (handlers report before/after — apps/harness/routing_audit.py)
    "replace_agent_runners", "replace_agent_runner_rules",
    "set_agent_runner_rule", "delete_agent_runner_rule",
    "set_agent_actor_route", "delete_agent_actor_route",
    # agents: schedules
    "create_schedule", "update_schedule", "delete_schedule",
    # runners
    "pair_runner", "retire_runner", "unretire_runner", "pause_runner", "unpause_runner",
    "grant_runner_admin", "revoke_runner_admin",
    "set_runner_credential", "swap_runner_logins", "set_runner_engine", "set_runner_flags", "set_runner_private",
    "add_runner_login", "remove_runner_login", "order_runner_logins",
    "enroll_runner_workspace", "unenroll_runner_workspace",
    "hold_fleet", "release_fleet_hold",
    # workspaces: membership, access, structure
    "create_workspace", "delete_workspace", "set_workspace_parent", "set_access_settings",
    "accept_invite", "create_invite", "reissue_invite", "revoke_invite",
    "approve_access_request", "deny_access_request", "invite_beta_request",
    "remove_member", "set_member_role",
    "create_system_account", "update_system_account", "delete_system_account",
    "add_system_sender", "remove_system_sender",
    # workspaces: policy and routing
    "create_admission_rule", "update_admission_rule", "delete_admission_rule",
    "set_admission_settings",
    "create_retention_rule", "update_retention_rule", "delete_retention_rule",
    "set_runner_order", "set_shared_vault",
    # cloud capabilities: what an agent declares, and who may lease it
    "put_agent_capabilities", "set_capability_grant", "delete_capability_grant",
    "set_capability_slot_data", "delete_capability_slot_data",  # a slot's credentials
    # workspaces: integrations
    "connect_app", "update_connected_app", "tokens_connected_apps_disconnect_app",
    "set_push_config", "create_mailbox", "update_mailbox", "delete_mailbox",
    "set_config_token", "clear_config_token", "declare_agent", "set_history",
    "set_channel_ingest", "revoke_channel_ingest",  # revoking withdraws an agent's access to a channel
    "allow_channel_ingest", "disallow_channel_ingest",  # an admin's standing grant of one channel to one agent
    # project check-ins: a channel allow-listed for an agent + a schedule
    "create_project_checkin", "update_project_checkin", "delete_project_checkin",
    "hcp_registerClient", "hcp_updateClient",
    # sharing and visibility
    "set_narrative_visibility", "storyboards_rotate_token", "ensure_token",
    "rotate_walkthrough_token", "rotate_arc_token", "session_sharing_rotate_token",
    "add_participant", "remove_participant",
    # a person's own settings: credentials, grants, preferences, memory
    "create_token", "revoke_token", "tokens_disconnect_app", "github_disconnect",
    "set_presence_preference", "set_session_workspace_preference", "set_preferences",
    "set_runner_app_preference",
    "register_private_runner", "retire_private_runner",  # your own machines (local use)
    "set_my_agent_memory", "set_my_grant_collection",
    "add_my_viewer", "remove_my_viewer",  # who may see your memory page
    "finish_passkey_registration", "delete_passkey",  # the second factor a step-up asks for
    "put_vault_keyset",  # the person's zero-knowledge vault key
    "create_my_memory_collection", "update_my_memory_collection", "delete_my_memory_collection",
    "grant_my_session_agent", "set_my_session_memory", "hcp_revokeGrant",
    "contact_hcp_policy", "contact_hcp_revoke", "contact_hcp_grant", "contact_hcp_opt_in",
    "contact_hcp_session_memory",
})

#: What a write that is NOT a setting is instead. Closed, like VOCABULARY.
WRITE_KINDS: dict[str, str] = {
    "content": "work product: docs, tasks, projects, reviews, narratives, runs, storyboards, issues",
    "conversation": "a chat or session: messages, attachments, lifecycle, page state",
    "turn": "dispatching or reporting a turn of work",
    "runner-protocol": "a runner daemon reporting in (heartbeats, claims, streams, mint steps)",
    "action": "a one-shot operation that changes no configuration (check, test, preview, run now, sync)",
    "person-data": "facts and records about a person, kept and retractable under their own rules",
    "telemetry": "events and reports recorded for later reading",
    "request": "asking for something someone else decides (access, beta)",
    "ephemeral-credential": "a short-lived ticket minted for one connection",
}

NOT_SETTINGS: dict[str, str] = {
    # a person setting things aside on their OWN list: nothing is decided or sent
    "hide_attention": "action", "unhide_attention": "action",
    # a box registering its own key: lands `pending`, trusted only once a person
    # approves it (approve_runner_device_key, a setting)
    "register_runner_device_key": "runner-protocol",
    # a box trading its approved device key's proof for an hour-long runner token
    "create_runner_session": "ephemeral-credential",
    # a stranded box (revoked token) asking for a key: lands `pending`, like the above
    "request_runner_device_key": "runner-protocol",
    # a new cloud box's pairing invitation: asking for it, and its creator saying
    # yes (passkey-gated). Recorded as activity (runner.invited / .approved); the
    # runner it lets pair is a new row, not a changed setting.
    "create_runner_invitation": "request", "approve_runner_invitation": "action",
    # an agent asking a person for a declared secret: carries no value; the
    # answer (fulfil_agent_secret_request) is the setting
    "request_agent_secret": "request",
    # the invited box pairing: only what its creator approved (runner.paired activity)
    "pair_runner_by_role": "runner-protocol",
    # a laptop box asking to be paired, a person saying yes or no (passkey-gated;
    # recorded as runner.pairing.approved), and the box claiming what was approved
    # (runner.paired) — a new runner row, like pair_runner_by_role, not a changed setting
    "create_runner_pairing": "request", "approve_runner_pairing": "action",
    "decline_runner_pairing": "action", "claim_runner_pairing": "runner-protocol",
    "reply_mail_thread": "conversation", "send_mail": "conversation", "set_mail_thread_state": "conversation",
    # content
    "create_run_doc": "content", "put_run_doc_state": "content", "mark_task_action_applied": "content",
    "create_project": "content", "patch_project": "content",
    "create_workspace_project": "content", "patch_workspace_project": "content",
    "kb_upload_target": "content", "kb_upload_originals": "content", "publish_kb": "content",
    "put_contributor_summary": "content", "delete_contributor_summary": "content",
    "create_run": "content", "fork_run": "content", "record_gate": "content", "record_verdict": "content",
    "create_sync": "content", "delete_sync": "content", "replace_skills": "content",
    "sync_skill_history": "content", "create_tasks": "content", "patch_task": "content",
    "create_idea": "content",  # an idea is a task with kind=idea
    "record_engagement_view": "telemetry",  # the engagement view beacon
    "propose_project": "content", "back_project": "content", "unback_project": "content",
    "back_project_as_agent": "content",
    "propose_task": "content", "back_task": "content", "unback_task": "content", "back_task_as_agent": "content",
    "check_scaffold_repo": "action",  # POST only to keep a token out of the URL; writes nothing
    "preview_my_memory_search": "action",  # POST for the question in a body; writes nothing
    "create_channel_ingest": "request",  # asks the channel; the approval click is the grant
    "mark_channel_ingest_read": "action",  # moves the agent's own read position; grants nothing
    "scaffold_agent": "content",  # pushes code into the caller's own repo; registering is upsert_agent (a setting)
    "act_on_task": "content", "patch_contact": "content",
    "delete_narrative": "content", "move_narrative": "content", "delete_version": "content",
    "upload_narrative_source": "content",
    "delete_run": "content", "upsert_issue": "content", "delete_issue": "content",
    "create_review": "content", "delete_review": "content", "submit_review": "content",
    "suggest_review": "content", "create_arc": "content", "patch_arc": "content", "delete_arc": "content",
    "upload_session": "content", "patch_session": "content", "delete_session": "content",
    "create_shareouts": "content", "clear_shareouts": "content",
    "create_storyboard": "content", "patch_storyboard": "content", "leave_feedback": "content",
    "edit_storyboard_decision": "content", "open_storyboard_round": "content",
    "close_storyboard_round": "content", "delete_storyboard_round_note": "content", "consume_storyboard_round": "content",
    "upload_walkthrough": "content", "patch_walkthrough": "content", "delete_walkthrough": "content",
    "ingest_feedback": "content", "resolve_feedback": "content",
    "open_thread": "content", "close_thread": "content",
    "resolve_failure_investigation": "content",
    # conversation
    "create_session": "conversation", "delete_attachment": "conversation", "reset_sessions": "conversation",
    "detach_session_from_slack": "conversation", "start_new_slack_thread": "conversation",
    "share_session_to_slack": "conversation", "purge_session_noise": "conversation",
    "approve_transfer_request": "conversation",
    "cancel_transfer_request": "conversation", "decline_transfer_request": "conversation",
    "answer_menu": "conversation", "app_view_call": "conversation", "app_view_context": "conversation",
    "app_view_message": "conversation", "app_view_read": "conversation", "archive_session": "conversation",
    "attach_session": "conversation", "upload_attachment": "conversation", "request_backfill": "conversation",
    "close_session": "conversation", "detach_session": "conversation", "move_session": "conversation",
    "set_session_notify": "conversation", "set_session_title": "conversation", "canopy_sessions_declare_page_actions": "conversation",
    "invoke_page_action": "conversation", "canopy_sessions_resolve_page_action": "conversation",
    "canopy_sessions_declare_page_state": "conversation", "place": "conversation",
    "reset_session": "conversation", "declare_run_input": "conversation", "share_secret": "conversation",
    "delete_secret": "conversation", "canopy_sessions_send": "conversation",
    "stop_session_turn": "conversation", "transfer": "conversation", "fork_session": "conversation", "unarchive_session": "conversation",
    "start_session": "conversation", "contact_app_view_call": "conversation",
    "contact_app_view_context": "conversation", "contact_app_view_message": "conversation",
    "contact_app_view_read": "conversation", "contact_archive_session": "conversation",
    "attach": "conversation", "detach": "conversation", "tokens_contact_declare_page_actions": "conversation",
    "tokens_contact_resolve_page_action": "conversation", "tokens_contact_declare_page_state": "conversation",
    "tokens_contact_send": "conversation", "stop": "conversation",
    # turn
    "create_turn": "turn", "enqueue_turn": "turn", "cancel_turn": "turn", "append_turn_events": "turn",
    "finish_turn": "turn", "start_turn": "turn", "append_turn_transcript": "turn",
    "open_local_turn": "turn", "renew_local_turn": "turn",
    "fire_schedule_route": "turn",
    "finish_checkin_run": "turn",  # the project agent's turn reporting its check-in run done
    # runner-protocol
    "post_bootstrap_report": "runner-protocol", "update_runner_capabilities": "runner-protocol",
    "claim_admission_notices": "runner-protocol", "report_admission_notice": "runner-protocol",
    "claim_turn": "runner-protocol", "runner_heartbeat": "runner-protocol",
    "post_menu_answer_result": "runner-protocol", "post_runner_mint_result": "runner-protocol",
    "post_session_menu": "runner-protocol",
    "post_close_results": "runner-protocol",
    "post_runner_mint_url": "runner-protocol", "report_runner_logins": "runner-protocol", "record_session": "runner-protocol",
    "resolve_session": "runner-protocol", "post_session_backfill": "runner-protocol",
    "post_session_stream": "runner-protocol", "report_sessions": "runner-protocol",
    "post_runner_usage": "runner-protocol", "request_usage_scan": "action", "send_up_missing_sessions": "action",
    "post_sign_in_request": "runner-protocol", "turn_github_token": "runner-protocol",
    "turn_agent_token": "runner-protocol",
    "post_sign_in_request_result": "runner-protocol",
    "report_drill": "runner-protocol",
    # action
    "check_agent_github": "action", "check_agent_salesforce": "action", "preview_cron": "action",
    "run_schedule_now": "action", "run_project_checkin_now": "action", "start_runner_drill": "action", "start_runner_mint": "action",
    "post_runner_mint_code": "action", "refresh_runner": "action", "test_connected_app": "action",
    "sync": "action", "hcp_searchPreferences": "action",
    # cloud capabilities: a lease is a machine for one use, not configuration
    "acquire_capability_lease": "action", "release_capability_lease": "action",
    "exec_capability_lease": "action", "create_capability_uploads": "ephemeral-credential",
    "force_release_capability_lease": "action", "terminate_capability_orphans": "action",
    "refresh_agent_capability": "action",  # re-reads the stack; nobody chooses a value
    # person-data
    "add_person_fact": "person-data", "retract_person_fact": "person-data",
    "hcp_createEntry": "person-data", "hcp_addPreference": "person-data",
    "hcp_updatePreference": "person-data", "hcp_deletePreference": "person-data",
    "contact_hcp_delete_entry": "person-data", "contact_hcp_proof": "person-data",
    "subscribe": "person-data", "unsubscribe": "person-data",
    # telemetry
    "record_events": "telemetry", "report_watch": "telemetry", "gmail_push": "telemetry", "ses_inbound": "telemetry",
    "ses_events": "telemetry",
    # request
    "submit_beta_request": "request", "decline_beta_request": "request",
    "request_workspace_access": "request",
    # ephemeral-credential
    "contact_token": "ephemeral-credential", "embed_self_token": "ephemeral-credential",
    # passkeys: a ceremony's challenge, and the five-minute step-up it earns
    "begin_passkey_registration": "ephemeral-credential", "begin_passkey_step_up": "ephemeral-credential",
    "finish_passkey_step_up": "ephemeral-credential",
    "reveal_agent_credential": "action",  # reads one value; changes no configuration (recorded as an event)
    "embed_ws_ticket": "ephemeral-credential", "contact_ws_ticket": "ephemeral-credential",
}


#: The MCP tools that are NOT routes (`apps/mcp/tools/`, registered with `@mcp.tool`):
#: name -> (gate labels from VOCABULARY, "read" | a WRITE_KINDS kind). A route is
#: declared above; a tool that bypasses the routes declares itself here, so the
#: question "who may call this" is asked for it too.
HAND_TOOL_GATES: dict[str, tuple[tuple[str, ...], str]] = {
    # The caller envelope of a turn: a member of the turn's tenant, or the
    # turn's own caller token. Same gate as `get_turn_caller_context`.
    "who_is_asking": (("member", "caller-token"), "read"),
    # The Connected site the visitor is on: caller tokens only, and only with
    # the visitor's live host grant (`apps/host_grants/host_gateway.py`).
    "site_tools": (("caller-token",), "read"),
    "site_call": (("caller-token",), "action"),
    # The caller's own open pages (or, for a caller token, its own conversation's).
    "current_page": (("self", "caller-token"), "read"),
    # /ide close review: carries an MCP Apps View, which has no REST shape.
    "propose_session_closes": (("session-acl",), "read"),
    # The View's Close button: a person's host grant only, then the session ACL.
    "close_proposed_sessions": (("host-grant", "session-acl"), "conversation"),
}
