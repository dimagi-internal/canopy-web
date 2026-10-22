// Laptop pairing by browser and the runner downloads (spec
// 2026-10-19-runner-installers). Its own module so the /pair page and the
// download buttons can be tested without ./harness.
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { ApiError, unwrap } from './problem'

export type RunnerPairingOut = components['schemas']['RunnerPairingOut']
export type RunnerPairingApprovedOut = components['schemas']['RunnerPairingApprovedOut']
export type RunnerDownloadsOut = components['schemas']['RunnerDownloadsOut']
export type RunnerDownloadOut = components['schemas']['RunnerDownloadOut']

/** The shared `ApiError` under its old name (callers branch on `.status`). */
export { ApiError as RunnerPairingError }


export async function getRunnerPairing(code: string): Promise<RunnerPairingOut> {
  const res = await apiV2.GET('/api/harness/runner-pairings/{user_code}', {
    params: { path: { user_code: code } },
  })
  return unwrap(res, 'getRunnerPairing') as RunnerPairingOut
}

/** Browser session + a fresh passkey step-up: a 403 `step_up_required` is
 *  answered by `apiV2`'s step-up middleware, which asks for the passkey and
 *  retries this call. */
export async function approveRunnerPairing(
  code: string,
  body: { workspace: string; name: string },
): Promise<RunnerPairingApprovedOut> {
  const res = await apiV2.POST('/api/harness/runner-pairings/{user_code}/approve', {
    params: { path: { user_code: code } },
    body,
  })
  return unwrap(res, 'approveRunnerPairing') as RunnerPairingApprovedOut
}

export async function declineRunnerPairing(code: string): Promise<void> {
  const res = await apiV2.POST('/api/harness/runner-pairings/{user_code}/decline', {
    params: { path: { user_code: code } },
  })
  unwrap(res, 'declineRunnerPairing')
}

export async function getRunnerDownloads(): Promise<RunnerDownloadsOut> {
  const res = await apiV2.GET('/api/harness/runner-releases/downloads')
  return unwrap(res, 'getRunnerDownloads') as RunnerDownloadsOut
}
