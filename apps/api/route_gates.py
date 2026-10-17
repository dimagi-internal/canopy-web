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
* ``contact`` — the ``/api/contact/`` surface, for contact principals.
* ``device-proof`` — no session or token: a proof from an ACTIVE runner device key
  (DPoP, or an EC2 instance role's signed STS call) bound to this request is the
  authority (``apps/harness/api_runner_sessions.py``).
* ``signed-link`` — a signed token in the URL is the authority (drill report,
  invite token, OAuth state).
* ``beta-reviewer`` — ``apps/beta_requests/services.may_review``: a superuser or
  the ``CANOPY_BETA_REQUESTS_TO`` address (a beta request names no workspace).
* ``superuser`` — ``User.is_superuser``: fleet-wide switches that span every tenant
  (the fleet hold).
* ``fleet-holder`` — an address in ``CANOPY_FLEET_HOLDERS`` (Ada): may SET the fleet
  hold, never release it (``apps/harness/api.py::_may_hold_fleet``).
* ``host`` — a connected-site / machine protocol endpoint (assertion,
  jwt-bearer, Pub/Sub push).
* ``hcp-grant`` — the Human Context Protocol subject rule
  (``apps/contacts/hcp_api.py::_principal``): an agent reaches ONLY the person
  who started the turn it names, and only under that client's (agent, channel,
  host) grant, which the person can revoke. An OAuth client of the HCP service
  (``apps/contacts/hcp_oauth.py``) is held the same way: its ``hcpat_`` access
  token reaches ONLY the person whose grant it was issued under, within that
  grant's scopes and sources, and opens ``/api/hcp/v1/`` and nothing else.
