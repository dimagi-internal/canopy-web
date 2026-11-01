// @vitest-environment jsdom
import type { ComponentProps } from 'react'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ChatSessionMenu } from './ChatSessionMenu'

afterEach(() => {
  cleanup()
})

function baseProps(overrides: Partial<ComponentProps<typeof ChatSessionMenu>> = {}) {
  return {
    sessionId: 's1',
    myRole: 'owner' as string | null,
    onToggleNotify: vi.fn(),
    onShare: vi.fn(async () => {}),
    onReset: vi.fn(),
    resetting: false,
    onClose: vi.fn(),
    closing: false,
    ...overrides,
  }
}

async function openSub(testId: string) {
  fireEvent.click(screen.getByTestId(testId))
  await waitFor(() => expect(screen.getAllByRole('menuitemradio').length).toBeGreaterThan(0))
}

async function openMenu() {
  fireEvent.click(screen.getByTestId('chat-session-menu'))
  await waitFor(() => expect(screen.getByText('People…')).toBeTruthy())
}

describe('ChatSessionMenu — Others see while you type', () => {
  it('shows the radio group with the current mode checked', async () => {
    const onTypingVisibilityChange = vi.fn()
    render(
      <ChatSessionMenu
        {...baseProps({ typingVisibility: 'typing', onTypingVisibilityChange })}
      />,
    )
    await openMenu()
    expect(screen.getByText('Others see while you type')).toBeTruthy()
    await openSub('typing-visibility')
    expect(screen.getByRole('menuitemradio', { name: 'My text' }).getAttribute('aria-checked'))
      .toBe('false')
    expect(screen.getByRole('menuitemradio', { name: 'Typing…' }).getAttribute('aria-checked'))
      .toBe('true')
    expect(screen.getByRole('menuitemradio', { name: 'Nothing' }).getAttribute('aria-checked'))
      .toBe('false')
  })

  it('calls the handler when an item is chosen', async () => {
    const onTypingVisibilityChange = vi.fn()
    render(
      <ChatSessionMenu
        {...baseProps({ typingVisibility: 'live', onTypingVisibilityChange })}
      />,
    )
    await openMenu()
    await openSub('typing-visibility')
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Nothing' }))
    expect(onTypingVisibilityChange).toHaveBeenCalledWith('hidden')
  })

  it('is hidden when the props are absent (a viewer)', async () => {
    render(<ChatSessionMenu {...baseProps({ myRole: 'viewer' })} />)
    await openMenu()
    expect(screen.queryByText('Others see while you type')).toBeNull()
    expect(screen.queryByRole('menuitemradio')).toBeNull()
  })
})

describe('ChatSessionMenu — Load all messages / Load full session', () => {
  it('hides both when there is nothing to offer', async () => {
    render(<ChatSessionMenu {...baseProps()} />)
    await openMenu()
    expect(screen.queryByTestId('menu-load-all')).toBeNull()
    expect(screen.queryByTestId('menu-load-full')).toBeNull()
  })

  it('shows Load all messages and calls through on click', async () => {
    const onLoadAll = vi.fn()
    render(<ChatSessionMenu {...baseProps({ showLoadAll: true, onLoadAll })} />)
    await openMenu()
    fireEvent.click(screen.getByTestId('menu-load-all'))
    expect(onLoadAll).toHaveBeenCalledTimes(1)
  })

  it('disables Load all messages and relabels it while loading', async () => {
    render(<ChatSessionMenu {...baseProps({ showLoadAll: true, loadingAll: true })} />)
    await openMenu()
    const item = screen.getByTestId('menu-load-all')
    expect(item.textContent).toBe('Loading…')
    expect(item.getAttribute('data-disabled')).not.toBeNull()
  })

  it('shows Load full session and calls through on click', async () => {
    const onLoadFull = vi.fn()
    render(<ChatSessionMenu {...baseProps({ showLoadFull: true, onLoadFull })} />)
    await openMenu()
    fireEvent.click(screen.getByTestId('menu-load-full'))
    expect(onLoadFull).toHaveBeenCalledTimes(1)
  })

  it('can show both at once, above Close', async () => {
    render(
      <ChatSessionMenu {...baseProps({ showLoadAll: true, showLoadFull: true })} />,
    )
    await openMenu()
    expect(screen.getByTestId('menu-load-all')).toBeTruthy()
    expect(screen.getByTestId('menu-load-full')).toBeTruthy()
  })
})

describe('ChatSessionMenu — Slack thread', () => {
  it('offers Disconnect from Slack on a connected session', async () => {
    const onDetachSlack = vi.fn()
    render(
      <ChatSessionMenu
        {...baseProps({ slackThread: { channel: 'C1', url: 'https://app.slack.com/x' }, onDetachSlack })}
      />,
    )
    await openMenu()
    fireEvent.click(screen.getByTestId('detach-slack'))
    expect(onDetachSlack).toHaveBeenCalled()
    expect(screen.queryByText('Open Slack thread')).toBeNull() // menu closed after the click
  })

  it('opens the thread in the Slack desktop app, with the browser as a fallback', async () => {
    const assign = vi.fn()
    vi.stubGlobal('location', { ...window.location, assign })
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const app_url = 'slack://channel?team=T1&id=C1&message=1.2&thread_ts=1.2'
    render(<ChatSessionMenu {...baseProps({ slackThread: { channel: 'C1', url: 'https://app.slack.com/x', app_url } })} />)
    await openMenu()
    fireEvent.click(screen.getByTestId('slack-thread-app-link'))
    expect(assign).toHaveBeenCalledWith(app_url)
    expect(open).not.toHaveBeenCalled()
    await openMenu()
    fireEvent.click(screen.getByText('Open Slack thread in the browser'))
    expect(open).toHaveBeenCalledWith('https://app.slack.com/x', '_blank', 'noopener,noreferrer')
    vi.unstubAllGlobals()
    open.mockRestore()
  })

  it('shows nothing about Slack when the session has no thread', async () => {
    render(<ChatSessionMenu {...baseProps({ slackThread: null, onDetachSlack: vi.fn() })} />)
    await openMenu()
    expect(screen.queryByTestId('detach-slack')).toBeNull()
  })

  it('hides the action from someone who cannot act in the session', async () => {
    render(<ChatSessionMenu {...baseProps({ slackThread: { channel: 'C1' } })} />)
    await openMenu()
    expect(screen.queryByTestId('detach-slack')).toBeNull()
  })
})

