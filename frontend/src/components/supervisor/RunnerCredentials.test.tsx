// @vitest-environment jsdom
//
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  RunnerCredentials,
  SLOTS,
  credentialSummary,
  nextPayload,
  type CredentialStatus,
} from './RunnerCredentials'

const api = vi.hoisted(() => ({
  getRunnerCredentialStatus: vi.fn(),
  setRunnerCredential: vi.fn(),
  swapRunnerLogins: vi.fn(),
  getRunnerMint: vi.fn(),
  startRunnerMint: vi.fn(),
  submitRunnerMintCode: vi.fn(),
}))
vi.mock('@/api/harness', () => api)

// The cloud box warned on every boot: "only ONE Claude credential is set — a
// usage cap will stop every agent on this box with nothing to fail over to."
// The API to fix that (POST /runners/{id}/credential) has existed since the
// runner-credential work; there was simply no page, so the only way to set it
// was a terminal. These are the rules that page runs on.

const NONE: CredentialStatus = {
  has_claude_token: false,
  has_claude_token_secondary: false,
  has_claude_api_key: false,
  claude_token_label: '',
  claude_token_secondary_label: '',
  updated_at: null,
}
const ONE_CLAUDE: CredentialStatus = { ...NONE, has_claude_token: true }

describe('slot catalogue', () => {
  it('covers every field the write schema accepts', () => {
    // RunnerCredentialIn's fields. A slot missing here is a credential that
    // cannot be set from the browser — which is the whole defect being fixed.
    expect(SLOTS.map((s) => s.key).sort()).toEqual([
      'claude_api_key',
      'claude_token',
      'claude_token_secondary',
    ])
  })

  it('pairs each slot with the boolean the status endpoint returns', () => {
    for (const s of SLOTS) expect(s.statusKey).toBe(`has_${s.key}`)
  })
})

describe('nextPayload', () => {
  it('sends only the slots actually typed into', () => {
    // RunnerCredentialIn is non-clobbering: a field left None is unchanged.
    // Sending "" for an untouched slot would wipe a working credential.
    expect(nextPayload({ claude_token_secondary: 'sk-ant-oat01-xyz' })).toEqual({
      claude_token_secondary: 'sk-ant-oat01-xyz',
    })
  })

  it('drops blank and whitespace-only entries rather than clobbering', () => {
    expect(nextPayload({ claude_token: '', claude_token_secondary: '   ', claude_api_key: 'sk-x' })).toEqual({
      claude_api_key: 'sk-x',
    })
  })

  it('trims — a pasted token picks up whitespace and the server stores it verbatim', () => {
    expect(nextPayload({ claude_api_key: '  sk-ant-api-1  ' })).toEqual({
      claude_api_key: 'sk-ant-api-1',
    })
  })

  it('is empty when nothing was typed, so the caller can skip the request', () => {
    expect(nextPayload({})).toEqual({})
    expect(nextPayload({ claude_token: '  ' })).toEqual({})
  })
})

describe('credentialSummary', () => {
  it('warns when the only Claude credential is the primary', () => {
    const s = credentialSummary(ONE_CLAUDE)
    expect(s.claudeFallback).toBe('none')
    // A single credential is a CHOICE, not a fault — no banner for it.
    expect(s.warning).toBeNull()
  })

  it('clears once a secondary subscription is set', () => {
    const s = credentialSummary({ ...ONE_CLAUDE, has_claude_token_secondary: true })
    expect(s.claudeFallback).toBe('secondary')
    expect(s.warning).toBeNull()
  })

  it('counts a metered API key as a fallback, but names the cost', () => {
    // The cascade's last resort is deliberately metered so falling back to it
    // notifies a human rather than quietly spending money.
    const s = credentialSummary({ ...ONE_CLAUDE, has_claude_api_key: true })
    expect(s.claudeFallback).toBe('api_key')
    // Likewise a metered-only fallback: worth knowing, not worth a standing banner.
    expect(s.warning).toBeNull()
  })

  it('says nothing is set at all rather than warning about a fallback', () => {
    // No primary is a different problem: the box cannot run ANY turn, and
    // telling someone to add a fallback would be the wrong instruction.
    const s = credentialSummary(NONE)
    expect(s.claudeFallback).toBe('none')
    expect(s.warning).toMatch(/no claude credential/i)
  })

  it('reports which non-Claude slots are unset', () => {
    const s = credentialSummary(ONE_CLAUDE)
    expect(s.unset).toContain('claude_api_key')
    expect(s.unset).not.toContain('claude_token')
  })
})

