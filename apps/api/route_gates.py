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
  ``own``.
* ``agent-admin`` — ``Agent.is_admin`` (its owner, a workspace owner, an
  ``AgentAdmin``).
* ``agent-owner`` — the agent's owner or a workspace owner (transfer, admin
  grants).
* ``session-acl`` — ``apps/canopy_sessions/access.py`` (read/write/share per chat).
* ``turn-content`` — ``apps/harness/turn_access.can_read_turn_content``.
* ``runner`` — the runner protocol: the runner's owner, or the box
  that claimed the turn.
* ``runner-admin`` — ``can_administer_runner``.
* ``runner-holds-agent`` — ``services.caller_runs_agent`` /
  ``runner_may_hold_agent``.
* ``contact`` — the ``/api/contact/`` surface, for contact principals.
* ``signed-link`` — a signed token in the URL is the authority (drill report,
  invite token, OAuth state).
* ``host`` — a connected-site / machine protocol endpoint (assertion,
  jwt-bearer, Pub/Sub push).
"""
from __future__ import annotations

VOCABULARY: frozenset[str] = frozenset({
    "anonymous", "token-link", "authenticated", "self", "member",
    "content.write", "agent.work", "session.drive", "events.write", "logs.read",
    "members.manage", "integrations", "runners.route", "retention.manage", "own",
    "agent-admin", "agent-owner", "session-acl", "turn-content",
    "runner", "runner-admin", "runner-holds-agent",
    "contact", "signed-link", "host",
})

GATES: dict[str, tuple[str, ...]] = {
    # --- apps/agents/api.py
    "list_agents": ("member",),
    "upsert_agent": ("agent.work", "agent-admin"),  # editor in target ws; a MOVE also needs agent admin
    "get_agent": ("member",),
    "link_canopy_user": ("agent-admin",),
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
    "list_tasks": ("member",),
    "create_tasks": ("agent.work",),  # or the agent's own login
    "get_task": ("member",),
    "patch_task": ("agent.work",),
    "act_on_task": ("member", "agent.work"),  # approve/decline/reply = member; dispatch/done = editor
    "list_task_actions": ("member",),
    "mark_task_action_applied": ("agent.work",),  # or the agent's own login
    # --- apps/agents/fleet_api.py  (fleet-wide lists, scoped to visible agents)
    "list_fleet_tasks": ("member",),
    "list_fleet_projects": ("member",),
    "set_agent_credentials": ("agent-admin",),
    "agent_credential_status": ("member",),
    "resolve_agent_credentials": ("runner-holds-agent",),  # bearer only
    "get_agent_vault": ("member",),
    "set_agent_vault": ("agent-admin",),
    "get_agent_github": ("member",),
    "set_agent_github": ("agent-owner",),  # strictly the agent's own owner (delegations.set_github)
    "check_agent_github": ("member",),
    "delete_agent_github": ("member", "self"),  # removes only the caller's own delegation
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
    "get_runner_credential_status": ("runner-admin",),
    "get_runner_credential": ("runner",),  # plaintext to the owner's token
    "turn_github_token": ("runner", "runner-holds-agent"),  # turn must be claimed by this box
    "runner_github_readiness": ("runner",),
    "start_runner_mint": ("runner-admin",),
    "get_runner_mint": ("runner-admin",),
    "claim_runner_mint": ("runner",),
    "post_runner_mint_url": ("runner",),
    "post_runner_mint_code": ("runner-admin",),
    "post_runner_mint_result": ("runner",),
    "list_runner_admins": ("runner-admin",),
    "grant_runner_admin": ("runner",),  # owner only (not RunnerAdmins)
    "revoke_runner_admin": ("runner",),
    "set_runner_flags": ("runner-admin",),
    "set_runner_engine": ("runner-admin",),
    "list_runners": ("member",),  # _runner_read_q: the tenant's fleet
    "update_runner_capabilities": ("runner",),
    "retire_runner": ("runner",),
    "unretire_runner": ("runner",),
    "pause_runner": ("runner",),
    "unpause_runner": ("runner",),
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
    "list_menu_answers": ("runner",),
    "post_menu_answer_result": ("runner",),
    "post_session_backfill": ("runner",),  # session must be bound to this runner
    # --- apps/harness/api.py: turns
    "list_unclaimable_turns": ("member", "session-acl", "turn-content"),  # prompt blanked unless turn-content
    "enqueue_turn": ("agent.work",),  # agent and project turns alike
    "harness_list_turns": ("member", "session-acl", "turn-content"),  # session turns by chat ACL; redacted
    "harness_list_sessions": ("member", "session-acl"),
    # --- apps/huddles/api.py: a derived view over the same visible turns as harness_list_turns
    "list_huddles": ("member", "session-acl"),  # anchors via visible_turns_qs; summary carries no content
    "get_huddle": ("member", "session-acl", "turn-content"),  # prompt/reply/digest only with turn-content
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
    "archive_session": ("session-acl",),  # write
    "reset_session": ("session-acl",),  # write
    "unarchive_session": ("session-acl",),  # write
    "set_session_notify": ("session-acl",),  # write
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
    "answer_menu": ("session-acl",),  # write
    "close_session": ("session-acl",),  # write
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
    "contact_ws_ticket": ("contact",),  # a one-time socket ticket standing for the caller's own contact token
    "start_session": ("contact",),
    "tokens_contact_list_sessions": ("contact",),
    "tokens_contact_get_session": ("contact",),
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
    # --- apps/api/api.py
    "_auth_smoke": ("authenticated",),  # internal smoke route
    # --- apps/common/api.py
    "health": ("anonymous",),
    "me": ("authenticated",),
    "get_presence_preference": ("self",),
    "set_presence_preference": ("self",),
    # --- apps/events/api.py
    "record_events": ("events.write",),  # in the pinned or default workspace
    "list_mcp_calls": ("logs.read", "self"),  # the workspace's calls for admins; your own always
    "list_events": ("logs.read",),  # non-admin gets no rows, not 403
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
    # --- apps/beta_requests/api.py
    "submit_beta_request": ("anonymous",),  # the public site's request-access form; grants nothing
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
    "set_runner_order": ("runners.route",),  # admin+; routes agents in every division below too; each runner's owner must be a member (422 otherwise)
    "runner_topology": ("logs.read",),  # root + each descendant where the caller holds it
    "agent_topology": ("logs.read",),  # same; grant/revoke flags mirror _may_manage_admins
}

#: Writes a VIEWER can make, each a decision rather than a default. A write
#: whose gate includes plain "member" must be here, with the reason a viewer may
#: do it — or the route must be gated and its entry above changed.
VIEWER_MAY_MUTATE: dict[str, str] = {
    "act_on_task": "approve/decline/reply answer what is already on the board (interaction tier); dispatch/done require agent.work",
    "check_agent_github": "re-probes the stored token against GitHub and records the result; grants and changes nothing",
    "delete_agent_github": "withdraws only the caller's OWN GitHub delegation",
    "pair_runner": "pairing grants nothing by itself: a box serves only workspaces where its owner holds agent.work (runner_tenant_slugs)",
    "resolve_session": "runner protocol; the runner gate (owner) is the real check, membership only scopes the agent",
    "record_session": "runner protocol; the runner gate (owner) is the real check, membership only scopes the agent",
    "preview_cron": "POST but read-only: computes next fire times, writes nothing",
    "create_session": "starting a chat with an agent is the interaction tier a viewer holds",
    "leave_feedback": "leaving a note on a board you can read is reader-tier, the same act a token holder with a comment grant may do",
}
