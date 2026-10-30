import { describe, expect, it } from 'vitest'

import { keyFlowError, PrfUnsupportedError, USE_A_PASSPHRASE } from './prf'

describe('keyFlowError', () => {
  it('says to use a passphrase when the passkey cannot hold the key', () => {
    expect(keyFlowError(new PrfUnsupportedError(), 'x')).toBe(USE_A_PASSPHRASE)
  })
  it('says a dismissed passkey sheet was dismissed', () => {
    const e = new Error('The operation either timed out or was not allowed')
    e.name = 'NotAllowedError'
    expect(keyFlowError(e, 'x')).toBe('The passkey prompt was dismissed.')
  })
  it('otherwise gives the error’s own words, or the fallback', () => {
    expect(keyFlowError(new Error('boom'), 'x')).toBe('boom')
    expect(keyFlowError('nope', 'Something went wrong')).toBe('Something went wrong')
  })
})
