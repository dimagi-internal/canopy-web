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
  /** The live product screens the cut films, for the reviewer to open. */
  app_links?: { label: string; url: string }[]
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

/** One default the set is built on — ace-web's decision row (spec B). */
export interface Decision {
  id: string
  scope: string
  question: string
  plain_question: string
  options: readonly string[]
  recommended: string
  override: string
  /** Derived: `override` if set, else `recommended`. */
  value: string
  reasoning: string
  override_reasoning: string
  source: string
  evidence_basis: 'stated' | 'inferred' | 'conflicting' | string
  conflict_signals: readonly string[]
  status: 'ai-default' | 'human-decided' | 'overridden' | 'deferred' | string
  revisit_when: string
  review_ask: '' | 'recommended-confirmation' | 'required-before' | string
  needed_by: '' | 'external-share' | 'next-run' | string
  confirm_reason: string
  decided_by: string
  decided_by_verified: boolean
  decided_at: string | null
  confirmed: boolean
  history: readonly { status: string; value: string; decided_by: string; at: string }[]
  supersedes: string
  superseded_by: string
  /** False for a narrative's read-time caveat — shown, not yet a stored row. */
  editable: boolean
  /** Edits staged in the open round, not yet applied. */
  pending?: StagedEdit[]
}

/** One note in a feedback round — names only, never an email. */
export interface RoundNote {
  id: number
  kind: FeedbackKind | string
  body: string
  suggested_text: string
  author_name: string
  anchor_id: string
  target_kind: string
  target_ref: string
  target_version: number | null
  state: string
  disposition_note: string
  resolved_in_version: number | null
  created_at: string
  /** True for the signed-in viewer's own notes. */
  mine: boolean
}

/** A decision edit held until the round closes. */
export interface StagedEdit {
  edit_id: string
  key: string
  action: string
  value: string
  reasoning: string
  by_name: string
  at: string
}

export interface RoundPayload {
  number: number
  state: string
  opened_at: string
  opened_by: string
  closed_at: string | null
  close_note: string
  pinned_versions: Record<string, number>
  note_count: number
  people_count: number
  notes: RoundNote[]
  staged_edits: StagedEdit[]
}

export type RoundSummary = Omit<RoundPayload, 'notes' | 'staged_edits'>

export type DecisionEdit = {
  action: 'confirm' | 'pick' | 'write_in' | 'undo'
  value?: string
  reasoning?: string
  author_name?: string
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
  /** True for editors, who open and close feedback rounds. */
  can_manage?: boolean
  acts: StoryboardAct[]
  /** The set's decision log. Absent on an older response. */
  decisions?: Decision[]
  /** The feedback round open now, with everyone's notes. */
  round?: RoundPayload | null
  /** The latest finished round, so people can see what happened to their notes. */
  previous_round?: RoundPayload | null
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

async function postJson<T>(url: string, body: unknown, fallback: string): Promise<T> {
  const resp = await fetch(apiUrl(url), {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken() ?? '' },
    body: JSON.stringify(body),
  })
  if (!resp.ok) {
    let detail = ''
    try {
      detail = (await resp.json())?.detail ?? ''
    } catch {
      /* ignore */
    }
    throw new Error(detail || `${fallback} (${resp.status})`)
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
  return postJson(
    withToken(`/api/storyboards/${encodeURIComponent(slug)}/feedback`, token, ws),
    payload,
    'Couldn’t save that note',
  )
}

/** Confirm, pick, write in or undo one decision. Members only; returns the updated row. */
export function editDecision(
  slug: string,
  key: string,
  body: DecisionEdit,
  token?: string | null,
  ws?: string | null,
): Promise<Decision> {
  return postJson(
    withToken(
      `/api/storyboards/${encodeURIComponent(slug)}/decisions/${encodeURIComponent(key)}/edit`,
      token,
      ws,
    ),
    body,
    'That did not save',
  )
}

export function openRound(slug: string, ws?: string | null): Promise<RoundSummary> {
  return postJson(
    withToken(`/api/storyboards/${encodeURIComponent(slug)}/rounds`, null, ws),
    {},
    'Couldn’t open the round',
  )
}

export function closeRound(
  slug: string,
  number: number,
  body: { close_note?: string; picks?: Record<string, string> },
  ws?: string | null,
): Promise<RoundSummary> {
  return postJson(
    withToken(`/api/storyboards/${encodeURIComponent(slug)}/rounds/${number}/close`, null, ws),
    body,
    'Couldn’t close the round',
  )
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
