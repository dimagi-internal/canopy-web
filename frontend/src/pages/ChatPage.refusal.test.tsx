// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Before you type: the composer reads the Turn Context preview, and when it says you could not take
 * a turn it shows why and how to fix it above the input, in the server's words. A preview that could
 * not be read refuses nothing: the server still decides on send.
 */

let panelProps: { banner?: ReactNode; disabledReason?: string } = {}
vi.mock('canopy-ui/chat', async (orig) => {
  const real = await orig<typeof import('canopy-ui/chat')>()
  return {
    ...real,
    ChatPanel: (props: { banner?: ReactNode; disabledReason?: string }) => {
      panelProps = props
      return <div data-testid="chat-panel">{props.banner}<span data-testid="disabled">{props.disabledReason ?? ''}</span></div>
    },
    useSessionSocket: () => ({
      state: { messages: [], activity: 'idle', menu: null, participants: [], presence_user_ids: [],
               current_user_id: 1, active_draft: null },
      connected: true, synced: true, awaitingReply: false, sendChat: vi.fn(), stopChat: vi.fn(),
      unqueueChat: vi.fn(), updateDraft: vi.fn(), takeOverDraft: vi.fn(), discardDraft: vi.fn(),
      prependMessages: vi.fn(), noteLocalSend: vi.fn(), typingVisibility: 'live', setTypingVisibility: vi.fn(),
    }),
  }
})

const SESSION = { id: 's1', sensitive: false, my_role: 'owner', agent_slug: 'ada', runner_name: 'box', title: 'T',
                  workspace: 'w1', messages: [] }
const REFUSAL = {
  code: 'not_on_access_list', why: "you are not on ada's access list",
  how_to_fix: 'Have jj@dimagi.com or an admin of ada add you to its access list as read only (to put questions to it).',
  who_can_fix: 'jj@dimagi.com or an admin of ada',
}
const getSession = vi.fn()
const getSessionContext = vi.fn()
vi.mock('@/api/chat', async (orig) => {
  const real = await orig<typeof import('@/api/chat')>()
  return {
    ...real,
    getSession: (...a: unknown[]) => getSession(...a),
    getSessionContext: (...a: unknown[]) => getSessionContext(...a),
    listSlashCommands: async () => [],
    attachSession: async () => undefined,
    detachSession: async () => undefined,
  }
})

const { ChatPage } = await import('./ChatPage')
const { COMPOSER_REFUSED } = await import('./chat/usePreviewRefusal')
const { forgetAll, rememberListed } = await import('./chat/sessionCache')

beforeEach(() => {
  panelProps = {}
  rememberListed([{ id: 's1', sensitive: false }])
  getSession.mockResolvedValue(SESSION)
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, json: async () => [] }) as unknown as Response))
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
  forgetAll()
})

const page = () => render(
  <MemoryRouter initialEntries={['/ide/s1']}>
    <Routes><Route path="/ide/:id" element={<ChatPage compact />} /></Routes>
  </MemoryRouter>,
)

describe('the composer before you type', () => {
  it('refused: says why, how to fix it and who can, above the input, and holds the send', async () => {
    getSessionContext.mockResolvedValue({ session_context: {}, withheld: '',
      turn_context: { access: 'refused', refusal: REFUSAL } })
    page()
    const notice = await screen.findByTestId('composer-refusal')
    expect(notice.textContent).toContain("You can't send a message here")
    expect(screen.getByTestId('composer-refusal-why').textContent).toBe("You are not on ada's access list.")
    expect(screen.getByTestId('composer-refusal-fix').textContent).toBe(REFUSAL.how_to_fix)
    expect(notice.textContent).toContain('jj@dimagi.com or an admin of ada')
    expect(notice.getAttribute('data-code')).toBe('not_on_access_list')
    expect(panelProps.disabledReason).toBe(COMPOSER_REFUSED)
    // The conversation is still drawn: only the composer is held.
    expect(screen.getByTestId('chat-panel')).toBeTruthy()
  })

  it('allowed: no notice, and the composer is open', async () => {
    getSessionContext.mockResolvedValue({ session_context: {}, withheld: '',
      turn_context: { access: 'confined', refusal: null } })
    page()
    await waitFor(() => expect(getSessionContext).toHaveBeenCalledWith('s1'))
    expect(screen.queryByTestId('composer-refusal')).toBeNull()
    expect(panelProps.disabledReason).toBeUndefined()
  })

  it('a preview that cannot be read, or is withheld, refuses nothing', async () => {
    getSessionContext.mockRejectedValue(new Error('500'))
    page()
    await waitFor(() => expect(getSessionContext).toHaveBeenCalled())
    expect(screen.queryByTestId('composer-refusal')).toBeNull()
    expect(panelProps.disabledReason).toBeUndefined()
    cleanup()
    getSessionContext.mockReset()
    getSessionContext.mockResolvedValue({ session_context: {}, turn_context: null, withheld: 'private' })
    page()
    await waitFor(() => expect(getSessionContext).toHaveBeenCalled())
    expect(screen.queryByTestId('composer-refusal')).toBeNull()
  })
})
