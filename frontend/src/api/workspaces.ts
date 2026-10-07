// Workspaces API — the tenant list backing the header switcher + provider,
// plus the members/invites admin surface (Task 4) and the pre-auth invite
// preview + accept calls the /invite/:token page drives (Task 5).
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type WorkspaceOut = components['schemas']['WorkspaceOut']
export type RequestableWorkspaceOut = components['schemas']['RequestableWorkspaceOut']
export type AccessRequestOut = components['schemas']['AccessRequestOut']
export type ApprovableRole = components['schemas']['AccessRequestApproveIn']['role']
export type AutoApproveRole = components['schemas']['AccessSettingsIn']['auto_approve_role']
export type MemberOut = components['schemas']['MemberOut']
export type InviteOut = components['schemas']['InviteOut']
export type InviteRole = components['schemas']['InviteCreateIn']['role']
export type MemberRole = components['schemas']['MemberRoleUpdateIn']['role']
export type InvitePreviewOut = components['schemas']['InvitePreviewOut']
export type SharedVaultOut = components['schemas']['SharedVaultOut']
export type RetentionOut = components['schemas']['RetentionOut']
export type RetentionRuleOut = components['schemas']['RetentionRuleOut']
export type RetentionRuleIn = components['schemas']['RetentionRuleIn']
export type RetentionPreviewOut = components['schemas']['RetentionPreviewOut']

// Every call below needs the HTTP status (404 for non-member, 403 for an
// invite-accept email mismatch, 410 for a dead invite) — not just a message —
// so callers can branch on it (see WorkspaceMembersPage's redirect-on-404 and
// InviteAcceptPage's mismatch state). Mirrors the `ApiError` shape already used
// by `api/sessions.ts` / `api/chat.ts`, just built on top of openapi-fetch's
// typed responses instead of a hand-rolled fetch.
export class WorkspaceApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

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
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load members'))
  }
  return res.data as unknown as MemberOut[]
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
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Could not send the request'))
  }
  return res.data as unknown as AccessRequestOut
}

export async function listAccessRequests(slug: string): Promise<AccessRequestOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/access-requests', {
    params: { path: { slug } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load access requests'))
  }
  return Array.from(res.data as unknown as AccessRequestOut[])
}

export async function getAccessRequest(slug: string, requestId: number): Promise<AccessRequestOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/access-requests/{request_id}', {
    params: { path: { slug, request_id: requestId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load the request'))
  }
  return res.data as unknown as AccessRequestOut
}

export async function approveAccessRequest(
  slug: string, requestId: number, role: ApprovableRole,
): Promise<AccessRequestOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/access-requests/{request_id}/approve', {
    params: { path: { slug, request_id: requestId } },
    body: { role },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to approve'))
  }
  return res.data as unknown as AccessRequestOut
}

export async function denyAccessRequest(slug: string, requestId: number, reason: string): Promise<AccessRequestOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/access-requests/{request_id}/deny', {
    params: { path: { slug, request_id: requestId } },
    body: { reason },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to deny'))
  }
  return res.data as unknown as AccessRequestOut
}

/** Owner-only. "" turns auto-approval off. */
export async function setAccessSettings(slug: string, autoApproveRole: AutoApproveRole): Promise<WorkspaceOut> {
  const res = await apiV2.PUT('/api/workspaces/{slug}/access-settings', {
    params: { path: { slug } },
    body: { auto_approve_role: autoApproveRole },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to save'))
  }
  return res.data as unknown as WorkspaceOut
}

export async function removeMember(slug: string, userId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/members/{user_id}/', {
    params: { path: { slug, user_id: userId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to remove member'))
  }
}

export async function setMemberRole(slug: string, userId: number, role: MemberRole): Promise<MemberOut> {
  const res = await apiV2.PATCH('/api/workspaces/{slug}/members/{user_id}/', {
    params: { path: { slug, user_id: userId } },
    body: { role },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to change role'))
  }
  return res.data as unknown as MemberOut
}

export async function listInvites(slug: string): Promise<InviteOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/invites/', {
    params: { path: { slug } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load invites'))
  }
  return res.data as unknown as InviteOut[]
}

export async function createInvite(slug: string, email: string, role: InviteRole): Promise<InviteOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/invites/', {
    params: { path: { slug } },
    body: { email, role },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to create invite'))
  }
  return res.data as InviteOut
}

