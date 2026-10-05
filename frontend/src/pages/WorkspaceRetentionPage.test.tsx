// @vitest-environment jsdom
//
// Retention as a workspace setting: everyone sees the policy, only a manager
// gets the controls, and the page says plainly whether anything is deleted yet.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import type { RetentionOut, RetentionPreviewOut } from '@/api/workspaces'

const { getRetention, saveRetentionRule, previewRetention, deleteRetentionRule, listAgents } = vi.hoisted(() => ({
  getRetention: vi.fn<(slug: string) => Promise<RetentionOut>>(),
  saveRetentionRule: vi.fn(),
  previewRetention: vi.fn<(slug: string) => Promise<RetentionPreviewOut>>(),
  deleteRetentionRule: vi.fn(),
  listAgents: vi.fn(),
}))

vi.mock('@/api/workspaces', async (orig) => ({
  ...(await orig<typeof import('@/api/workspaces')>()),
  getRetention,
  saveRetentionRule,
  previewRetention,
  deleteRetentionRule,
}))
vi.mock('@/api/agents', () => ({ listAgents }))

const { WorkspaceRetentionPage } = await import('./WorkspaceRetentionPage')

const choices = {
  kinds: [{ value: 'chat', label: 'Chat' }, { value: 'turn', label: 'Turn' }],
  sources: [{ value: 'email', label: 'Email' }],
  principals: [{ value: 'contact', label: 'Contact' }],
}

const rule = {
  id: 7, workspace: 'connect', kind: 'chat', source: '', principal: 'contact', agent: '',
  keep_days: 7, note: 'not ours to keep', summary: 'Chats started by contacts: 7 days',
  created_by: 'admin@dimagi.com', updated_at: '2026-10-05T00:00:00Z',
}

function policy(over: Partial<RetentionOut> = {}): RetentionOut {
  return {
    workspace: 'connect', enforced: false, can_manage: true, rules: [rule],
    inherited: [{ ...rule, id: 1, workspace: '', summary: 'Turns: 90 days' }], choices, ...over,
  }
}

function show() {
  listAgents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 0 })
  return render(
    <MemoryRouter initialEntries={['/w/connect/settings/retention']}>
      <Routes>
        <Route path="/w/:workspace/settings/retention" element={<WorkspaceRetentionPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('WorkspaceRetentionPage', () => {
  it('shows the rules, the inherited ones, and that nothing is deleted yet', async () => {
    getRetention.mockResolvedValue(policy())
    show()
    await waitFor(() => expect(screen.getByTestId('retention-rules').textContent)
      .toContain('Chats started by contacts: 7 days'))
    expect(screen.getByTestId('retention-inherited').textContent).toContain('deployment')
    expect(screen.getByTestId('retention-enforced').textContent).toContain('nothing is deleted')
  })

  it('gives a member who cannot manage no controls', async () => {
    getRetention.mockResolvedValue(policy({ can_manage: false }))
    show()
    await waitFor(() => screen.getByTestId('retention-rules'))
    expect(screen.queryByTestId('retention-form')).toBeNull()
    expect(screen.queryByTestId('retention-preview')).toBeNull()
    expect(screen.queryByTestId('retention-delete-7')).toBeNull()
  })

  it('adds a keep-forever rule', async () => {
    getRetention.mockResolvedValue(policy({ rules: [] }))
    saveRetentionRule.mockResolvedValue(rule)
    show()
    await waitFor(() => screen.getByTestId('retention-form'))
    fireEvent.change(screen.getByLabelText('Kind'), { target: { value: 'turn' } })
    fireEvent.click(screen.getByTestId('retention-forever'))
    fireEvent.click(screen.getByTestId('retention-save'))
    await waitFor(() => expect(saveRetentionRule).toHaveBeenCalledWith(
      'connect', expect.objectContaining({ kind: 'turn', keep_days: null }), undefined))
  })

  it('previews counts per rule', async () => {
    getRetention.mockResolvedValue(policy())
    previewRetention.mockResolvedValue({
      workspace: 'connect', enforced: false, totals: { chat_messages: 12 },
      by_rule: [{ rule_id: 7, summary: rule.summary, counts: { chat_messages: 12, chats_touched: 2 } }],
    })
    show()
    await waitFor(() => screen.getByTestId('retention-preview-run'))
    fireEvent.click(screen.getByTestId('retention-preview-run'))
    await waitFor(() => expect(screen.getByTestId('retention-preview-table').textContent)
      .toContain('12 chat messages'))
  })
})
