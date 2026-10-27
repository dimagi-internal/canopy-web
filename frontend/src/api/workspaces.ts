// Workspaces API — the tenant list backing the header switcher + provider,
// plus the members/invites admin surface (Task 4) and the pre-auth invite
// preview + accept calls the /invite/:token page drives (Task 5).
import { apiV2 } from './client.v2'
import { ApiError, ensureOk } from './problem'
import type { components } from './generated'

export type WorkspaceOut = components['schemas']['WorkspaceOut']
export type RequestableWorkspaceOut = components['schemas']['RequestableWorkspaceOut']
export type AccessRequestOut = components['schemas']['AccessRequestOut']
export type ApprovableRole = components['schemas']['AccessRequestApproveIn']['role']
export type AutoApproveRole = components['schemas']['AccessSettingsIn']['auto_approve_role']
export type MemberOut = components['schemas']['MemberOut']
export type MemberProfileOut = components['schemas']['MemberProfileOut']
export type InviteOut = components['schemas']['InviteOut']
export type InviteRole = components['schemas']['InviteCreateIn']['role']
export type MemberRole = components['schemas']['MemberRoleUpdateIn']['role']
export type MemberTreeOut = components['schemas']['MemberTreeOut']
export type MemberTreeWorkspaceOut = components['schemas']['MemberTreeWorkspaceOut']
export type MemberTreePersonOut = components['schemas']['MemberTreePersonOut']
export type MemberTreeCellOut = components['schemas']['MemberTreeCellOut']
export type InvitePreviewOut = components['schemas']['InvitePreviewOut']
export type RetentionOut = components['schemas']['RetentionOut']
export type RetentionRuleOut = components['schemas']['RetentionRuleOut']
export type RetentionRuleIn = components['schemas']['RetentionRuleIn']
export type RetentionPreviewOut = components['schemas']['RetentionPreviewOut']
export type AdmissionRulesOut = components['schemas']['AdmissionRulesOut']
export type AdmissionRuleOut = components['schemas']['AdmissionRuleOut']
export type AdmissionRuleIn = components['schemas']['AdmissionRuleIn']
export type AdmissionRulePatch = components['schemas']['AdmissionRulePatch']

// Every call below needs the HTTP status (404 for non-member, 403 for an
// invite-accept email mismatch, 410 for a dead invite) — not just a message —
// so callers can branch on it (see WorkspaceMembersPage's redirect-on-404 and
// InviteAcceptPage's mismatch state). Mirrors the `ApiError` shape already used
// by `api/sessions.ts` / `api/chat.ts`, just built on top of openapi-fetch's
// typed responses instead of a hand-rolled fetch.
// `WorkspaceApiError` is the shared `ApiError` (./problem) under its old name.
export { ApiError as WorkspaceApiError }

// None of these operations declares an error response in the OpenAPI schema
// (django-ninja's HttpError raises are exceptions, not declared response
// models), so openapi-fetch's *types* infer `error` as always-`undefined` —
// `if (res.error)` narrows the whole `res` union to `never` inside the branch
// (a real TS false-negative, not a runtime one: openapi-fetch's runtime always
// parses the body into `error` on a non-ok response, schema or no schema — see
// its `response.ok ? data : error` split). Branching on `res.response.ok`
// instead sidesteps the bad narrowing; `res.error`'s static type stays
// `undefined` but `problemMessage` takes `unknown`, so the real parsed body
// still reaches it at runtime.
export async function listMembers(slug: string): Promise<MemberOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/members/', {
    params: { path: { slug } },
  })
  ensureOk(res, 'Failed to load members')
  return res.data as unknown as MemberOut[]
}

/** One member as the caller may see them; `admin` is null below members.manage. */
export async function getMember(slug: string, userId: number): Promise<MemberProfileOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/members/{user_id}/profile/', {
    params: { path: { slug, user_id: userId } },
  })
  ensureOk(res, 'Failed to load member')
  return res.data as unknown as MemberProfileOut
}

