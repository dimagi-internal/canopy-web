// @vitest-environment jsdom
//
// Changing a key is the job: find it, Change, paste (or load a file), Save — in the row.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import { VaultEditor, type VaultEntry } from './VaultEditor'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const ENTRIES: VaultEntry[] = [
  { name: 'OCS_API_TOKEN', set: true, updatedAt: '2026-10-22T03:37:00Z', updatedBy: 'jj@dimagi.com' },
  { name: 'ACE_HQ_PASSWORD', set: true },
  { name: 'NOVA_API_KEY', set: false, flag: { label: 'no value', tone: 'warning' } },
]

function show(props: Partial<Parameters<typeof VaultEditor>[0]> = {}) {
  const onSave = vi.fn(async () => undefined)
  render(<VaultEditor entries={ENTRIES} canEdit onSave={onSave} {...props} />)
  return { onSave }
}

const names = () => screen.getAllByTestId(/^vault-row-/).map((r) => r.getAttribute('data-testid')!.slice('vault-row-'.length))

describe('VaultEditor', () => {
  it('changes a value in its row, and drops the draft once it lands', async () => {
    const { onSave } = show()
    fireEvent.click(screen.getByRole('button', { name: 'Change ACE_HQ_PASSWORD' }))
    const input = screen.getByLabelText('New value for ACE_HQ_PASSWORD') as HTMLTextAreaElement
    // Plain text you can see, on as many lines as it needs.
    expect(input.tagName).toBe('TEXTAREA')
    fireEvent.change(input, { target: { value: '  hunter2  ' } })
    fireEvent.click(screen.getByTestId('vault-save-ACE_HQ_PASSWORD'))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('ACE_HQ_PASSWORD', 'hunter2'))
    await waitFor(() => expect(screen.queryByLabelText('New value for ACE_HQ_PASSWORD')).toBeNull())
    // Opening it again starts empty: nothing of the old value is kept.
    fireEvent.click(screen.getByRole('button', { name: 'Change ACE_HQ_PASSWORD' }))
    expect((screen.getByLabelText('New value for ACE_HQ_PASSWORD') as HTMLInputElement).value).toBe('')
  })

  it('keeps the row open and says why when the save is refused', async () => {
    show({ onSave: vi.fn(async () => { throw new Error('step up first') }) })
    fireEvent.click(screen.getByRole('button', { name: 'Change OCS_API_TOKEN' }))
    fireEvent.change(screen.getByLabelText('New value for OCS_API_TOKEN'), { target: { value: 'x' } })
    fireEvent.click(screen.getByTestId('vault-save-OCS_API_TOKEN'))
    expect((await screen.findByTestId('vault-error')).textContent).toBe('step up first')
    expect(screen.getByLabelText('New value for OCS_API_TOKEN')).toBeTruthy()
  })

  it('keeps the line breaks of a multi-line value such as a PEM', async () => {
    const { onSave } = show()
    const pem = '-----BEGIN TEST VALUE-----\nMIIE\nabcd\n-----END TEST VALUE-----'
    fireEvent.click(screen.getByRole('button', { name: 'Set NOVA_API_KEY' }))
    fireEvent.change(screen.getByLabelText('New value for NOVA_API_KEY'), { target: { value: `${pem}\n` } })
    fireEvent.click(screen.getByTestId('vault-save-NOVA_API_KEY'))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('NOVA_API_KEY', pem))
  })

  it('loads a file into a new secret, and stores it only on Save', async () => {
    const { onSave } = show()
    fireEvent.click(screen.getByTestId('vault-add'))
    fireEvent.change(screen.getByLabelText('New secret name'), { target: { value: 'BAILEY_GH_APP_KEY' } })
    const pem = '-----BEGIN TEST VALUE-----\nMIIE\n-----END TEST VALUE-----'
    fireEvent.change(screen.getByTestId('upload-new'), { target: { files: [new File([`${pem}\n`], 'app.pem')] } })
    // The file's picker event is no click, so it only fills the field — the
    // passkey check starts from Save.
    await waitFor(() => expect((screen.getByLabelText('New secret value') as HTMLTextAreaElement).value).toBe(pem))
    expect(onSave).not.toHaveBeenCalled()
    fireEvent.click(within(screen.getByTestId('vault-add-row')).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('BAILEY_GH_APP_KEY', pem))
  })

  it('drops an error once its row is closed', async () => {
    show({ onSave: vi.fn(async () => { throw new Error('passkey check did not finish') }) })
    fireEvent.click(screen.getByTestId('vault-add'))
    fireEvent.change(screen.getByLabelText('New secret name'), { target: { value: 'X' } })
    fireEvent.change(screen.getByLabelText('New secret value'), { target: { value: 'v' } })
    fireEvent.click(within(screen.getByTestId('vault-add-row')).getByRole('button', { name: 'Save' }))
    await screen.findByTestId('vault-error')
    fireEvent.click(within(screen.getByTestId('vault-add-row')).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByTestId('vault-error')).toBeNull()
  })

  it('adds a new secret, refusing a name that is not one', async () => {
    const { onSave } = show()
    fireEvent.click(screen.getByTestId('vault-add'))
    fireEvent.change(screen.getByLabelText('New secret name'), { target: { value: '1BAD' } })
    fireEvent.change(screen.getByLabelText('New secret value'), { target: { value: 'v' } })
    fireEvent.click(within(screen.getByTestId('vault-add-row')).getByRole('button', { name: 'Save' }))
    expect(screen.getByTestId('vault-error').textContent).toMatch(/letters, digits/)
    expect(onSave).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('New secret name'), { target: { value: 'ELEVENLABS_API_KEY' } })
    fireEvent.click(within(screen.getByTestId('vault-add-row')).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSave).toHaveBeenCalledWith('ELEVENLABS_API_KEY', 'v'))
  })

  it('puts what needs you first, then the rest by name', () => {
    show()
    expect(names()).toEqual(['NOVA_API_KEY', 'ACE_HQ_PASSWORD', 'OCS_API_TOKEN'])
    // An unset one is Set, not Change, and has nothing to reveal.
    expect(screen.getByRole('button', { name: 'Set NOVA_API_KEY' })).toBeTruthy()
  })

  it('finds a key by name', () => {
    show()
    fireEvent.change(screen.getByLabelText('Search secrets'), { target: { value: 'hq' } })
    expect(names()).toEqual(['ACE_HQ_PASSWORD'])
  })

  it('reveals one value, and drops it after a while', async () => {
    const onReveal = vi.fn(async () => 'the-plaintext')
    show({ onReveal, revealMs: 50 })
    expect(screen.queryByTestId('reveal-NOVA_API_KEY')).toBeNull()
    fireEvent.click(screen.getByTestId('reveal-OCS_API_TOKEN'))
    await waitFor(() => expect(screen.getByTestId('revealed-OCS_API_TOKEN').textContent).toContain('the-plaintext'))
    await waitFor(() => expect(screen.queryByTestId('revealed-OCS_API_TOKEN')).toBeNull())
  })

  it('deletes only after asking', async () => {
    const onDelete = vi.fn(async () => undefined)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValueOnce(false).mockReturnValueOnce(true)
    show({ onDelete })
    const del = screen.getByRole('button', { name: 'Delete the stored value for OCS_API_TOKEN' })
    fireEvent.click(del)
    expect(onDelete).not.toHaveBeenCalled()
    fireEvent.click(del)
    await waitFor(() => expect(onDelete).toHaveBeenCalledWith('OCS_API_TOKEN'))
    expect(confirm).toHaveBeenCalledTimes(2)
  })

  it('offers no changes to someone who cannot make them', () => {
    show({ canEdit: false, onReveal: vi.fn(), onDelete: vi.fn() })
    expect(screen.queryByTestId('vault-add')).toBeNull()
    expect(screen.queryByRole('button', { name: /^(Change|Set) / })).toBeNull()
    expect(screen.queryByTestId('reveal-OCS_API_TOKEN')).toBeNull()
  })
})
