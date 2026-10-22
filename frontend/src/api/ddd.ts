/**
 * API client for the /api/ddd surface (DDD run views), on `apiV2`.
 *
 * The release view is anonymous-capable: its page (/w/<ws>/ddd-release/…) is a
 * scoped viewer path, so apiV2 neither rewrites it onto /api/w/<ws>/ nor bounces
 * a 401 to login, and the page's `?t=` share token rides along (`shareQuery`).
 */

import { apiV2, shareQuery } from './client.v2'
import type { components } from './generated'
import { ensureOk, unwrap } from './problem'

type Schemas = components['schemas']

export type WalkthroughKind = Schemas['RunArtifactOut']['kind']
export type DddLinkKind = Schemas['WalkthroughLink']['kind']

/** One scene of a version's narration — free-form JSON the schema leaves untyped. */
export interface DddNarration {
  scene?: number
  id?: string
  title?: string
  persona?: string
  provenance?: string
  text: string
  // features and other fields may be present; kept open.
  [k: string]: unknown
}

export type DddNarrativeListItem = Schemas['NarrativeListItemOut']
export type DddNarrativeRun = Schemas['NarrativeRunOut']
export type DddNarrativeStory = Schemas['NarrativeStoryOut']
/** One recorded-narrative cut and its pinned video (canopy-web#1288). */
export type DddNarrativeCut = Schemas['NarrativeCutOut']
// Per-scene narration is carried so the client can render a plain-language
// before/after (vN -> vN+1) diff without a second fetch.
export type DddNarrativeVersion = Omit<Schemas['NarrativeVersionOut'], 'narration'> & {
  narration: readonly DddNarration[]
}
export type DddNarrativeDetail = Omit<Schemas['NarrativeDetailOut'], 'versions'> & {
  versions: readonly DddNarrativeVersion[]
}
export type DddRunArtifact = Schemas['RunArtifactOut']
export type DddRunArtifactRef = Schemas['RunArtifactRefOut']
export type DddRunNarrative = Omit<Schemas['RunNarrativeOut'], 'narration' | 'personas'> & {
  narration: readonly DddNarration[]
  personas: Record<string, { name?: string; role?: string; color?: string; org?: string }>
}
export type DddLink = Schemas['WalkthroughLink']
// `slides` = the canopy:walkthrough HTML slideshow (role=deck); `documentation`
// = the feature docs page (role=docs).
export type DddRunPackage = Omit<Schemas['RunPackageOut'], 'narrative'> & {
  narrative?: DddRunNarrative | null
}
/** The curated, shareable run-release payload (the clean public-capable page). */
export type DddRunRelease = Omit<Schemas['RunReleaseOut'], 'narrative'> & {
  narrative?: DddRunNarrative | null
}
export type SetNarrativeVisibilityResult = Schemas['NarrativeVisibilityOut']

export interface ListNarrativesParams {
  project?: string
  mine?: boolean
}

export async function listNarratives(
  params: ListNarrativesParams = {},
): Promise<DddNarrativeListItem[]> {
  const res = await apiV2.GET('/api/ddd/narratives/', {
    params: {
      query: {
        ...(params.project ? { project: params.project } : {}),
        ...(params.mine ? { mine: 'true' } : {}),
      },
    },
  })
  return unwrap(res, 'listNarratives') as unknown as DddNarrativeListItem[]
}

export async function getNarrative(slug: string): Promise<DddNarrativeDetail> {
  const res = await apiV2.GET('/api/ddd/narratives/{slug}/', { params: { path: { slug } } })
  return unwrap(res, 'getNarrative') as unknown as DddNarrativeDetail
}

/** Make an entire narrative public or private (cascades to all artifacts + reviews). */
export async function setNarrativeVisibility(
  slug: string,
  makePublic: boolean,
): Promise<SetNarrativeVisibilityResult> {
  const res = await apiV2.PATCH('/api/ddd/narratives/{slug}/visibility/', {
    params: { path: { slug } },
    body: { visibility: makePublic ? 'link' : 'private' },
  })
  return unwrap(res, 'setNarrativeVisibility')
}

export async function getRun(runId: string): Promise<DddRunPackage> {
  const res = await apiV2.GET('/api/ddd/runs/{run_id}/', { params: { path: { run_id: runId } } })
  return unwrap(res, 'getRun') as unknown as DddRunPackage
}

/**
 * Fetch the clean, shareable release view of a run. Anonymous-capable: pass the
 * `?t=<share_token>` from the URL so a public viewer (no Dimagi login) is
 * admitted; members are recognised by their session cookie and `token` is
 * unnecessary. `ws` is the workspace the page's URL names; the API 404s a run
 * elsewhere.
 */
export async function getRelease(
  runId: string,
  token?: string | null,
  ws?: string | null,
): Promise<DddRunRelease> {
  const res = await apiV2.GET('/api/ddd/release/{run_id}/', {
    params: { path: { run_id: runId }, query: shareQuery(token, { ws }) },
  })
  return unwrap(res, 'getRelease') as unknown as DddRunRelease
}

/** Delete a single run: its walkthroughs + reviews (best-effort Drive cleanup). */
export async function deleteRun(runId: string): Promise<void> {
  const res = await apiV2.DELETE('/api/ddd/runs/{run_id}/', { params: { path: { run_id: runId } } })
  ensureOk(res, 'deleteRun')
}

/** Delete one narrative version and the runs nested under it. */
export async function deleteNarrativeVersion(slug: string, version: number): Promise<void> {
  const res = await apiV2.DELETE('/api/ddd/narratives/{slug}/versions/{version}/', {
    params: { path: { slug, version } },
  })
  ensureOk(res, 'deleteNarrativeVersion')
}

/** Delete an entire narrative: every version + run for the slug. */
export async function deleteNarrative(slug: string): Promise<void> {
  const res = await apiV2.DELETE('/api/ddd/narratives/{slug}/', { params: { path: { slug } } })
  ensureOk(res, 'deleteNarrative')
}