describe('ChatSessionMenu — Move to workspace', () => {
  const targets = [
    { slug: 'dimagi', display_name: 'Dimagi' },
    { slug: 'marshellis', display_name: 'Marshellis' },
  ]

  it('is hidden when the session is not yours to move', async () => {
    render(<ChatSessionMenu {...baseProps({ workspace: 'dimagi', moveTargets: targets })} />)
    await openMenu()
    expect(screen.queryByTestId('move-workspace')).toBeNull()
  })

  it('is hidden when there is nowhere else to put it', async () => {
    render(
      <ChatSessionMenu
        {...baseProps({ workspace: 'dimagi', moveTargets: [targets[0]], onMove: vi.fn() })}
      />,
    )
    await openMenu()
    expect(screen.queryByTestId('move-workspace')).toBeNull()
  })

  it('moves to the picked workspace', async () => {
    const onMove = vi.fn(async () => {})
    render(
      <ChatSessionMenu {...baseProps({ workspace: 'dimagi', moveTargets: targets, onMove })} />,
    )
    await openMenu()
    fireEvent.click(screen.getByTestId('move-workspace'))
    await waitFor(() => expect(screen.getByTestId('move-workspace-form')).toBeTruthy())
    // The current workspace is not offered as a target.
    const options = Array.from(
      (screen.getByTestId('move-workspace-select') as HTMLSelectElement).options,
    ).map((o) => o.value)
    expect(options).toEqual(['marshellis'])
    fireEvent.click(screen.getByRole('button', { name: 'Move' }))
    await waitFor(() => expect(onMove).toHaveBeenCalledWith('marshellis'))
  })
})

describe('ChatSessionMenu — Memory', () => {
  const memory = {
    state: {
      session_id: 's1',
      agent: { slug: 'ada', name: 'Ada' },
      record: { available: true, default: true, override: null, effective: true, granted: false, grant: null },
      use: { available: true, default: true, override: null, effective: true, granted: true, grant: null },
    },
    editable: true,
    features: ['record', 'use'],
    busy: null,
    failed: false,
    toggle: vi.fn(async () => {}),
    keep: vi.fn(async () => {}),
  } as unknown as NonNullable<ComponentProps<typeof ChatSessionMenu>['memory']>

  it('is an item with its state as dots, opening the switches in a dialog', async () => {
    render(<ChatSessionMenu {...baseProps({ memory })} />)
    await openMenu()
    // Ungranted reads as off (red); granted and on as on (green). No "?".
    const item = screen.getByRole('menuitem', { name: 'Memory… Learn off, Use on' })
    fireEvent.click(item)
    await waitFor(() => expect(screen.getByText('Your memory in this conversation')).toBeTruthy())
    fireEvent.click(screen.getByRole('switch', { name: 'Learn from me in this conversation' }))
    expect(memory.toggle).toHaveBeenCalledWith('record')
  })

  it('is absent when there is no memory to show', async () => {
    render(<ChatSessionMenu {...baseProps({ memory: null })} />)
    await openMenu()
    expect(screen.queryByTestId('session-memory')).toBeNull()
  })
})

describe('ChatSessionMenu — Copy join link', () => {
  it('copies the workspace chat address and says so', async () => {
    const writeText = vi.fn(async () => {})
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    render(<ChatSessionMenu {...baseProps({ workspace: 'dimagi' })} />)
    await openMenu()
    fireEvent.click(screen.getByTestId('copy-join-link'))
    await waitFor(() => expect(screen.getByText('Join link copied')).toBeTruthy())
    expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/w/dimagi/sessions/s1`)
  })

  it('is hidden until the session knows its workspace', async () => {
    render(<ChatSessionMenu {...baseProps()} />)
    await openMenu()
    expect(screen.queryByTestId('copy-join-link')).toBeNull()
  })
})

describe('ChatSessionMenu — Tool calls', () => {
  it('expands or collapses every tool call from the menu', async () => {
    const onToolRowsChange = vi.fn()
    render(<ChatSessionMenu {...baseProps({ toolRows: 'default', onToolRowsChange })} />)
    await openMenu()
    await openSub('tool-rows')
    expect(screen.getByRole('menuitemradio', { name: 'Per row (default)' }).getAttribute('aria-checked'))
      .toBe('true')
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Expand all' }))
    expect(onToolRowsChange).toHaveBeenCalledWith('all')
  })

  it('is hidden when the host offers no choice', async () => {
    render(<ChatSessionMenu {...baseProps()} />)
    await openMenu()
    expect(screen.queryByText('Tool calls')).toBeNull()
  })
})
