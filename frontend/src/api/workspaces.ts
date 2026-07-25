// Workspaces API — the tenant list backing the header switcher + provider,
// plus the members/invites admin surface (Task 4) and the pre-auth invite
// preview + accept calls the /invite/:token page drives (Task 5).
import { apiV2 } from './client.v2'
import { problemMessage } from './problem'
import type { components } from './generated'

export type WorkspaceOut = components['schemas']['WorkspaceOut']
export type MemberOut = components['schemas']['MemberOut']
export type InviteOut = components['schemas']['InviteOut']
export type InviteRole = components['schemas']['InviteCreateIn']['role']
export type InvitePreviewOut = components['schemas']['InvitePreviewOut']

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

export async function removeMember(slug: string, userId: number): Promise<void> {
  const res = await apiV2.DELETE('/api/workspaces/{slug}/members/{user_id}/', {
    params: { path: { slug, user_id: userId } },
  })
  if (!res.response.ok) {
    throw new WorkspaceApiError(res.response.status, problemMessage(res.error, 'Failed to remove member'))
  }
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
