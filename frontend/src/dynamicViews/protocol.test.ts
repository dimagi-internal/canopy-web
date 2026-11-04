import { describe, expect, it } from 'vitest'
import { frameRejection } from './protocol'

describe('frameRejection', () => {
  it('a failed answer becomes an Error with the sentence, the code and the link', () => {
    const e = frameRejection({ channel: 'canopy-view', type: 'read-result', id: 1, ok: false, error: 'Connect Salesforce in Settings → Connections.', code: 'not_connected', connect_url: 'https://c/start/' } as never)
    expect(e).toBeInstanceOf(Error)
    expect(e.message).toBe('Connect Salesforce in Settings → Connections.')
    expect(e).toMatchObject({ error: 'not_connected', connect_url: 'https://c/start/' })
  })

  it('adds nothing when there is nothing to add', () => {
    const e = frameRejection({ channel: 'canopy-view', type: 'read-result', id: 1, ok: false, error: 'No.' } as never)
    expect(e.message).toBe('No.')
    expect(Object.keys(e)).toEqual([])
  })
})
