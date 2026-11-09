// Listen sessions: the phone opens a relay to one of its owner's computers. The audio
// never touches these routes; it rides /ws/listen/<id>/, end-to-end encrypted.
import { apiV2 } from './client.v2'
import type { components } from './generated'
import { unwrap } from './problem'

type Schemas = components['schemas']

export type ListenRunner = Schemas['ListenRunnerOut']
export type ListenSession = Schemas['ListenSessionOut']
export type ListenHello = Schemas['ListenHello']
export type ListenKind = Schemas['ListenSessionIn']['kind']

export async function listListenRunners(): Promise<ListenRunner[]> {
  const res = await apiV2.GET('/api/listen/runners')
  return unwrap(res, 'listListenRunners').items as ListenRunner[]
}

/** Opens a session and rings the computer. Throws ApiError: 404 not yours, 409 offline or busy. */
export async function openListenSession(runnerId: string, kind: ListenKind, hello: ListenHello): Promise<string> {
  const res = await apiV2.POST('/api/listen/sessions', { body: { runner_id: runnerId, kind, hello } })
  return unwrap(res, 'openListenSession').id
}

export async function getListenSession(id: string): Promise<ListenSession> {
  const res = await apiV2.GET('/api/listen/sessions/{session_id}', { params: { path: { session_id: id } } })
  return unwrap(res, 'getListenSession') as ListenSession
}

export async function endListenSession(id: string): Promise<ListenSession> {
  const res = await apiV2.POST('/api/listen/sessions/{session_id}/end', { params: { path: { session_id: id } } })
  return unwrap(res, 'endListenSession') as ListenSession
}
