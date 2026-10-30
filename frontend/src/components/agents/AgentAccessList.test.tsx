// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccessLineOut, AccessListOut, AgentAccessOut } from '@/api/agents'

const getAccessList = vi.fn()
const getAgentAccess = vi.fn()
const addAccessLine = vi.fn()
const changeAccessLine = vi.fn()
const removeAccessLine = vi.fn()
const setInheritWorkspaceDefaults = vi.fn()
vi.mock('@/api/agents', async (orig) => ({
  ...(await orig<typeof import('@/api/agents')>()),
  getAccessList: (...a: unknown[]) => getAccessList(...a),
  getAgentAccess: (...a: unknown[]) => getAgentAccess(...a),
  addAccessLine: (...a: unknown[]) => addAccessLine(...a),
  changeAccessLine: (...a: unknown[]) => changeAccessLine(...a),
  removeAccessLine: (...a: unknown[]) => removeAccessLine(...a),
  setInheritWorkspaceDefaults: (...a: unknown[]) => setInheritWorkspaceDefaults(...a),
}))
vi.mock('@/workspace/WorkspaceProvider', () => ({
  useWorkspace: () => ({
    workspaces: [
      { slug: 'connect', display_name: 'Connect', role: 'admin' },
      { slug: 'dimagi', display_name: 'Dimagi', role: 'viewer' },
    ],
    active: 'connect',
    loading: false,
    refresh: vi.fn(),
  }),
}))

const { AgentAccessList } = await import('./AgentAccessList')

const line = (o: Partial<AccessLineOut>): AccessLineOut => ({
  id: 1, scope: 'agent', scope_workspace: null, kind: 'person', who: 'ann@dimagi.com', role: 'user', user_id: 9,
  subject_workspace: null, at_least: '', domain: '', verified: true, manual_only: false, note: '',
  granted_by_email: null, granted_at: null, can_edit: true, ...o,
})

const LIST = (o: Partial<AccessListOut> = {}): AccessListOut => ({
  uses_access_list: true,
  inherit_workspace_defaults: true,
  can_edit: true,
  lines: [
    line({ id: 1, who: 'ann@dimagi.com', role: 'user' }),
    line({ id: 2, kind: 'domain', who: 'Anyone at @partner.org (verified)', role: 'viewer', domain: 'partner.org' }),
    line({ id: 3, scope: 'workspace', scope_workspace: 'dimagi', kind: 'workspace', who: 'Everyone in dimagi', role: 'viewer', can_edit: false }),
  ],
  implicit: [
    { who: 'op@dimagi.com', role: 'owner', why: 'Owns this agent' },
    { who: 'boss@dimagi.com', role: 'admin', why: 'Owns the workspace' },
  ],
  roles: [
    { role: 'owner', can: ['ask', 'run the whole agent', 'transfer or delete it'] },
    { role: 'admin', can: ['ask', 'run the whole agent', 'configure it'] },
    { role: 'editor', can: ['ask', 'run the whole agent'] },
    { role: 'user', can: ['ask', 'use what it publishes'] },
    { role: 'viewer', can: ['ask'] },
  ],
  ...o,
})

const ROSTER = {
  members: [
    { user_id: 1, email: 'op@dimagi.com', name: 'Op', workspace_role: 'editor', agent_role: 'owner', basis: 'Owns this agent', granted_at: null, access: 'full', capabilities: [], full_rule: null, manual_only: false, may_request_auto: false, role: 'owner', source: 'owner', via: 'Owns this agent' },
    { user_id: 4, email: 'ed@dimagi.com', name: 'Ed', workspace_role: 'editor', agent_role: 'member', basis: 'Workspace member', granted_at: null, access: 'full', capabilities: [], full_rule: null, manual_only: true, may_request_auto: false, role: 'editor', source: 'workspace-editor', via: 'The role this workspace gives an editor' },
    { user_id: 6, email: 'no@partner.org', name: 'No', workspace_role: 'viewer', agent_role: 'member', basis: 'Workspace member', granted_at: null, access: 'none', capabilities: [], full_rule: null, manual_only: false, may_request_auto: false, role: null, source: '', via: '' },
  ],
  outsiders: [],
  interface_published: false,
  slack_enabled: false,
} as unknown as AgentAccessOut

