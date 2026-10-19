// Pairing invitations: a new cloud box pairs by its EC2 instance role once the
// person who invited it (`up.sh`) approves here with a passkey. Its own module
// so the approve page can be tested without ./harness.
import { apiV2 } from './client.v2'
import type { components } from './generated'

export type RunnerInvitationOut = components['schemas']['RunnerInvitationOut']

export class RunnerInvitationError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function unwrap<T>(res: { data?: T; error?: unknown; response: Response }, what: string): T {
  if (res.error !== undefined || res.data === undefined) {
    const detail = (res.error as { detail?: unknown } | undefined)?.detail
    throw new RunnerInvitationError(res.response.status, typeof detail === 'string' ? detail : `${what} failed`)
  }
  return res.data
}

export async function getRunnerInvitation(id: string): Promise<RunnerInvitationOut> {
  const res = await apiV2.GET('/api/harness/runner-invitations/{invitation_id}', {
    params: { path: { invitation_id: id } },
  })
  return unwrap(res, 'getRunnerInvitation') as RunnerInvitationOut
}

/** pending → approved. Browser session + a fresh passkey step-up: a 403
 *  `step_up_required` is answered by `apiV2`'s step-up middleware
 *  (`auth/stepUp.ts`), which asks for the passkey and retries this call. */
export async function approveRunnerInvitation(id: string): Promise<RunnerInvitationOut> {
  const res = await apiV2.POST('/api/harness/runner-invitations/{invitation_id}/approve', {
    params: { path: { invitation_id: id } },
  })
  return unwrap(res, 'approveRunnerInvitation') as RunnerInvitationOut
}