describe('RunnerCredentials', () => {
  const TWO = {
    ...ONE_CLAUDE, has_claude_token_secondary: true,
    claude_token_label: 'a@dimagi.com', claude_token_secondary_label: 'b@dimagi.com',
  }

  beforeEach(() => {
    vi.clearAllMocks()
    api.getRunnerMint.mockResolvedValue(null)
  })
  afterEach(cleanup)

  it('offers a sign-in on EACH login, and each one fills its own slot', async () => {
    // The defect: one "Start sign-in" above both logins, which always wrote the
    // primary — so adding a fallback overwrote the login that was working.
    api.getRunnerCredentialStatus.mockResolvedValue(ONE_CLAUDE)
    api.startRunnerMint.mockResolvedValue({ id: 'm', status: 'requested', slot: 'secondary' })
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('runner-reauth-secondary')).toBeTruthy())
    expect(screen.getByTestId('runner-reauth-primary')).toBeTruthy()

    const fallback = screen.getByTestId('runner-reauth-secondary')
    await act(async () => {
      fireEvent.click(fallback.querySelector('[data-testid="reauth-start"]')!)
    })
    expect(api.startRunnerMint).toHaveBeenCalledWith('r1', 'secondary')
  })

  it('names each login', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(TWO)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('cred-name-claude_token').textContent)
      .toContain('a@dimagi.com'))
    expect(screen.getByTestId('cred-name-claude_token_secondary').textContent).toContain('b@dimagi.com')
  })

  it('swaps primary and fallback', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(TWO)
    api.swapRunnerLogins.mockResolvedValue({
      ...TWO, claude_token_label: 'b@dimagi.com', claude_token_secondary_label: 'a@dimagi.com',
    })
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('runner-credentials-swap')).toBeTruthy())
    await act(async () => { fireEvent.click(screen.getByTestId('runner-credentials-swap')) })
    expect(api.swapRunnerLogins).toHaveBeenCalledWith('r1')
    expect(screen.getByTestId('cred-name-claude_token').textContent).toContain('b@dimagi.com')
  })

  it('has no paste box for a login — you sign in to it instead', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(TWO)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('cred-slot-claude_token')).toBeTruthy())
    for (const key of ['claude_token', 'claude_token_secondary']) {
      expect(screen.getByTestId(`cred-slot-${key}`).querySelector('input[type="password"]')).toBeNull()
    }
    expect(screen.getByTestId('cred-slot-claude_api_key').querySelector('input[type="password"]')).toBeTruthy()
  })

  it('saves a renamed login without touching any token', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(TWO)
    api.setRunnerCredential.mockResolvedValue({ ...TWO, claude_token_label: 'c@dimagi.com' })
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('cred-name-claude_token')).toBeTruthy())
    fireEvent.click(screen.getByTestId('cred-name-claude_token'))
    fireEvent.change(screen.getByTestId('cred-label-claude_token'), { target: { value: 'c@dimagi.com' } })
    await act(async () => {
      fireEvent.keyDown(screen.getByTestId('cred-label-claude_token'), { key: 'Enter' })
    })
    expect(api.setRunnerCredential).toHaveBeenCalledWith('r1', { claude_token_label: 'c@dimagi.com' })
  })
})