/** This workspace and the ones below it the caller is in, with everyone's role in each. */
export async function getMemberTree(slug: string): Promise<MemberTreeOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/member-tree', {
    params: { path: { slug } },
  })
  ensureOk(res, 'Failed to load members across workspaces')
  return res.data as unknown as MemberTreeOut
}

/** Add someone already in this workspace's tree directly, at `role` — no invite.
 *  404 = not in the tree (as you see it); 409 = already a member (use setMemberRole). */
export async function addTreeMember(slug: string, userId: number, role: MemberRole): Promise<MemberOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/members/', {
    params: { path: { slug } },
    body: { user_id: userId, role },
  })
  ensureOk(res, 'Failed to add member')
  return res.data as unknown as MemberOut
}

export async function listWorkspaces(): Promise<WorkspaceOut[]> {
  const { data } = await apiV2.GET('/api/workspaces/')
  return (data as unknown as WorkspaceOut[]) ?? []
}

// --- access requests ("request an invitation") ------------------------------
// The way into a workspace besides an invite (docs/architecture/access.md):
// someone whose login email is at one of the workspace's
// `access_request_domains` asks; an admin or owner approves at a role, or
// denies. With the workspace's `auto_approve_role` set the request comes back
// already `approved` at that role.

// A capability list — only workspaces this caller may ask to join — so it is
// always safe to render as-is.
export async function listRequestableWorkspaces(): Promise<RequestableWorkspaceOut[]> {
  const { data } = await apiV2.GET('/api/workspaces/requestable')
  return Array.from((data as unknown as RequestableWorkspaceOut[]) ?? [])
}

// Idempotent while pending. 404 = no such workspace OR your domain is not on
// its list (indistinguishable by design); 409 = already a member.
export async function requestWorkspaceAccess(slug: string, note: string): Promise<AccessRequestOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/access-requests', {
    params: { path: { slug } },
    body: { note },
  })
  ensureOk(res, 'Could not send the request')
  return res.data as unknown as AccessRequestOut
}

export async function listAccessRequests(slug: string): Promise<AccessRequestOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/access-requests', {
    params: { path: { slug } },
  })
  ensureOk(res, 'Failed to load access requests')
  return Array.from(res.data as unknown as AccessRequestOut[])
}

export async function getAccessRequest(slug: string, requestId: number): Promise<AccessRequestOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/access-requests/{request_id}', {
    params: { path: { slug, request_id: requestId } },
  })
  ensureOk(res, 'Failed to load the request')
  return res.data as unknown as AccessRequestOut
}

export async function approveAccessRequest(
  slug: string, requestId: number, role: ApprovableRole,
): Promise<AccessRequestOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/access-requests/{request_id}/approve', {
    params: { path: { slug, request_id: requestId } },
    body: { role },
  })
  ensureOk(res, 'Failed to approve')
  return res.data as unknown as AccessRequestOut
}

export async function denyAccessRequest(slug: string, requestId: number, reason: string): Promise<AccessRequestOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/access-requests/{request_id}/deny', {
    params: { path: { slug, request_id: requestId } },
    body: { reason },
  })
  ensureOk(res, 'Failed to deny')
  return res.data as unknown as AccessRequestOut
}

/** Owner-only. "" turns auto-approval off. */
export async function setAccessSettings(slug: string, autoApproveRole: AutoApproveRole): Promise<WorkspaceOut> {
  const res = await apiV2.PUT('/api/workspaces/{slug}/access-settings', {
    params: { path: { slug } },
    body: { auto_approve_role: autoApproveRole },
  })
  ensureOk(res, 'Failed to save')
  return res.data as unknown as WorkspaceOut
}

export async function removeMember(slug: string, userId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/members/{user_id}/', {
    params: { path: { slug, user_id: userId } },
  })
  ensureOk(res, 'Failed to remove member')
}

export async function setMemberRole(slug: string, userId: number, role: MemberRole): Promise<MemberOut> {
  const res = await apiV2.PATCH('/api/workspaces/{slug}/members/{user_id}/', {
    params: { path: { slug, user_id: userId } },
    body: { role },
  })
  ensureOk(res, 'Failed to change role')
  return res.data as unknown as MemberOut
}

