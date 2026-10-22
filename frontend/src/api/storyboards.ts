/**
 * API client for /api/storyboards — the shared arc and its feedback.
 *
 * Every read threads `?t=<share_token>` so a token-holding viewer with no
 * Dimagi login is served; the backend self-enforces (member OR matching token)
 * and 404s on a wrong one, so there is nothing to branch on here. On `apiV2`:
 * a viewer's page (/w/<ws>/storyboard/…, /w/<ws>/narrative/…) is a scoped viewer
 * path, so a 401 there never bounces to login.
 *
 * Acts, entries and cuts are the generated schemas. `Storyboard` narrows the
 * generated board's free-string `capability` / `layout` and its decision rows.
 */

import { apiV2, shareQuery } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'
import type { DddNarration } from './ddd'

type Schemas = components['schemas']

export type Capability = 'read' | 'comment' | 'suggest'
export type Layout = 'review' | 'reel'
export type FeedbackKind = 'comment' | 'suggestion'

/** A place where what the script says and what the data shows disagree. */
export type Divergence = Schemas['Divergence']

/** One recorded cut of an entry, with what a reviewer needs to judge it. */
export type EntryCut = Schemas['EntryCutOut']

export type StoryboardEntry = Schemas['EntryOut']

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

export type StoryboardAct = Schemas['StoryboardActOut']

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
export type RoundNote = Schemas['RoundNoteOut']

/** A decision edit held until the round closes. */
export type StagedEdit = Schemas['StagedEditOut']

export type RoundPayload = Schemas['RoundPayloadOut']

export type RoundSummary = Omit<RoundPayload, 'notes' | 'staged_edits'>

export type DecisionEdit = {
  action: 'confirm' | 'pick' | 'write_in' | 'undo'
  value?: string
  reasoning?: string
  author_name?: string
}

export type Storyboard = Omit<
  Schemas['StoryboardOut'],
  'brief' | 'capability' | 'layout' | 'decisions'
> & {
  /** Who it is for, the point to land, the data disclosure, the short version,
   *  and the caveats. Absent on boards created before it existed. */
  brief?: StoryboardBrief
  capability: Capability
  /** `reel` = the finished product: videos, one line each, nothing else. */
  layout: Layout
  /** The set's decision log. Absent on an older response. */
  decisions?: Decision[]
}

export type Note = Omit<Schemas['NoteOut'], 'kind'> & { kind: FeedbackKind }

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

// Bodies: the server fills every field the caller leaves out (its pydantic
// defaults), which the generated `*In` types mark required — hence the casts.

export async function getStoryboard(
  slug: string,
  token?: string | null,
  ws?: string | null,
): Promise<Storyboard> {
  // `ws`: the workspace the page's URL names — the API 404s a board that does
  // not live there (canopy-web#1337).
  const res = await apiV2.GET('/api/storyboards/{slug}', {
    params: { path: { slug }, query: shareQuery(token, { ws }) },
  })
  return unwrap(res, 'getStoryboard') as unknown as Storyboard
}

/** What came back. Members only — a 401/404 here just means "not yours". */
export async function getStoryboardNotes(slug: string): Promise<{ items: Note[] }> {
  const res = await apiV2.GET('/api/storyboards/{slug}/notes', { params: { path: { slug } } })
  return unwrap(res, 'getStoryboardNotes') as unknown as { items: Note[] }
}

/** Leave a note. Returns the ingest summary; throws with a readable message on 403. */
export async function leaveFeedback(
  slug: string,
  payload: LeaveFeedbackIn,
  token?: string | null,
  ws?: string | null,
): Promise<{ created: number }> {
  const res = await apiV2.POST('/api/storyboards/{slug}/feedback', {
    params: { path: { slug }, query: shareQuery(token, { ws }) },
    body: payload as Schemas['AnonFeedbackIn'],
  })
  return unwrap(res, 'Couldn’t save that note') as unknown as { created: number }
}

/** Confirm, pick, write in or undo one decision. Members only; returns the updated row. */
export async function editDecision(
  slug: string,
  key: string,
  body: DecisionEdit,
  token?: string | null,
  ws?: string | null,
): Promise<Decision> {
  const res = await apiV2.POST('/api/storyboards/{slug}/decisions/{key}/edit', {
    params: { path: { slug, key }, query: shareQuery(token, { ws }) },
    body: body as Schemas['DecisionEditIn'],
  })
  return unwrap(res, 'That did not save') as unknown as Decision
}

export async function openRound(slug: string, ws?: string | null): Promise<RoundSummary> {
  const res = await apiV2.POST('/api/storyboards/{slug}/rounds', {
    params: { path: { slug }, query: shareQuery(null, { ws }) },
  })
  return unwrap(res, 'Couldn’t open the round') as unknown as RoundSummary
}

export async function closeRound(
  slug: string,
  number: number,
  body: { close_note?: string; picks?: Record<string, string> },
  ws?: string | null,
): Promise<RoundSummary> {
  const res = await apiV2.POST('/api/storyboards/{slug}/rounds/{number}/close', {
    params: { path: { slug, number }, query: shareQuery(null, { ws }) },
    body: body as Schemas['RoundCloseIn'],
  })
  return unwrap(res, 'Couldn’t close the round') as unknown as RoundSummary
}

/** One narrative of a board, as its own page (/w/<ws>/narrative/<slug>?b=). */
export type BoardNarrative = Omit<
  Schemas['NarrativeReadOut'],
  'narration' | 'previous_narration' | 'capability'
> & {
  narration: DddNarration[]
  previous_narration: DddNarration[]
  capability: Capability
}

/** Read one narrative of a board. Token-gated like the board itself. */
export async function getBoardNarrative(
  board: string,
  slug: string,
  token?: string | null,
  ws?: string | null,
): Promise<BoardNarrative> {
  const res = await apiV2.GET('/api/storyboards/{slug}/narratives/{narrative_slug}', {
    params: { path: { slug: board, narrative_slug: slug }, query: shareQuery(token, { ws }) },
  })
  return unwrap(res, 'getBoardNarrative') as unknown as BoardNarrative
}

/** One row of the index — what a member sees before opening a board. */
// `share_url`: absolute, token-bearing link. Members only — this is the thing you send.
export type StoryboardListItem = Omit<Schemas['StoryboardListItemOut'], 'capability' | 'layout'> & {
  capability: Capability
  layout: Layout
}

/** Every board in a workspace the caller belongs to. Members only. */
export async function listStoryboards(): Promise<{ items: StoryboardListItem[] }> {
  const res = await apiV2.GET('/api/storyboards/')
  return unwrap(res, 'listStoryboards') as unknown as { items: StoryboardListItem[] }
}
