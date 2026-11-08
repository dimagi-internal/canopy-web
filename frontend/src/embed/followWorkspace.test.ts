import { describe, expect, it } from 'vitest'
import { mayReloadAgents, workspaceOfPage } from './followWorkspace'

describe('the panel follows the page into another workspace', () => {
  it('reads the workspace from the route the host pushes', () => {
    expect(workspaceOfPage({ path: '/w/marshellis/views/gmail-clean-up' })).toBe('marshellis')
    expect(workspaceOfPage({ path: '/ide/abc' })).toBe('')
    expect(workspaceOfPage({})).toBe('')
    expect(workspaceOfPage(null)).toBe('')
  })

  it('never swaps the list under a conversation', () => {
    expect(mayReloadAgents('ready')).toBe(true)
    expect(mayReloadAgents('choosing')).toBe(true)
    expect(mayReloadAgents('chatting')).toBe(false)
    expect(mayReloadAgents('private')).toBe(false)
  })
})