export async function listInvites(slug: string): Promise<InviteOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/invites/', {
    params: { path: { slug } },
  })
  ensureOk(res, 'Failed to load invites')
  return res.data as unknown as InviteOut[]
}

export async function createInvite(slug: string, email: string, role: InviteRole): Promise<InviteOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/invites/', {
    params: { path: { slug } },
    body: { email, role },
  })
  ensureOk(res, 'Failed to create invite')
  return res.data as InviteOut
}

export async function revokeInvite(slug: string, inviteId: number): Promise<void> {
  const res = await apiV2.POST('/api/workspaces/{slug}/invites/{invite_id}/revoke', {
    params: { path: { slug, invite_id: inviteId } },
  })
  ensureOk(res, 'Failed to revoke invite')
}

// Rotates the token, resets the expiry and emails the new link: the returned
// invite carries it (and `email_status`), and the old link stops working. Also
// how an expired invite is revived.
export async function reissueInvite(slug: string, inviteId: number): Promise<InviteOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/invites/{invite_id}/reissue', {
    params: { path: { slug, invite_id: inviteId } },
  })
  ensureOk(res, 'Failed to resend the invite')
  return res.data as InviteOut
}

export async function previewInvite(token: string): Promise<InvitePreviewOut> {
  const res = await apiV2.GET('/api/workspaces/invites/{token}/preview', {
    params: { path: { token } },
  })
  ensureOk(res, 'Failed to load invite')
  return res.data as InvitePreviewOut
}

export async function acceptInvite(token: string): Promise<WorkspaceOut> {
  const res = await apiV2.POST('/api/workspaces/invites/{token}/accept', {
    params: { path: { token } },
  })
  ensureOk(res, 'Failed to accept invite')
  return res.data as unknown as WorkspaceOut
}

// Used by FirstRunPage. Returns a result object rather than throwing: both
// failure modes here (403 not-eligible, 409 slug-taken, 422 bad charset) are
// expected user-facing outcomes the caller displays inline, not exceptional
// conditions. This endpoint declares no error response in the OpenAPI schema
// (see the `WorkspaceApiError` comment above), so `res.error` narrows the
// whole result to `never` under `if (res.error)` — branching on
// `res.response.ok` instead sidesteps that false-negative.
export async function createWorkspace(
  slug: string,
  displayName: string,
): Promise<{ slug: string } | { error: string }> {
  const res = await apiV2.POST('/api/workspaces/', {
    body: { slug, display_name: displayName },
  })
  if (!res.response.ok) {
    // 409 = slug taken, 403 = not eligible (F1), 422 = bad slug charset.
    // The server's problem+json `detail` is the only message worth showing:
    // the slug rules are enforced by Workspace.SLUG_PATTERN server-side and
    // restating them here would be a second copy free to drift.
    const detail = (res.error as { detail?: string } | undefined)?.detail
    return { error: detail || 'Could not create the workspace.' }
  }
  return { slug: (res.data as unknown as WorkspaceOut).slug }
}


// --- the tenant's shared 1Password vault -------------------------------------
// The TENANT half of the two-level credential model: this vault holds what every
// agent in the workspace shares (the gog OAuth clients, the fleet GitHub token),
// and canopy-web holds a service-account key scoped to it. An AGENT's own vault
// and key live on the agent (see AgentVaultSection) — a key that read both would
// undo the split that bounds a compromise to one agent.
//
// Owner-only on the server; the key is write-only and never comes back.
export type RunnerTopologyOut = components['schemas']['RunnerTopologyOut']
export type TopologyRunnerOut = components['schemas']['TopologyRunnerOut']
export type TopologyAgentOut = components['schemas']['TopologyAgentOut']
export type TopologyRouteOut = components['schemas']['TopologyRouteOut']

