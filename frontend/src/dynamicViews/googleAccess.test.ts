import { describe, expect, it } from 'vitest'
import { checkAsk } from './googleAccess'

describe('checkAsk', () => {
  it('lets a view ask only for an agent it declares tasks for', () => {
    expect(checkAsk({ action: 'start', agent: 'hal' }, ['bailey'])).toMatch(/declares no task for hal/)
  })

  it('defaults to an hour of Gmail and refuses longer', () => {
    expect(checkAsk({ action: 'start', agent: 'bailey', account: ' gina@gmail.com ' }, ['bailey']))
      .toEqual({ agent: 'bailey', account: 'gina@gmail.com', services: ['gmail'], minutes: 60, purpose: '' })
    expect(checkAsk({ action: 'start', agent: 'bailey', minutes: 90 }, ['bailey'])).toMatch(/1 to 60 minutes/)
  })

  it('refuses a service canopy does not lend', () => {
    expect(checkAsk({ action: 'start', agent: 'bailey', services: ['youtube'] }, ['bailey'])).toMatch(/unknown Google service/)
  })
})
