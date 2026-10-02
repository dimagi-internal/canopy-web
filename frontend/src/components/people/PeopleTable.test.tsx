// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AddPersonForm, PeopleTable, type PersonRow } from './PeopleTable'
import { roleOptions } from './roles'

afterEach(cleanup)

const OPTIONS = roleOptions(['viewer', 'editor'])

function row(o: Partial<PersonRow>): PersonRow {
  return { key: 1, name: 'a@x.org', role: 'viewer', options: OPTIONS, editable: false, ...o }
}

describe('PeopleTable', () => {
  it('renders an editable role as a dropdown and a fixed one as text with its reason', () => {
    render(
      <PeopleTable
        rows={[
          row({ key: 1, name: 'a@x.org', editable: true, onRoleChange: vi.fn() }),
          row({ key: 2, name: 'b@x.org', role: 'owner', roleLabel: 'Owner', why: 'owns the workspace' }),
        ]}
      />,
    )
    const select = screen.getByLabelText('Change role for a@x.org') as HTMLSelectElement
    expect(select.tagName).toBe('SELECT')
    expect(select.className).toContain('bg-input')
    expect(screen.queryByLabelText('Change role for b@x.org')).toBeNull()
    expect(screen.getByText('Owner')).toBeTruthy()
    expect(screen.getByText('owns the workspace')).toBeTruthy()
  })

  it('wires a disabled dropdown to its visible reason', () => {
    render(<PeopleTable rows={[row({ editable: true, roleDisabled: true, why: 'Only owner', onRoleChange: vi.fn() })]} />)
    const select = screen.getByLabelText('Change role for a@x.org') as HTMLSelectElement
    expect(select.disabled).toBe(true)
    expect(document.getElementById(select.getAttribute('aria-describedby') as string)?.textContent).toBe('Only owner')
  })

  it('saves on change, disables while saving, and shows a failure inline', async () => {
    let reject: (e: Error) => void = () => {}
    const onRoleChange = vi.fn(() => new Promise((_, r) => { reject = r }))
    render(<PeopleTable rows={[row({ editable: true, onRoleChange })]} />)
    const select = screen.getByLabelText('Change role for a@x.org') as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'editor' } })
    expect(onRoleChange).toHaveBeenCalledWith('editor')
    await waitFor(() => expect(select.disabled).toBe(true))
    reject(new Error('nope'))
    expect(await screen.findByText('nope')).toBeTruthy()
    expect(select.disabled).toBe(false)
  })

  it('offers Remove only on rows that have it', () => {
    const onRemove = vi.fn(() => Promise.resolve())
    render(<PeopleTable rows={[row({ key: 1, name: 'a@x.org', onRemove }), row({ key: 2, name: 'b@x.org' })]} />)
    fireEvent.click(screen.getByLabelText('Remove a@x.org'))
    expect(onRemove).toHaveBeenCalled()
    expect(screen.queryByLabelText('Remove b@x.org')).toBeNull()
  })
})

describe('AddPersonForm', () => {
  it('submits email + chosen role and clears on success', async () => {
    const onAdd = vi.fn(() => Promise.resolve())
    render(<AddPersonForm options={OPTIONS} defaultRole="editor" onAdd={onAdd} />)
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'c@x.org' } })
    fireEvent.change(screen.getByLabelText('Role'), { target: { value: 'viewer' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(onAdd).toHaveBeenCalledWith('c@x.org', 'viewer'))
    await waitFor(() => expect((screen.getByLabelText('Email') as HTMLInputElement).value).toBe(''))
  })

  it('shows a single role as text, not a one-option dropdown', () => {
    render(<AddPersonForm options={[{ value: 'admin', label: 'Admin' }]} onAdd={vi.fn()} />)
    expect(screen.queryByRole('combobox')).toBeNull()
    expect(screen.getByText('Admin')).toBeTruthy()
  })
})
