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
