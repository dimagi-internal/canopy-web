// @vitest-environment jsdom
// The panel's permission prompt: the agent's reason, then what the SITE says it covers.
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PageGrantConfirm } from './PageGrantConfirm'
import type { PageGrant, PageGrantInfo } from './pageGrant'

afterEach(cleanup)

const BASE: PageGrantInfo = {
  id: 7, site: 'connect-labs', agent: 'ace', agent_name: 'ACE', scope: 'marketplace:read reviews:read marketplace:review',
  granted_at: '2026-11-07T08:00:00Z', expires_at: '2026-11-07T08:30:00Z', confirmed: false, requested: true,
}

function pageGrant(info: PageGrantInfo): PageGrant {
  return {
    wanted: () => true,
    current: () => info,
    asking: () => true,
    subscribe: () => () => {},
    refresh: vi.fn(async () => {}),
    confirm: vi.fn(async () => {}),
    stop: vi.fn(async () => {}),
  } as unknown as PageGrant
}

describe('PageGrantConfirm', () => {
  it('names the site, lists what it says each scope lets the agent do, and keeps the raw scopes', () => {
    render(<PageGrantConfirm agentName="ACE" pageGrant={pageGrant({
      ...BASE, site_title: 'Connect Labs', page_title: 'A marketplace round', requested_reason: 'propose criteria',
      scope_labels: [
        { scope: 'marketplace:read', label: 'read the network’s organisations and rounds' },
        { scope: 'marketplace:review', label: 'write AI reviews, each confirmed on a card' },
      ],
    })} />)
    const text = screen.getByTestId('page-grant-confirm').textContent ?? ''
    expect(text).toContain('ACE needs your permission to interact with this page and data on Connect Labs')
    expect(screen.getByTestId('page-grant-reason').textContent).toBe('To propose criteria')
    const words = screen.getByTestId('page-grant-words').textContent ?? ''
    expect(words).toContain('For 30 minutes, Connect Labs says this lets it:')
    expect(words).toContain('read the network’s organisations and rounds')
    expect(words).toContain('write AI reviews, each confirmed on a card')
    expect(screen.getByTestId('page-grant-scopes').textContent)
      .toBe('A marketplace round · permissions: marketplace:read reviews:read marketplace:review')
  })

  it('reads a site that describes nothing as before: its name as words and the plain sentence', () => {
    render(<PageGrantConfirm agentName="ACE" pageGrant={pageGrant(BASE)} />)
    expect(screen.getByTestId('page-grant-confirm').textContent)
      .toContain('ACE needs your permission to interact with this page and data on Connect Labs')
    const words = screen.getByTestId('page-grant-words').textContent ?? ''
    expect(words).toContain('For 30 minutes it can see and do on Connect Labs only what you can yourself')
    expect(words).not.toContain('says this lets it')
    expect(screen.getByTestId('page-grant-scopes').textContent)
      .toBe('permissions: marketplace:read reviews:read marketplace:review')
  })
})
