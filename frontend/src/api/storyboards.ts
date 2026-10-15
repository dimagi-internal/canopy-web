/**
 * API client for /api/storyboards — the shared arc and its feedback.
 *
 * Every read threads `?t=<share_token>` so a token-holding viewer with no
 * Dimagi login is served; the backend self-enforces (member OR matching token)
 * and 404s on a wrong one, so there is nothing to branch on here.
 */

import { apiUrl, getCsrfToken } from './base'

export type Capability = 'read' | 'comment' | 'suggest'
export type Layout = 'review' | 'reel'
export type FeedbackKind = 'comment' | 'suggestion'

/** A place where what the script says and what the data shows disagree. */
export interface Divergence {
  script: string
  data: string
  resolution: string
  /** Set when it belongs to one narrative rather than the whole set. */
  narrative_slug: string
}

/** One recorded cut of an entry, with what a reviewer needs to judge it. */
export interface EntryCut {
  cut_id: string
  /** What a note on this cut anchors to: `cut:<cut_id>`. */
  anchor_id: string
  title: string
  /** False for a cut declared by the publisher that has no video yet. */
  recorded: boolean
  video_url: string | null
  video_viewer_url: string | null
  poster_url: string | null
  duration_sec: number | null
  word_count: number
  scenes: { id: string; title: string; text: string }[]
  /** What is on screen and where the cursor goes. */
  screen_notes: string
  /** Why this cut matters to the board's audience. */
  audience_note: string
  /** Where the on-screen data comes from. */
  provenance: string
  optional: boolean
}

export interface StoryboardEntry {
  narrative_slug: string
  title: string
  lede: string
  version: number | null
  video_url: string | null
  video_viewer_url: string | null
  published: boolean
  /** A recorded narrative's cuts, in running order. Empty for an explainer. */
  cuts?: EntryCut[]
  assumptions?: string[]
  divergences?: Divergence[]
  open_questions?: string[]
}

/** Framing for the whole set — every field optional. */
export interface StoryboardBrief {
  audience?: string
  purpose?: string
  data_disclosure?: string
  short_version?: {
    label?: string
    note?: string
    items: { narrative_slug: string; cut_id?: string }[]
  } | null
  assumptions?: string[]
  divergences?: Divergence[]
  open_questions?: string[]
}

export interface StoryboardAct {
  /** What an act-level note anchors to (`act:<id>`). */
  anchor_id: string
  title: string
  prose: string
  entries: StoryboardEntry[]
}

export interface Storyboard {
  slug: string
  title: string
  lede: string
  /** Who it is for, the point to land, the data disclosure, the short version,
   *  and the caveats. Absent on boards created before it existed. */
  brief?: StoryboardBrief
  capability: Capability
  /** `reel` = the finished product: videos, one line each, nothing else. */
  layout: Layout
  /** True for the people who SENT the link — the only ones shown the notes. */
  is_member: boolean
  acts: StoryboardAct[]
}

export interface Note {
  id: number
  kind: FeedbackKind
  body: string
  suggested_text: string
  author_name: string
  channel: string
  state: string
  target_kind: string
  target_ref: string
  target_version: number | null
  anchor_id: string
  created_at: string
}

export interface LeaveFeedbackIn {
  narrative_slug?: string
  target_version?: number | null
  anchor_id?: string
  kind?: FeedbackKind
  body?: string
  suggested_text?: string
  author_name?: string
  author_email?: string
}

/** `url` with the share token and the workspace the page's URL names. The API
 * 404s a board that does not live in `ws` (canopy-web#1337). */
function withToken(url: string, token?: string | null, ws?: string | null): string {
  const q = new URLSearchParams()
  if (token) q.set('t', token)
  if (ws) q.set('ws', ws)
  const qs = q.toString()
  return qs ? `${url}?${qs}` : url
}

async function getJson<T>(url: string): Promise<T> {
  const resp = await fetch(apiUrl(url), { credentials: 'same-origin' })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(detail || `Request failed (${resp.status})`)
  }
  return resp.json() as Promise<T>
}

export function getStoryboard(
  slug: string,
  token?: string | null,
  ws?: string | null,
): Promise<Storyboard> {
  return getJson(withToken(`/api/storyboards/${encodeURIComponent(slug)}`, token, ws))
}

/** What came back. Members only — a 401/404 here just means "not yours". */
export function getStoryboardNotes(slug: string): Promise<{ items: Note[] }> {
  return getJson(`/api/storyboards/${encodeURIComponent(slug)}/notes`)
}

/** Leave a note. Returns the ingest summary; throws with a readable message on 403. */
export async function leaveFeedback(
  slug: string,
  payload: LeaveFeedbackIn,
  token?: string | null,
  ws?: string | null,
): Promise<{ created: number }> {
  const resp = await fetch(
    apiUrl(withToken(`/api/storyboards/${encodeURIComponent(slug)}/feedback`, token, ws)),
    {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() ?? '' },
      body: JSON.stringify(payload),
    },
  )
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(detail || `Couldn’t save that note (${resp.status})`)
  }
  return resp.json()
}

/** One row of the index — what a member sees before opening a board. */
export interface StoryboardListItem {
  slug: string
  title: string
  lede: string
  capability: Capability
  layout: Layout
  act_count: number
  /** Absolute, token-bearing link. Members only — this is the thing you send. */
  share_url: string | null
}

/** Every board in a workspace the caller belongs to. Members only. */
export function listStoryboards(): Promise<{ items: StoryboardListItem[] }> {
  return getJson('/api/storyboards/')
}
