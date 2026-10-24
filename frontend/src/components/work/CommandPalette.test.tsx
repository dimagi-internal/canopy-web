// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'

import type { ResolvedNavEntry } from '@/components/AppLayout/nav'

const listTasks = vi.fn()
const createTask = vi.fn()
vi.mock('@/api/work', async (orig) => ({
  ...(await orig<typeof import('@/api/work')>()),
  listTasks: (...a: unknown[]) => listTasks(...a),
  createTask: (...a: unknown[]) => createTask(...a),
}))
vi.mock('./useDirectory', () => ({
  useDirectory: () => ({
    people: [],
    agents: [{ slug: 'eva', name: 'Eva', workspace: 'connect' }],
    labels: [],
    projects: [{ id: 1, name: 'Kenya pilot', address: 'connect/kenya', handle: 'kenya', url: '/w/connect/projects/kenya' }],
  }),
}))

const { CommandPalette } = await import('./CommandPalette')
const { KeyboardHelp } = await import('./KeyboardHelp')

const NAV: ResolvedNavEntry[] = [
  { kind: 'link', item: { href: '/home', label: 'Home' } },
  { kind: 'menu', label: 'Work', items: [{ href: '/w/connect/tasks', label: 'Tasks' }, { href: '/w/connect/triage', label: 'Triage' }] },
]

function Where() {
  const l = useLocation()
  return <div data-testid="where">{l.pathname + l.search}</div>
}

function show() {
  render(
    <MemoryRouter initialEntries={['/w/connect/agents']}>
      <input aria-label="Some box" />
      <Routes>
        <Route path="*" element={<Where />} />
      </Routes>
      <CommandPalette workspace="connect" nav={NAV} />
      <KeyboardHelp />
    </MemoryRouter>,
  )
}

const openPalette = () => act(() => {
  fireEvent.keyDown(window, { key: 'k', metaKey: true })
})
const search = () => screen.getByRole('combobox', { name: 'Search or type a command' })
const options = () => screen.getAllByRole('option').map((o) => o.textContent ?? '')

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('CommandPalette', () => {
  it('opens on ⌘K (and Ctrl+K) as a modal dialog with a listbox, and Esc closes it', () => {
    show()
    expect(screen.queryByRole('dialog')).toBeNull()
    openPalette()
    const dialog = screen.getByRole('dialog', { name: 'Command palette' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    expect(screen.getByRole('listbox')).toBeTruthy()
    expect(document.activeElement).toBe(search())
    fireEvent.keyDown(search(), { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    act(() => {
      fireEvent.keyDown(window, { key: 'k', ctrlKey: true })
    })
    expect(screen.getByRole('dialog')).toBeTruthy()
  })

  it('filters pages, projects and agents, and Enter goes to the one picked', () => {
    show()
    openPalette()
    fireEvent.change(search(), { target: { value: 'tria' } })
    expect(options()[0]).toContain('Triage')
    fireEvent.keyDown(search(), { key: 'Enter' })
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/triage')
    expect(screen.queryByRole('dialog')).toBeNull()

    openPalette()
    fireEvent.change(search(), { target: { value: 'kenya' } })
    expect(options().some((o) => o.includes('Kenya pilot'))).toBe(true)
    expect(options().some((o) => o.includes('Triage'))).toBe(false)
    fireEvent.keyDown(search(), { key: 'Enter' })
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/projects/kenya')
  })

  it('arrow keys move the choice', () => {
    show()
    openPalette()
    fireEvent.change(search(), { target: { value: 'eva' } })
    const before = screen.getAllByRole('option').findIndex((o) => o.getAttribute('aria-selected') === 'true')
    fireEvent.keyDown(search(), { key: 'ArrowDown' })
    const after = screen.getAllByRole('option').findIndex((o) => o.getAttribute('aria-selected') === 'true')
    expect(after).toBe(Math.min(before + 1, screen.getAllByRole('option').length - 1))
  })

  it('a key-shaped query jumps straight to the task', () => {
    show()
    openPalette()
    fireEvent.change(search(), { target: { value: 'con-142' } })
    expect(options()[0]).toContain('Open CON-142')
    fireEvent.keyDown(search(), { key: 'Enter' })
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/tasks/CON-142')
    expect(listTasks).not.toHaveBeenCalled()
  })

  it('searches the workspace’s tasks as you type', async () => {
    listTasks.mockResolvedValue([{ key: 'CON-7', title: 'Fix the login page', status: 'todo', workspace: 'connect' }])
    show()
    openPalette()
    fireEvent.change(search(), { target: { value: 'login' } })
    await waitFor(() => expect(listTasks).toHaveBeenCalledWith({ q: 'login', limit: 20 }, 'connect'))
    await screen.findByText('Fix the login page')
    fireEvent.click(screen.getByText('Fix the login page'))
    expect(screen.getByTestId('where').textContent).toBe('/w/connect/tasks/CON-7')
  })

  it('"New task" creates one titled by the rest of the query and opens it', async () => {
    listTasks.mockResolvedValue([])
    createTask.mockResolvedValue({ key: 'CON-50', workspace: 'connect' })
    show()
    openPalette()
    fireEvent.change(search(), { target: { value: 'new task Write the brief' } })
    fireEvent.click(screen.getByText('New task “Write the brief”'))
    await waitFor(() => expect(createTask).toHaveBeenCalledWith({ title: 'Write the brief' }, 'connect'))
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/w/connect/tasks/CON-50'))
  })
})

describe('KeyboardHelp', () => {
  it('? lists every shortcut, and Esc closes it', () => {
    show()
    act(() => {
      fireEvent.keyDown(window, { key: '?', shiftKey: true })
    })
    const help = screen.getByRole('dialog', { name: 'Keyboard shortcuts' })
    for (const k of ['⌘', 'K', 'C', 'J', 'X', 'Space', 'Esc', 'A', 'S', 'B', 'D', 'H', 'M']) {
      expect(Array.from(help.querySelectorAll('kbd')).some((el) => el.textContent === k)).toBe(true)
    }
    act(() => {
      fireEvent.keyDown(window, { key: 'Escape' })
    })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('a single key does nothing while you type in a box', () => {
    show()
    const box = screen.getByLabelText('Some box')
    act(() => {
      fireEvent.keyDown(box, { key: '?', shiftKey: true })
    })
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})