"""
from __future__ import annotations

VOCABULARY: frozenset[str] = frozenset({
    "anonymous", "token-link", "authenticated", "self", "member",
    "content.write", "agent.work", "session.drive", "events.write", "logs.read",
    "members.manage", "integrations", "runners.route", "retention.manage",
    "admission.manage", "projects.decide", "own",
    "agent-admin", "agent-owner", "agent-login", "session-acl", "turn-content",
    "runner", "runner-admin", "runner-holds-agent",
    "contact", "signed-link", "host", "beta-reviewer", "superuser", "fleet-holder",
    "hcp-grant", "device-proof",
})

GATES: dict[str, tuple[str, ...]] = {
    # --- apps/agents/api.py
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
    "transfer_owner": ("agent-owner",),  # leaving it ownerless needs OWN
    "get_interface": ("member",),
    "publish_interface": ("agent-admin",),
    "unpublish_interface": ("agent-admin",),
    "list_admins": ("member",),
    "agent_access": ("member",),
    "grant_admin": ("agent-owner",),
    "revoke_admin": ("agent-owner",),
    "delete_agent": ("agent.work",),
    "set_runner_preference": ("agent.work",),
    "set_turn_mode": ("agent.work",),
    "set_credential_source": ("agent-admin",),
    "set_device_binding": ("agent-admin",),
    "agent_credential_reads": ("agent-admin",),  # the read log; never a value
    "agent_device_keys": ("agent-admin",),  # can_manage per key = runner-admin
    "agent_credential_alerts": ("agent-admin",),  # read-anomaly alerts; never a value
    "get_pat_rotation": ("agent-admin",),
    "set_pat_rotation": ("agent-admin",),  # turning it on also asks mint_agent_canopy_token's guard
    "rotate_pat_now": ("agent-admin",),  # browser: + passkey step-up; token allowed
    # zero-knowledge vault (apps/agents/zk.py): browser writes need a passkey step-up
    "get_agent_zk_state": ("agent-admin",),  # wraps are public; ?values= ciphertext, read-logged
    "enable_agent_zk": ("agent-admin",),  # browser: + step-up; token allowed
    "set_agent_zk_values": ("agent-admin",),  # browser: + step-up; token allowed
    "add_agent_zk_wraps": ("agent-admin",),  # browser: + step-up; token allowed
    "rotate_agent_zk": ("agent-admin",),  # browser: + step-up; token allowed
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
    "list_fleet_projects": ("member",),
    "set_agent_credentials": ("agent-admin",),
    "agent_credential_status": ("member",),
    "agent_credential_access": ("member",),  # no values; reports the resolve gate for the caller
    "resolve_agent_credentials": ("runner-holds-agent", "agent-admin"),  # bearer only
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
    # --- apps/agents/oauth_api.py
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
    # --- apps/harness/api.py: runner registry (the runner protocol speaks AS the owner)
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
    "register_runner_device_key": ("runner-admin",),  # lands pending; approval is separate
    "create_runner_session": ("device-proof",),  # anonymous door; an ACTIVE key's proof is the authority
    "list_runner_device_keys": ("runner-admin",),
    "approve_runner_device_key": ("runner-admin",),  # browser session only (+ passkey step-up)
    "revoke_runner_device_key": ("runner-admin",),
    "turn_github_token": ("runner", "runner-holds-agent"),  # turn must be claimed by this box
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
    "list_runner_admins": ("runner-admin",),
    "grant_runner_admin": ("runner",),  # owner only (not RunnerAdmins)
    "revoke_runner_admin": ("runner",),
    "list_runner_workspaces": ("runner-admin",),  # an admin sees only workspaces it is a member of
    "enroll_runner_workspace": ("runner-admin",),  # + the runner's OWNER holds agent.work there (422)
    "unenroll_runner_workspace": ("runner-admin",),
    "set_runner_flags": ("runner-admin",),
    "set_runner_engine": ("runner-admin",),
    "list_runners": ("member",),  # _runner_read_q: the tenant's fleet
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
    "claim_admission_notices": ("runner",),  # + runner_may_hold_agent per notice
    "report_admission_notice": ("runner",),  # only a notice this runner holds
    "post_session_backfill": ("runner",),  # session must be bound to this runner
    # --- apps/harness/api.py: turns
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
    # --- apps/huddles/groups_api.py: work groups. None of these can start a channel read
    "list_huddle_groups": ("member",),
    "create_huddle_group": ("integrations",),  # + integrations where the leader agent lives
    "get_huddle_group": ("member",),
    "update_huddle_group": ("integrations",),
    "delete_huddle_group": ("integrations",),
    "list_huddle_group_runs": ("member",),
    "get_huddle_group_run": ("member", "agent-login"),  # material included: members, and the leader's login
    "finish_huddle_group_run": ("agent-login",),  # the leader agent's own login only
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
    "finish_turn": ("runner", "agent.work", "session-acl"),
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
    "reset_sessions": ("session-acl",),  # readable rows filtered to can_write
    "canopy_sessions_get_session": ("session-acl",),
    "list_messages": ("session-acl",),
    "search_sessions": ("session-acl",),  # readable_sessions, via the list's _filtered_sessions
    "list_human_inputs": ("session-acl",),  # _session_or_404 / _session_by_key_or_404, as get_session
    "archive_session": ("session-acl",),  # write
    "reset_session": ("session-acl",),  # write
    "unarchive_session": ("session-acl",),  # write
    "set_session_notify": ("session-acl",),  # write
    # filing.move: read gate, then the session's own person (creator / runner owner),
    # then content.write in the TARGET workspace; an agent's session is a 422.
    "move_session": ("session-acl", "content.write"),
    "list_transfer_requests": ("self", "runner-admin"),  # ones you asked for, or for boxes you administer
    "approve_transfer_request": ("runner-admin",),  # the target box's admins, re-checked at decision
    "decline_transfer_request": ("runner-admin",),
    "cancel_transfer_request": ("self",),  # only whoever asked
    "list_participants": ("session-acl",),
    "add_participant": ("session-acl",),  # can_share (chat owner)
    "remove_participant": ("session-acl",),  # can_share, or self-removal
    "canopy_sessions_send": ("session-acl",),  # write
    "place": ("session-acl",),  # write
    "transfer": ("session-acl",),  # write
    "export_session": ("session-acl", "self"),  # visible AND you started it
    "answer_menu": ("session-acl",),  # write
    "close_session": ("session-acl",),  # write
    "detach_session_from_slack": ("session-acl", "agent-login"),  # write; or the agent's own live turn
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
    # MCP Apps View plumbing (apps/tokens/mcp_apps_views.py): a session reader may
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

    # --- apps/tokens/contact_api.py  (/api/contact/: ContactAuth; contact_session_q)
    "contact_token": ("host",),  # auth=None; site-signed assertion (+ optional ID-JAG)
    "contact_me": ("contact",),
    "contact_app_view_resource": ("contact",),
    "contact_app_view_call": ("contact",),  # own session + own host grant
    "contact_app_view_read": ("contact",),
    "contact_app_view_context": ("contact",),
    "contact_app_view_message": ("contact",),
    "contact_ws_ticket": ("contact",),  # a one-time socket ticket standing for the caller's own contact token
    # --- apps/tokens/contact_hcp_api.py  (/api/contact/hcp/: a contact's own HCP, trusted sites only)
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
    "set_my_grant_collection": ("self",),
    # --- apps/contacts/hcp_api.py  (HCP v1 over the people brain; apps/contacts/hcp.py)
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
    "refresh_mail_domain": ("member",),  # re-reads DNS/SES; changes no configuration
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
    "create_session": "starting a chat with an agent is the interaction tier a viewer holds",
    "create_idea": "any member may put their OWN want on a project, in their own words, as themself — a suggested kind=idea task carrying only their quote, with no on_approve; it names nobody else, and recording someone else's words is create_tasks (agent.work)",
    "put_contributor_summary": "an agent's login is a member, and that is who writes a contributor page — only while the person lets agents learn about them and has granted that agent; otherwise only the person themself",
    "add_person_fact": "an agent's login is a member, and that is who writes facts (contract canopy#804); a fact is append-only, attributed, visible to its subject and retractable by them",
    "refresh_mail_domain": "re-checks DNS and SES verification and caches the answer; changes no configuration",
    "edit_storyboard_decision": "a member's ruling on a demo-set default is the reader-tier act the decision log exists for; it is attributed and undoable (owner decision 2026-10-15)",
    "back_project": "any member says where they stand on a project, as themself, in one line — a signal that starts nothing (decision 5, 2026-10-16); deciding is the decision card's, gated to projects.decide or the project's owner",
    "unback_project": "withdraws only the caller's OWN backing",
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
    "add_mail_domain", "update_mail_domain", "remove_mail_domain", "save_mail_route", "delete_mail_route",
    "share_mail_domain", "unshare_mail_domain",
    # agents: identity, ownership, access, integrations, credentials
    "upsert_agent", "move_agent", "delete_agent", "transfer_owner", "link_canopy_user", "mint_agent_canopy_token",
    "grant_admin", "revoke_admin",
    "publish_interface", "unpublish_interface",
    "set_credential_source", "set_agent_credentials", "delete_agent_credential", "set_agent_vault",
    "set_device_binding", "approve_runner_device_key", "revoke_runner_device_key",
    "set_pat_rotation", "rotate_pat_now",
    "enable_agent_zk", "set_agent_zk_values", "add_agent_zk_wraps", "rotate_agent_zk",
    "set_agent_github", "delete_agent_github", "set_agent_salesforce", "delete_agent_salesforce",
    "set_slack_enabled", "set_turn_mode", "set_sender_trust", "delete_sender_trust",
    # agents: routing (handlers report before/after — apps/harness/routing_audit.py)
    "set_runner_preference", "replace_agent_runners", "replace_agent_runner_rules",
    "set_agent_runner_rule", "delete_agent_runner_rule",
    "set_agent_actor_route", "delete_agent_actor_route",
    # agents: schedules
    "create_schedule", "update_schedule", "delete_schedule",
    # runners
    "pair_runner", "retire_runner", "unretire_runner", "pause_runner", "unpause_runner",
    "grant_runner_admin", "revoke_runner_admin",
    "set_runner_credential", "swap_runner_logins", "set_runner_engine", "set_runner_flags",
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
    # work groups: a channel allow-listed for an agent + a schedule
    "create_huddle_group", "update_huddle_group", "delete_huddle_group",
    "hcp_registerClient", "hcp_updateClient",
    # sharing and visibility
    "set_narrative_visibility", "storyboards_rotate_token", "ensure_token",
    "rotate_walkthrough_token", "rotate_arc_token", "session_sharing_rotate_token",
    "add_participant", "remove_participant",
    # a person's own settings: credentials, grants, preferences, memory
    "create_token", "revoke_token", "tokens_disconnect_app", "github_disconnect",
    "set_presence_preference", "set_session_workspace_preference", "set_preferences",
    "set_my_agent_memory", "set_my_grant_collection",
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
    # a box registering its own key: lands `pending`, trusted only once a person
    # approves it (approve_runner_device_key, a setting)
    "register_runner_device_key": "runner-protocol",
    # a box trading its approved device key's proof for an hour-long runner token
    "create_runner_session": "ephemeral-credential",
    "refresh_mail_domain": "action",
    "reply_mail_thread": "conversation", "send_mail": "conversation", "set_mail_thread_state": "conversation",
    # content
    "create_run_doc": "content", "put_run_doc_state": "content", "mark_task_action_applied": "content",
    "create_project": "content", "patch_project": "content",
    "put_contributor_summary": "content", "delete_contributor_summary": "content",
    "create_run": "content", "fork_run": "content", "record_gate": "content", "record_verdict": "content",
    "create_sync": "content", "delete_sync": "content", "replace_skills": "content",
    "sync_skill_history": "content", "create_tasks": "content", "patch_task": "content",
    "create_idea": "content",  # an idea is a task with kind=idea
    "record_engagement_view": "telemetry",  # the engagement view beacon
    "propose_project": "content", "back_project": "content", "unback_project": "content",
    "back_project_as_agent": "content",
    "check_scaffold_repo": "action",  # POST only to keep a token out of the URL; writes nothing
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
    "approve_transfer_request": "conversation",
    "cancel_transfer_request": "conversation", "decline_transfer_request": "conversation",
    "answer_menu": "conversation", "app_view_call": "conversation", "app_view_context": "conversation",
    "app_view_message": "conversation", "app_view_read": "conversation", "archive_session": "conversation",
    "attach_session": "conversation", "upload_attachment": "conversation", "request_backfill": "conversation",
    "close_session": "conversation", "detach_session": "conversation", "move_session": "conversation",
    "set_session_notify": "conversation", "canopy_sessions_declare_page_actions": "conversation",
    "invoke_page_action": "conversation", "canopy_sessions_resolve_page_action": "conversation",
    "canopy_sessions_declare_page_state": "conversation", "place": "conversation",
    "reset_session": "conversation", "declare_run_input": "conversation", "share_secret": "conversation",
    "delete_secret": "conversation", "canopy_sessions_send": "conversation",
    "stop_session_turn": "conversation", "transfer": "conversation", "unarchive_session": "conversation",
    "start_session": "conversation", "contact_app_view_call": "conversation",
    "contact_app_view_context": "conversation", "contact_app_view_message": "conversation",
    "contact_app_view_read": "conversation", "contact_archive_session": "conversation",
    "attach": "conversation", "detach": "conversation", "tokens_contact_declare_page_actions": "conversation",
    "tokens_contact_resolve_page_action": "conversation", "tokens_contact_declare_page_state": "conversation",
    "tokens_contact_send": "conversation", "stop": "conversation",
    # turn
    "create_turn": "turn", "enqueue_turn": "turn", "cancel_turn": "turn", "append_turn_events": "turn",
    "finish_turn": "turn", "start_turn": "turn", "append_turn_transcript": "turn",
    "fire_schedule_route": "turn",
    "finish_huddle_group_run": "turn",  # the leader's turn reporting its work group run done
    # runner-protocol
    "post_bootstrap_report": "runner-protocol", "update_runner_capabilities": "runner-protocol",
    "claim_admission_notices": "runner-protocol", "report_admission_notice": "runner-protocol",
    "claim_turn": "runner-protocol", "runner_heartbeat": "runner-protocol",
    "post_menu_answer_result": "runner-protocol", "post_runner_mint_result": "runner-protocol",
    "post_close_results": "runner-protocol",
    "post_runner_mint_url": "runner-protocol", "report_runner_logins": "runner-protocol", "record_session": "runner-protocol",
    "resolve_session": "runner-protocol", "post_session_backfill": "runner-protocol",
    "post_session_stream": "runner-protocol", "report_sessions": "runner-protocol",
    "post_sign_in_request": "runner-protocol", "turn_github_token": "runner-protocol",
    "report_drill": "runner-protocol",
    # action
    "check_agent_github": "action", "check_agent_salesforce": "action", "preview_cron": "action",
    "run_schedule_now": "action", "start_runner_drill": "action", "start_runner_mint": "action",
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
