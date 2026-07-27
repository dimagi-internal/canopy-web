/**
 * API client for /api/storyboards — the shared arc and its feedback.
 *
 * Every read threads `?t=<share_token>` so a token-holding viewer with no
 * Dimagi login is served; the backend self-enforces (member OR matching token)
 * and 404s on a wrong one, so there is nothing to branch on here.
 */

import { apiUrl, getCsrfToken } from './base'

export type Capability = 'read' | 'comment' | 'suggest'
export type FeedbackKind = 'comment' | 'suggestion'

export interface StoryboardEntry {
  narrative_slug: string
  title: string
  lede: string
  version: number | null
  video_url: string | null
  video_viewer_url: string | null
  published: boolean
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
  capability: Capability
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

function withToken(url: string, token?: string | null): string {
  return token ? `${url}?t=${encodeURIComponent(token)}` : url
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

export function getStoryboard(slug: string, token?: string | null): Promise<Storyboard> {
  return getJson(withToken(`/api/storyboards/${encodeURIComponent(slug)}`, token))
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
): Promise<{ created: number }> {
  const resp = await fetch(
    apiUrl(withToken(`/api/storyboards/${encodeURIComponent(slug)}/feedback`, token)),
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
