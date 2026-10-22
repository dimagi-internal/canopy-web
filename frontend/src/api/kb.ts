// A workspace project's knowledge base: its sources, the owner's review of a
// Drive source's files, who may query it, and its version log
// (apps/agents/kb_api.py).

import { apiV2 } from './client.v2'
import type { components } from './generated'
import { ensureOk, unwrap } from './problem'

export type KbReviewOut = components['schemas']['KbReviewOut']
export type KbReviewSourceOut = components['schemas']['KbReviewSourceOut']
export type KbDriveFileOut = components['schemas']['KbDriveFileOut']
export type KbVersionOut = components['schemas']['KbVersionOut']
// openapi-typescript makes a defaulted field required, so `release_hold` is filled in here.
export type KbApprovalIn = Omit<components['schemas']['KbApprovalIn'], 'release_hold'> & { release_hold?: boolean }

/** The review page: every source, and each Drive file with the owner's decision. 404: not yours to see. */
export async function getKbReview(workspace: string, handle: string): Promise<KbReviewOut> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/kb/review', {
    params: { path: { workspace, handle } },
  })
  return unwrap(res, 'getKbReview') as KbReviewOut
}

/** The owner switches files and folders on or off (a folder's decision cascades server-side). */
export async function decideKbFiles(
  workspace: string,
  handle: string,
  source: number,
  files: KbApprovalIn[],
): Promise<void> {
  const res = await apiV2.POST('/api/workspace-projects/{workspace}/{handle}/kb/review/approvals', {
    params: { path: { workspace, handle } },
    body: { source, files: files.map((f) => ({ ...f, release_hold: f.release_hold ?? false })) },
  })
  ensureOk(res, 'decideKbFiles')
}

/** The owner names who may query a KB made from their files. */
export async function setKbReaders(
  workspace: string,
  handle: string,
  change: { add?: string[]; remove?: string[] },
): Promise<KbReviewOut> {
  const res = await apiV2.POST('/api/workspace-projects/{workspace}/{handle}/kb/readers', {
    params: { path: { workspace, handle } },
    body: { add: change.add ?? [], remove: change.remove ?? [] },
  })
  return unwrap(res, 'setKbReaders') as KbReviewOut
}

/** The version log, newest first. */
export async function listKbVersions(workspace: string, handle: string): Promise<KbVersionOut[]> {
  const res = await apiV2.GET('/api/workspace-projects/{workspace}/{handle}/kb/versions', {
    params: { path: { workspace, handle } },
  })
  return unwrap(res, 'listKbVersions') as KbVersionOut[]
}