export async function getRunnerTopology(slug: string): Promise<RunnerTopologyOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/runner-topology', {
    params: { path: { slug } },
  })
  ensureOk(res, 'Failed to load the runner topology')
  return res.data as unknown as RunnerTopologyOut
}

export type PrivateRunnerUseOut = components['schemas']['PrivateRunnerUseOut']

// People's own machines their local turns on this workspace's agents ran on
// (canopy-web#1704). Listed beside the runners; a private runner never claims work.
export async function listWorkspacePrivateRunners(slug: string): Promise<PrivateRunnerUseOut[]> {
  const res = await apiV2.GET('/api/private-runners/used', { params: { query: { workspace: slug } } })
  ensureOk(res, 'Failed to load private runners')
  return res.data as unknown as PrivateRunnerUseOut[]
}

export type AgentTopologyOut = components['schemas']['AgentTopologyOut']
export type AgentTopologyAgentOut = components['schemas']['AgentTopologyAgentOut']
export type AgentEdgeOut = components['schemas']['AgentEdgeOut']

export async function getAgentTopology(slug: string): Promise<AgentTopologyOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/agent-topology', {
    params: { path: { slug } },
  })
  ensureOk(res, 'Failed to load the agent topology')
  return res.data as unknown as AgentTopologyOut
}

export type RunnerOrderRowOut = components['schemas']['RunnerOrderRowOut']

/** The workspace's default runner order: repo turns, and every agent here (or in
 *  a workspace below without its own) that has no order of its own. */
export async function getRunnerOrder(slug: string): Promise<RunnerOrderRowOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/runner-order', { params: { path: { slug } } })
  ensureOk(res, 'Failed to load the runner order')
  return Array.from(res.data as unknown as RunnerOrderRowOut[])
}

/** Wholesale replace (index = rank). Workspace owners only. */
export async function setRunnerOrder(
  slug: string,
  rows: readonly { runnerId: string; enabled: boolean }[],
): Promise<RunnerOrderRowOut[]> {
  const res = await apiV2.PUT('/api/workspaces/{slug}/runner-order', {
    params: { path: { slug } },
    body: { runners: rows.map((r) => ({ runner_id: r.runnerId, enabled: r.enabled })) },
  })
  ensureOk(res, 'Failed to save the runner order')
  return Array.from(res.data as unknown as RunnerOrderRowOut[])
}

// --- Content retention (Settings → Retention) ---

export async function getRetention(slug: string): Promise<RetentionOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/retention', { params: { path: { slug } } })
  ensureOk(res, 'Failed to load retention rules')
  return res.data as unknown as RetentionOut
}

export async function previewRetention(slug: string): Promise<RetentionPreviewOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/retention/preview', { params: { path: { slug } } })
  ensureOk(res, 'Failed to preview retention')
  return res.data as unknown as RetentionPreviewOut
}

export async function saveRetentionRule(
  slug: string, body: RetentionRuleIn, ruleId?: number,
): Promise<RetentionRuleOut> {
  const res = ruleId === undefined
    ? await apiV2.POST('/api/workspaces/{slug}/retention/rules', { params: { path: { slug } }, body })
    : await apiV2.PUT('/api/workspaces/{slug}/retention/rules/{rule_id}', {
      params: { path: { slug, rule_id: ruleId } }, body,
    })
  ensureOk(res, 'Failed to save the rule')
  return res.data as unknown as RetentionRuleOut
}

export async function deleteRetentionRule(slug: string, ruleId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/retention/rules/{rule_id}', {
    params: { path: { slug, rule_id: ruleId } },
  })
  ensureOk(res, 'Failed to delete the rule')
}

// --- Admission rules (Settings → Admission rules) ---

export async function getAdmissionRules(slug: string): Promise<AdmissionRulesOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/admission-rules', { params: { path: { slug } } })
  ensureOk(res, 'Failed to load admission rules')
  return res.data as unknown as AdmissionRulesOut
}

