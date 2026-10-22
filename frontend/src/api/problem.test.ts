import { describe, it, expect } from 'vitest'
import { ApiError, ensureOk, problemMessage, unwrap } from './problem'

describe('ApiError', () => {
  it('reads the problem fields and keeps the raw body', () => {
    const body = {
      type: 'https://canopy-web.dimagi.com/problems/not-found',
      title: 'Not found',
      status: 404,
      detail: 'No such review.',
      extras: { pins: ['dimagi'] },
    }
    const e = ApiError.from(404, body, 'Failed to load')
    expect(e).toBeInstanceOf(Error)
    expect(e.status).toBe(404)
    expect(e.message).toBe('No such review.')
    expect(e.detail).toBe('No such review.')
    expect(e.title).toBe('Not found')
    expect(e.type).toBe(body.type)
    expect(e.code).toBe('not-found')
    expect(e.extras).toEqual({ pins: ['dimagi'] })
    expect(e.body).toBe(body)
  })

  it('prefers a top-level `error` member as the code', () => {
    // The vault's 404 and the passkey step-up 403 carry their reason there.
    const e = ApiError.from(403, { type: 'x/forbidden', title: 'Forbidden', error: 'step_up_required' }, 'no')
    expect(e.code).toBe('step_up_required')
    expect(e.message).toBe('Forbidden')
  })

  it('takes the first message of a validation 422', () => {
    const e = ApiError.from(422, { detail: [{ loc: ['body', 'text'], msg: 'A reply needs text.' }] }, 'no')
    expect(e.detail).toBe('A reply needs text.')
    expect(e.message).toBe('A reply needs text.')
  })

  it('falls back cleanly for a non-problem body', () => {
    const e = ApiError.from(502, '<html>502</html>', 'Failed to load')
    expect(e.message).toBe('Failed to load')
    expect(e.detail).toBe('')
    expect(e.code).toBe('')
    expect(e.extras).toBeUndefined()
  })
})

function res(status: number, data?: unknown, error?: unknown) {
  return { data, error, response: new Response(null, { status }) }
}

describe('unwrap / ensureOk', () => {
  it('returns the data of a success', () => {
    expect(unwrap(res(200, { id: 1 }), 'getThing')).toEqual({ id: 1 })
  })

  it("throws an ApiError carrying the server's detail", () => {
    let caught: unknown
    try {
      unwrap(res(409, undefined, { title: 'Conflict', detail: 'Already closed.' }), 'actOnTask')
    } catch (e) {
      caught = e
    }
    expect(caught).toBeInstanceOf(ApiError)
    expect((caught as ApiError).status).toBe(409)
    expect((caught as ApiError).message).toBe('Already closed.')
  })

  it('names the operation and status when the server says nothing', () => {
    expect(() => unwrap(res(500), 'listAgents')).toThrow('listAgents failed (500)')
  })

  it('uses a sentence fallback verbatim', () => {
    expect(() => ensureOk(res(500, undefined, {}), 'Failed to load members')).toThrow(/^Failed to load members$/)
  })

  it('treats a bodiless success as an error for unwrap but not for ensureOk', () => {
    expect(() => ensureOk(res(204), 'deleteThing')).not.toThrow()
    expect(() => unwrap(res(204), 'getThing')).toThrow(ApiError)
  })
})

describe('problemMessage', () => {
  it('prefers the per-occurrence detail', () => {
    // The concrete case: POST /schedules/ 409s with a real reason. Without this
    // the user sees "Failed to create schedule" and has no idea a name collided.
    expect(
      problemMessage(
        {
          type: 'https://canopy-web.dimagi.com/problems/conflict',
          title: 'Conflict',
          status: 409,
          detail: "A schedule named 'Weekly report' already exists for this agent.",
        },
        'Failed to create schedule',
      ),
    ).toBe("A schedule named 'Weekly report' already exists for this agent.")
  })

  it('falls back to title when detail is absent', () => {
    // `detail` is optional in the Problem model; `title` never is.
    expect(problemMessage({ title: 'Not found', status: 404 }, 'Failed to load')).toBe(
      'Not found',
    )
  })

  it('ignores an empty or whitespace detail', () => {
    expect(
      problemMessage({ title: 'Conflict', detail: '   ' }, 'Failed to create schedule'),
    ).toBe('Conflict')
  })

  it('falls back to the generic message when neither field is a string', () => {
    expect(problemMessage({ detail: 42, title: null }, 'Failed to run schedule')).toBe(
      'Failed to run schedule',
    )
  })

  it('falls back for a non-problem body', () => {
    // A proxy's HTML 502 or an opaque network failure never parses to a
    // problem+json object — it must not produce "[object Object]".
    expect(problemMessage('<html>502</html>', 'Failed to load schedules')).toBe(
      'Failed to load schedules',
    )
    expect(problemMessage(undefined, 'Failed to load schedules')).toBe(
      'Failed to load schedules',
    )
    expect(problemMessage(null, 'Failed to load schedules')).toBe('Failed to load schedules')
  })
})