export async function revokeInvite(slug: string, inviteId: number): Promise<void> {
  const res = await apiV2.POST('/api/workspaces/{slug}/invites/{invite_id}/revoke', {
    params: { path: { slug, invite_id: inviteId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to revoke invite'))
  }
}

// Rotates the token, resets the expiry and emails the new link: the returned
// invite carries it (and `email_status`), and the old link stops working. Also
// how an expired invite is revived.
export async function reissueInvite(slug: string, inviteId: number): Promise<InviteOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/invites/{invite_id}/reissue', {
    params: { path: { slug, invite_id: inviteId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to resend the invite'))
  }
  return res.data as InviteOut
}

export async function previewInvite(token: string): Promise<InvitePreviewOut> {
  const res = await apiV2.GET('/api/workspaces/invites/{token}/preview', {
    params: { path: { token } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load invite'))
  }
  return res.data as InvitePreviewOut
}

export async function acceptInvite(token: string): Promise<WorkspaceOut> {
  const res = await apiV2.POST('/api/workspaces/invites/{token}/accept', {
    params: { path: { token } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to accept invite'))
  }
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
export async function getSharedVault(slug: string): Promise<SharedVaultOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/shared-vault', {
    params: { path: { slug } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(
      res.response.status, problemMessage(res.error, 'Failed to load the shared vault'))
  }
  return res.data as unknown as SharedVaultOut
}

export async function setSharedVault(
  slug: string, body: { vault?: string; service_key?: string },
): Promise<SharedVaultOut> {
  const res = await apiV2.PUT('/api/workspaces/{slug}/shared-vault', {
    params: { path: { slug } },
    body,
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(
      res.response.status, problemMessage(res.error, 'Failed to save the shared vault'))
  }
  return res.data as unknown as SharedVaultOut
}

export type RunnerTopologyOut = components['schemas']['RunnerTopologyOut']
export type TopologyRunnerOut = components['schemas']['TopologyRunnerOut']
export type TopologyAgentOut = components['schemas']['TopologyAgentOut']
export type TopologyRouteOut = components['schemas']['TopologyRouteOut']

export async function getRunnerTopology(slug: string): Promise<RunnerTopologyOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/runner-topology', {
    params: { path: { slug } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(
      res.response.status, problemMessage(res.error, 'Failed to load the runner topology'))
  }
  return res.data as unknown as RunnerTopologyOut
}

export type AgentTopologyOut = components['schemas']['AgentTopologyOut']
export type AgentTopologyAgentOut = components['schemas']['AgentTopologyAgentOut']
export type AgentEdgeOut = components['schemas']['AgentEdgeOut']

export async function getAgentTopology(slug: string): Promise<AgentTopologyOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/agent-topology', {
    params: { path: { slug } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(
      res.response.status, problemMessage(res.error, 'Failed to load the agent topology'))
  }
  return res.data as unknown as AgentTopologyOut
}

export type RunnerOrderRowOut = components['schemas']['RunnerOrderRowOut']

/** The workspace's default runner order: repo turns, and every agent here (or in
 *  a workspace below without its own) that has no order of its own. */
export async function getRunnerOrder(slug: string): Promise<RunnerOrderRowOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/runner-order', { params: { path: { slug } } })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load the runner order'))
  }
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
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to save the runner order'))
  }
  return Array.from(res.data as unknown as RunnerOrderRowOut[])
}

// --- Content retention (Settings → Retention) ---

export async function getRetention(slug: string): Promise<RetentionOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/retention', { params: { path: { slug } } })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load retention rules'))
  }
  return res.data as unknown as RetentionOut
}

export async function previewRetention(slug: string): Promise<RetentionPreviewOut> {
  const res = await apiV2.GET('/api/workspaces/{slug}/retention/preview', { params: { path: { slug } } })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to preview retention'))
  }
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
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to save the rule'))
  }
  return res.data as unknown as RetentionRuleOut
}

export async function deleteRetentionRule(slug: string, ruleId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/retention/rules/{rule_id}', {
    params: { path: { slug, rule_id: ruleId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to delete the rule'))
  }
}

// ---- system accounts (apps/workspaces/system_accounts.py) ----
// Non-human members (e.g. CloudWatch alarm mail) that cannot sign in. Aligned
// mail from a bound sender address, in this workspace, becomes their turn.
export type SystemAccountOut = components['schemas']['SystemAccountOut']
export type SystemSenderOut = components['schemas']['SystemSenderOut']
export type SystemAccountCreateIn = components['schemas']['SystemAccountCreateIn']
export type SystemAccountUpdateIn = components['schemas']['SystemAccountUpdateIn']
export type SystemRole = NonNullable<SystemAccountCreateIn['role']>

export async function listSystemAccounts(slug: string): Promise<SystemAccountOut[]> {
  const res = await apiV2.GET('/api/workspaces/{slug}/system-accounts/', {
    params: { path: { slug } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to load system accounts'))
  }
  return res.data as unknown as SystemAccountOut[]
}

export async function createSystemAccount(slug: string, body: SystemAccountCreateIn): Promise<SystemAccountOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/system-accounts/', {
    params: { path: { slug } },
    body,
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to create system account'))
  }
  return res.data as unknown as SystemAccountOut
}

export async function updateSystemAccount(
  slug: string,
  accountId: number,
  body: SystemAccountUpdateIn,
): Promise<SystemAccountOut> {
  const res = await apiV2.PATCH('/api/workspaces/{slug}/system-accounts/{account_id}/', {
    params: { path: { slug, account_id: accountId } },
    body,
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to update system account'))
  }
  return res.data as unknown as SystemAccountOut
}

export async function deleteSystemAccount(slug: string, accountId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/system-accounts/{account_id}/', {
    params: { path: { slug, account_id: accountId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to delete system account'))
  }
}

export async function addSystemSender(
  slug: string,
  accountId: number,
  address: string,
  subjectPattern: string,
): Promise<SystemSenderOut> {
  const res = await apiV2.POST('/api/workspaces/{slug}/system-accounts/{account_id}/senders/', {
    params: { path: { slug, account_id: accountId } },
    body: { address, subject_pattern: subjectPattern },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to add sender'))
  }
  return res.data as unknown as SystemSenderOut
}

export async function removeSystemSender(slug: string, accountId: number, senderId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/system-accounts/{account_id}/senders/{sender_id}/', {
    params: { path: { slug, account_id: accountId, sender_id: senderId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to remove sender'))
  }
}