export async function createAdmissionRule(slug: string, body: AdmissionRuleIn): Promise<AdmissionRuleOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/admission-rules', { params: { path: { slug } }, body })
  ensureOk(res, 'Failed to add the rule')
  return res.data as unknown as AdmissionRuleOut
}

export async function updateAdmissionRule(
  slug: string, ruleId: number, body: AdmissionRulePatch,
): Promise<AdmissionRuleOut> {
  const res = await apiV2.PATCH('/api/workspaces/{slug}/admission-rules/{rule_id}', {
    params: { path: { slug, rule_id: ruleId } }, body,
  })
  ensureOk(res, 'Failed to save the rule')
  return res.data as unknown as AdmissionRuleOut
}

export async function setAdmissionSenderNotices(slug: string, enabled: boolean): Promise<boolean> {
  const res = await apiV2.PUT('/api/workspaces/{slug}/admission-settings', {
    params: { path: { slug } }, body: { sender_notices_enabled: enabled },
  })
  ensureOk(res, 'Failed to switch automatic replies')
  return Boolean(res.data?.sender_notices_enabled)
}

export async function deleteAdmissionRule(slug: string, ruleId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/admission-rules/{rule_id}', {
    params: { path: { slug, rule_id: ruleId } },
  })
  ensureOk(res, 'Failed to delete the rule')
}

export type WorkspaceHealthOut = components['schemas']['WorkspaceHealthOut']
export type AgentHealthOut = components['schemas']['AgentHealthOut']
export type WorkspaceHealthNodeOut = components['schemas']['WorkspaceHealthNodeOut']

export type AgentSetupHealthOut = components['schemas']['AgentSetupHealthOut']

/** The tree's health. `setup: false` leaves each agent's checklist and vault
 *  state out (null), for `getWorkspaceHealthSetup` to fill in after. */
export async function getWorkspaceHealth(slug: string, opts: { setup?: boolean } = {}): Promise<WorkspaceHealthOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/health', {
    params: { path: { slug }, query: opts.setup === false ? { setup: false } : {} },
  })
  ensureOk(res, 'Failed to load workspace health')
  return res.data as unknown as WorkspaceHealthOut
}

/** The checklist and vault state of some of the tree's agents (at most 25). */
export async function getWorkspaceHealthSetup(slug: string, agents: readonly string[]): Promise<AgentSetupHealthOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/health/setup', {
    params: { path: { slug }, query: { agents: agents.join(',') } },
  })
  ensureOk(res, 'Failed to load setup checklists')
  return (res.data as unknown as { agents: AgentSetupHealthOut[] }).agents
}

export type WorkspaceNetworkOut = components['schemas']['WorkspaceNetworkOut']
export type NetworkNodeOut = components['schemas']['NetworkNodeOut']
export type NetworkLinkOut = components['schemas']['NetworkLinkOut']

export async function getWorkspaceNetwork(
  slug: string, opts: { days: number; tree: boolean },
): Promise<WorkspaceNetworkOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/network', {
    params: { path: { slug }, query: { days: opts.days, tree: opts.tree } },
  })
  ensureOk(res, 'Failed to load the network')
  return res.data as unknown as WorkspaceNetworkOut
}

export type WorkspaceAnalyticsOut = components['schemas']['WorkspaceAnalyticsOut']
export type AnalyticsDayOut = components['schemas']['AnalyticsDayOut']

export async function getWorkspaceAnalytics(
  slug: string, opts: { days: number; tree: boolean },
): Promise<WorkspaceAnalyticsOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/analytics', {
    params: { path: { slug }, query: { days: opts.days, tree: opts.tree } },
  })
  ensureOk(res, 'Failed to load analytics')
  return res.data as unknown as WorkspaceAnalyticsOut
}

export type MyInviteOut = components['schemas']['MyInviteOut']

/** Invites addressed to the signed-in person that they have not accepted yet. */
export async function listMyInvites(): Promise<MyInviteOut[]> {
  const res = await apiV2.GET('/api/workspaces/invites/mine')
  ensureOk(res, 'Failed to load your invites')
  return res.data as unknown as MyInviteOut[]
}
