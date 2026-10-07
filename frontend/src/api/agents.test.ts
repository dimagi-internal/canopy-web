import { describe, expect, it, vi } from 'vitest'

const GET = vi.fn(async () => ({ data: [], error: undefined, response: new Response() }))
const POST = vi.fn(async () => ({
  data: { task: {}, action: {}, turn_ids: [] },
  error: undefined,
  response: new Response(),
}))
vi.mock('./client.v2', () => ({ apiV2: { GET, POST }, WORKSPACE_HEADER: 'X-Workspace' }))
const { listTasks, actOnTask, AgentApiError } = await import('./agents')

describe('task client', () => {
  it('passes filters as query params', async () => {
    await listTasks('eva', { waiting: 'me', project: 'P2' })
    expect(GET).toHaveBeenCalledWith('/api/agents/{slug}/tasks/', {
      params: { path: { slug: 'eva' }, query: { waiting: 'me', project: 'P2' } },
    })
  })
  it('acts by ext_id', async () => {
    await actOnTask('eva', 'T2', 'reply', 'Tuesday')
    expect(POST).toHaveBeenCalledWith('/api/agents/{slug}/tasks/{ref}/actions', {
      params: { path: { slug: 'eva', ref: 'T2' } },
      body: { action: 'reply', comment: 'Tuesday' },
    })
  })
  it('a failed act carries the HTTP status and detail (409 → the card refetches)', async () => {
    POST.mockResolvedValueOnce({
      data: undefined,
      error: { detail: 'This ask is already closed.' },
      response: new Response(null, { status: 409 }),
    } as never)
    const err = await actOnTask('eva', 'T2', 'approve').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(AgentApiError)
    expect(err).toMatchObject({ status: 409, detail: 'This ask is already closed.' })
  })
  it('a validation 422 (detail is a list) surfaces the first msg', async () => {
    POST.mockResolvedValueOnce({
      data: undefined,
      error: { detail: [{ loc: ['body', 'comment'], msg: 'A reply needs text.', type: 'value_error' }] },
      response: new Response(null, { status: 422 }),
    } as never)
    const err = await actOnTask('eva', 'T2', 'reply', '').catch((e: unknown) => e)
    expect(err).toMatchObject({ status: 422, detail: 'A reply needs text.' })
  })
})
