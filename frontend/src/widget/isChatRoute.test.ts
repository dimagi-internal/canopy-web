import { describe, expect, it } from 'vitest'

import { isChatRoute } from './CanopyWidget'

// The launcher sat on top of the chat page's Send button. Every chat route
// missed here puts it back there.
describe('isChatRoute', () => {
  it.each([
    '/w/connect/chat',
    '/w/connect/chat/',
    '/w/connect/chat/251708cb-9190-43eb-b058-e810c411afb4',
  ])('hides the launcher on %s', (path) => {
    expect(isChatRoute(path)).toBe(true)
  })

  it.each([
    '/w/connect/agents/hal/history',
    '/w/connect',
    '/insights',
    '/w/connect/chatbots', // a prefix is not a match
    '/supervisor',
  ])('keeps it on %s', (path) => {
    expect(isChatRoute(path)).toBe(false)
  })
})