function show() {
  return render(
    <MemoryRouter>
      <AgentAccessList agentSlug="ace" agentName="Ace" workspace="connect" canManageAdmins />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  getAccessList.mockResolvedValue(LIST())
  getAgentAccess.mockResolvedValue(ROSTER)
})
afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AgentAccessList', () => {
  it('says the roles in plain words, lowest first, from the server', async () => {
    show()
    const legend = await screen.findByTestId('access-legend')
    expect(legend.textContent).toBe(
      'Viewercan ask' + 'Usercan ask, use what it publishes' + 'Editorcan ask, run the whole agent' +
        'Agent admincan ask, run the whole agent, configure it' + 'Ownercan ask, run the whole agent, transfer or delete it',
    )
  })

  it('lists the owner and admins as read-only, its own lines as editable, and inherited ones as set elsewhere', async () => {
    show()
    const implicit = await screen.findAllByTestId('access-line-implicit')
    expect(implicit.map((r) => r.textContent)).toEqual([
      'op@dimagi.comOwnerOwns this agent · not a line, cannot be removed here',
      'boss@dimagi.comAgent adminOwns the workspace · not a line, cannot be removed here',
    ])
    const own = screen.getAllByTestId('access-line')
    expect(own).toHaveLength(2)
    expect((within(own[0]).getByLabelText('Role for ann@dimagi.com') as HTMLSelectElement).value).toBe('user')
    const inherited = screen.getByTestId('access-line-inherited')
    expect(inherited.textContent).toContain('Everyone in dimagi')
    expect(inherited.textContent).toContain('set on dimagi')
    expect(within(inherited).queryByRole('button')).toBeNull()
    expect(within(inherited).queryByRole('combobox')).toBeNull()
    expect(within(inherited).getByRole('link', { name: 'change there' }).getAttribute('href')).toBe('/w/dimagi/settings/members')
    // never the raw words
    expect(document.body.textContent).not.toMatch(/manual_only|subject_key/)
  })

  it('changes a role, flips the approval flag, and removes a line', async () => {
    changeAccessLine.mockResolvedValue(line({}))
    removeAccessLine.mockResolvedValue(undefined)
    show()
    const row = (await screen.findAllByTestId('access-line'))[0]
    fireEvent.change(within(row).getByLabelText('Role for ann@dimagi.com'), { target: { value: 'editor' } })
    await waitFor(() => expect(changeAccessLine).toHaveBeenCalledWith('ace', 1, { role: 'editor' }))
    fireEvent.click(within(row).getByLabelText('Needs approval before anything goes out for ann@dimagi.com'))
    await waitFor(() => expect(changeAccessLine).toHaveBeenCalledWith('ace', 1, { manual_only: true }))
    fireEvent.click(within(row).getByRole('button', { name: 'Remove ann@dimagi.com' }))
    await waitFor(() => expect(removeAccessLine).toHaveBeenCalledWith('ace', 1))
    await waitFor(() => expect(getAccessList.mock.calls.length).toBeGreaterThan(3))
  })

  it('does not let an address-only line be raised to editor', async () => {
    show()
    const row = (await screen.findAllByTestId('access-line'))[1]
    const opt = within(row).getByRole('option', { name: 'Editor' }) as HTMLOptionElement
    expect(opt.disabled).toBe(true)
  })

  it('adds a person by email', async () => {
    addAccessLine.mockResolvedValue(line({}))
    show()
    await screen.findByTestId('access-line-form')
    fireEvent.change(screen.getByLabelText('Their email'), { target: { value: 'new@dimagi.com' } })
    fireEvent.click(within(screen.getByTestId('access-line-form')).getByLabelText('Needs approval before anything goes out'))
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(addAccessLine).toHaveBeenCalledWith('ace', expect.objectContaining({
        kind: 'person', role: 'user', email: 'new@dimagi.com', manual_only: true,
      })),
    )
    await waitFor(() => expect((screen.getByLabelText('Their email') as HTMLInputElement).value).toBe(''))
  })

  it('adds everyone in a workspace at least a workspace role, at a domain', async () => {
    addAccessLine.mockResolvedValue(line({}))
    show()
    fireEvent.change(await screen.findByLabelText('Who'), { target: { value: 'workspace' } })
    fireEvent.change(screen.getByLabelText('Workspace'), { target: { value: 'dimagi' } })
    fireEvent.change(screen.getByLabelText('Which people'), { target: { value: 'editor' } })
    fireEvent.change(screen.getByLabelText('Only addresses at this domain'), { target: { value: 'dimagi.com' } })
    fireEvent.click(screen.getByRole('radio', { name: /editor/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(addAccessLine).toHaveBeenCalledWith('ace', expect.objectContaining({
        kind: 'workspace', workspace: 'dimagi', at_least: 'editor', domain: 'dimagi.com', role: 'editor',
      })),
    )
  })

  it('disables editor for a domain or a known visitor, and steps down from it', async () => {
    show()
    fireEvent.change(await screen.findByLabelText('Who'), { target: { value: 'workspace' } })
    fireEvent.click(screen.getByRole('radio', { name: /editor/i }))
    expect((screen.getByRole('radio', { name: /editor/i }) as HTMLInputElement).checked).toBe(true)
    for (const kind of ['domain', 'contact']) {
      fireEvent.change(screen.getByLabelText('Who'), { target: { value: kind } })
      expect((screen.getByRole('radio', { name: /editor/i }) as HTMLInputElement).disabled).toBe(true)
      expect((screen.getByRole('radio', { name: /user/i }) as HTMLInputElement).checked).toBe(true)
    }
  })

  it("shows the server's refusal next to the form", async () => {
    const { ApiError } = await import('@/api/problem')
    addAccessLine.mockRejectedValue(new ApiError(422, 'no such person among the workspaces you can see', undefined))
    show()
    fireEvent.change(await screen.findByLabelText('Their email'), { target: { value: 'nobody@x.org' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    const form = screen.getByTestId('access-line-form')
    expect((await within(form).findByRole('alert')).textContent).toBe('no such person among the workspaces you can see')
  })

  it('toggles taking the workspace defaults', async () => {
    setInheritWorkspaceDefaults.mockResolvedValue(LIST({ inherit_workspace_defaults: false }))
    show()
    const box = (await screen.findByLabelText(/Also take Connect.s default access/)) as HTMLInputElement
    expect(box.checked).toBe(true)
    fireEvent.click(box)
    await waitFor(() => expect(setInheritWorkspaceDefaults).toHaveBeenCalledWith('ace', false))
  })

  it('draws what each person gets now from the roster, in words', async () => {
    show()
    fireEvent.click(await screen.findByTestId('agent-access-show-all'))
    const rows = await screen.findAllByTestId('agent-access-row')
    expect(rows).toHaveLength(3)
    const ed = rows.find((r) => r.textContent?.includes('ed@dimagi.com')) as HTMLElement
    expect(ed.textContent).toContain('Editor')
    expect(ed.textContent).toContain('A workspace editor: every turn needs approval')
    const no = rows.find((r) => r.textContent?.includes('no@partner.org')) as HTMLElement
    expect(no.textContent).toContain('No access')
    expect(screen.queryAllByRole('combobox', { name: /Change role/ })).toHaveLength(0)
  })

  it('is read-only for someone who is not an agent admin', async () => {
    getAccessList.mockResolvedValue(LIST({ can_edit: false }))
    show()
    await screen.findAllByTestId('access-line')
    expect(screen.queryByTestId('access-line-form')).toBeNull()
    expect(screen.queryByLabelText('Role for ann@dimagi.com')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Remove ann@dimagi.com' })).toBeNull()
    expect((screen.getByLabelText(/Also take Connect/) as HTMLInputElement).disabled).toBe(true)
  })

  it('says when the agent still runs on its older published rules', async () => {
    getAccessList.mockResolvedValue(LIST({ uses_access_list: false }))
    show()
    expect((await screen.findByText(/has not moved onto this list yet/)).textContent).toContain('see Doors')
    expect(screen.getByRole('button', { name: 'See what moving would do' })).toBeTruthy()
  })
})
