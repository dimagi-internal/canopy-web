// Meetings: a thin typed wrapper over the generated client. A meeting is private to the person
// who saved it until they attach it to a project.
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { ensureOk, unwrap } from './problem'

type Schemas = components['schemas']

export type MeetingRow = Schemas['MeetingRowOut']
export type Meeting = Schemas['MeetingOut']
export type MeetingArtifact = Schemas['MeetingArtifactOut']
export type MeetingInput = Schemas['MeetingIn']

export async function listMeetings(params: {
  workspace?: string
  project?: string
  q?: string
  offset?: number
}): Promise<{ meetings: MeetingRow[]; has_more: boolean }> {
  const res = await apiV2.GET('/api/meetings/', { params: { query: params } })
  return unwrap(res, 'listMeetings') as { meetings: MeetingRow[]; has_more: boolean }
}

export async function getMeeting(id: string): Promise<Meeting> {
  const res = await apiV2.GET('/api/meetings/{meeting_id}', { params: { path: { meeting_id: id } } })
  return unwrap(res, 'getMeeting') as Meeting
}

/** The generated type marks fields the server defaults as required, so leave out any you don't set. */
export async function createMeeting(body: Partial<MeetingInput>): Promise<Meeting> {
  const res = await apiV2.POST('/api/meetings/', { body: body as MeetingInput })
  return unwrap(res, 'createMeeting') as Meeting
}

export async function updateMeeting(id: string, body: Schemas['MeetingPatch']): Promise<Meeting> {
  const res = await apiV2.PATCH('/api/meetings/{meeting_id}', { params: { path: { meeting_id: id } }, body })
  return unwrap(res, 'updateMeeting') as Meeting
}

export async function deleteMeeting(id: string): Promise<void> {
  const res = await apiV2.DELETE('/api/meetings/{meeting_id}', { params: { path: { meeting_id: id } } })
  ensureOk(res, 'deleteMeeting')
}

export async function addMeetingArtifact(id: string, body: Schemas['MeetingArtifactIn']): Promise<MeetingArtifact> {
  const res = await apiV2.POST('/api/meetings/{meeting_id}/artifacts', { params: { path: { meeting_id: id } }, body })
  return unwrap(res, 'addMeetingArtifact')
}

export async function startMeetingFollowUp(id: string, body: Schemas['MeetingFollowUpIn']) {
  const res = await apiV2.POST('/api/meetings/{meeting_id}/follow-up', { params: { path: { meeting_id: id } }, body })
  return unwrap(res, 'startMeetingFollowUp')
}

/** Text that is only a link is a pointer for the agent to read, not notes to save. */
export function linkOnly(text: string): string | null {
  const trimmed = text.trim()
  return /^https?:\/\/\S+$/.test(trimmed) ? trimmed : null
}

/** What to call a pasted link's source. Goes by the link's host, wherever else the words appear in it. */
export function linkSource(link: string): 'google_doc' | 'link' {
  try {
    return new URL(link).hostname === 'docs.google.com' ? 'google_doc' : 'link'
  } catch {
    return 'link'
  }
}

export const KIND_LABELS: Record<string, string> = {
  transcript: 'Transcript',
  summary: 'Summary',
  notes: 'Notes',
  recap: "Agent's follow-up",
}
