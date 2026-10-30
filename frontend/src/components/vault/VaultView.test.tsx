// @vitest-environment jsdom
//
// The one vault frame (VaultView, VaultSection): a summary line, then sections that start
// closed unless something needs the reader or the address points at them.
import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

import { VaultView, type VaultFacts } from './VaultView'

const FACTS: VaultFacts = { secrets: 7, people: 3, machines: 2 }
const part = (name: string, over: Record<string, unknown> = {}) => ({ summary: `${name} summary`, body: <p>{name} body</p>, ...over })

function view(facts = FACTS, over: Partial<Parameters<typeof VaultView>[0]> = {}, at = '/') {
  return render(
    <MemoryRouter initialEntries={[at]}>
      <VaultView facts={facts} secrets={part('secrets')} access={part('access')} storage={part('storage')} {...over} />
    </MemoryRouter>,
  )
}

afterEach(cleanup)

describe('VaultView', () => {
  it('opens with one line saying what is in the vault and who can open it', () => {
    view()
    expect(screen.getByTestId('vault-summary').textContent)
      .toBe('Canopy vault · canopy-web can’t read it · 7 secrets · 3 people · 2 machines')
  })

  it('says what needs the reader, in the summary', () => {
    view({ ...FACTS, secrets: 1, people: 1, machines: 1, attention: ['1 declared secret with no value'] })
    expect(screen.getByTestId('vault-summary').textContent).toContain('1 secret · 1 person · 1 machine')
    expect(screen.getByTestId('vault-attention').textContent).toContain('1 declared secret with no value')
  })

  it('starts every section closed, with a one-line summary of what is inside', () => {
    view()
    for (const id of ['secrets', 'access-to-secrets', 'vault-storage']) {
      expect(screen.getByTestId(`vault-section-${id}`).getAttribute('aria-expanded')).toBe('false')
    }
    expect(screen.getByTestId('vault-section-secrets-summary').textContent).toBe('secrets summary')
    expect(screen.getByText('access body').closest('[hidden]')).toBeTruthy()
  })

  it('opens and closes a section from its heading', () => {
    view()
    fireEvent.click(screen.getByTestId('vault-section-access-to-secrets'))
    expect(screen.getByText('access body').closest('[hidden]')).toBeNull()
    fireEvent.click(screen.getByTestId('vault-section-access-to-secrets'))
    expect(screen.getByText('access body').closest('[hidden]')).toBeTruthy()
  })

  it('opens a section that needs the reader, until they close it', () => {
    view(FACTS, { storage: part('storage', { tone: 'attention' }) })
    const button = screen.getByTestId('vault-section-vault-storage')
    expect(button.getAttribute('aria-expanded')).toBe('true')
    fireEvent.click(button)
    expect(button.getAttribute('aria-expanded')).toBe('false')
  })

  it('opens the section an old address points at, by its id or an alias', () => {
    view(FACTS, { storage: part('storage', { anchors: ['cred-zk'] }) }, '/settings/security#cred-zk')
    expect(screen.getByTestId('vault-section-vault-storage').getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByTestId('vault-section-secrets').getAttribute('aria-expanded')).toBe('false')
    cleanup()
    view(FACTS, {}, '/x#access-to-secrets')
    expect(screen.getByTestId('vault-section-access-to-secrets').getAttribute('aria-expanded')).toBe('true')
  })
})
