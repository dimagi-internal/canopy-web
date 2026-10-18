// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import type { RunnerOut } from '@/api/harness'
import type { ChatSession } from '@/api/chat'
import { SessionContextMenu } from './SessionContextMenu'

const runner = (id: string, over: Partial<RunnerOut> = {}) =>
  ({ id, name: id, host: 'h', status: 'online', capabilities: { sessions: true }, flags: [], ...over }) as RunnerOut

const session = (over: Partial<ChatSession> = {}) =>
  ({
    id: 's1',
    title: 'fix glob',
    status: 'active',
    running: false,
    waiting_on_you: false,
    runner_name: 'cloud',
    runner_online: true,
    runner_status: 'online',
    runner_requirements: [],
    ...over,
  }) as ChatSession

afterEach(cleanup)

function open(s: ChatSession, runners: RunnerOut[] = []) {
  const onAction = vi.fn()
  render(<SessionContextMenu session={s} x={10} y={10} runners={runners} onAction={onAction} onDismiss={() => {}} />)
  return onAction
}

describe('SessionContextMenu', () => {
  it('transfers to an eligible runner other than the current one', () => {
    const onAction = open(session(), [runner('cloud'), runner('laptop'), runner('off', { status: 'offline' })])
    fireEvent.click(screen.getByTestId('ide-menu-transfer'))
    expect(screen.queryByTestId('ide-menu-transfer-cloud')).toBeNull()
    expect(screen.queryByTestId('ide-menu-transfer-off')).toBeNull()
    fireEvent.click(screen.getByTestId('ide-menu-transfer-laptop'))
    expect(onAction).toHaveBeenCalledWith({ kind: 'transfer', runner: expect.objectContaining({ id: 'laptop' }) })
  })

  it('cannot transfer an unbound session, or stop an idle one', () => {
    open(session({ runner_name: null }), [runner('laptop')])
    expect((screen.getByTestId('ide-menu-transfer') as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByTestId('ide-menu-stop') as HTMLButtonElement).disabled).toBe(true)
  })

  it('blocks delete while its runner is offline, and offers unarchive for an archived row', () => {
    open(session({ runner_online: false, runner_status: 'offline' }))
    expect((screen.getByTestId('ide-menu-close') as HTMLButtonElement).disabled).toBe(true)
    cleanup()
    open(session({ status: 'archived' }))
    expect(screen.getByTestId('ide-menu-unarchive')).toBeTruthy()
  })

  it('opens one session in Claude desktop, from a Claude CLI runner', () => {
    const key = '40cca779-4c36-4b93-8007-69dc2478d51b'
    const onAction = open(session({ runner_id: 'box', session_key: key }), [runner('box', { engine: 'claude-cli' })])
    fireEvent.click(screen.getByTestId('ide-menu-open-desktop'))
    expect(onAction).toHaveBeenCalledWith({ kind: 'open-desktop', href: `claude://resume?session=${key}` })
  })

  it('refuses while a Claude CLI turn is running, and is absent for an emdash runner', () => {
    const key = '40cca779-4c36-4b93-8007-69dc2478d51b'
    open(session({ runner_id: 'box', session_key: key, running: true }), [runner('box', { engine: 'claude-cli' })])
    expect((screen.getByTestId('ide-menu-open-desktop') as HTMLButtonElement).disabled).toBe(true)
    cleanup()
    open(session({ runner_id: 'box', session_key: key }), [runner('box', { engine: 'emdash' })])
    expect(screen.queryByTestId('ide-menu-open-desktop')).toBeNull()
  })
})

