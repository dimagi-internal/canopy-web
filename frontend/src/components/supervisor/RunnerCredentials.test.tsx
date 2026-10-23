// @vitest-environment jsdom
//
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { RunnerCredentialEntry } from '@/api/harness'
import {
  boxLoginAccount,
  credentialWarning,
  moved,
  probeFor,
  probeLabel,
  RunnerCredentials,
  type CredentialStatus,
} from './RunnerCredentials'

const api = vi.hoisted(() => ({
  getRunnerCredentialStatus: vi.fn(),
  addRunnerLogin: vi.fn(),
  addRunnerCredential: vi.fn(),
  updateRunnerCredential: vi.fn(),
  removeRunnerCredential: vi.fn(),
  orderRunnerCredentials: vi.fn(),
  getRunnerMint: vi.fn(),
  startRunnerMint: vi.fn(),
  submitRunnerMintCode: vi.fn(),
}))
vi.mock('@/api/harness', () => api)

function entry(over: Partial<RunnerCredentialEntry> & Pick<RunnerCredentialEntry, 'key' | 'kind'>): RunnerCredentialEntry {
  return { name: '', account: '', signed_in: false, has_value: false, reported_at: null, ...over }
}

function status(credentials: RunnerCredentialEntry[]): CredentialStatus {
  return {
    credentials,
    has_claude_token: false,
    has_claude_token_secondary: false,
    has_claude_api_key: false,
    claude_token_label: '',
    claude_token_secondary_label: '',
    logins: [],
    updated_at: null,
  }
}

const LOGIN = entry({ key: 'default', kind: 'login', signed_in: true, account: 'a@dimagi.com' })
const TOKEN = entry({ key: 'tt', kind: 'token', has_value: true, name: 'ace' })
const KEY = entry({ key: 'kk', kind: 'api_key', has_value: true, name: 'ZDR org' })
const MIXED = status([KEY, LOGIN, TOKEN])

beforeEach(() => {
  api.getRunnerMint.mockResolvedValue(null)
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('credentialWarning', () => {
  it('warns only when nothing on the list can run a turn', () => {
    expect(credentialWarning(status([]))).toMatch(/No working credential/)
    expect(credentialWarning(status([entry({ key: 'x', kind: 'login' })]))).toMatch(/No working credential/)
  })

  it('counts an API key — a box running on one key is not credential-less', () => {
    expect(credentialWarning(status([KEY]))).toBeNull()
  })

  it('counts a box the runner says is signed in before the list has it', () => {
    expect(credentialWarning(status([]), true)).toBeNull()
  })
})

describe('probe labels', () => {
  it('match the labels the box reports each entry under', () => {
    expect(probeLabel(LOGIN)).toBe('box-login')
    expect(probeLabel({ kind: 'login', key: 'ab12' })).toBe('box-login:ab12')
    expect(probeLabel(TOKEN)).toBe('token:tt')
    expect(probeLabel(KEY)).toBe('api-key:kk')
  })

  it('reads one probe out of the health checks', () => {
    const checks = { 'claude.probe.token:tt': { status: 'warn', detail: 'at its usage cap' } }
    expect(probeFor(checks, 'token:tt')).toEqual({ tone: 'warn', detail: 'at its usage cap' })
    expect(probeFor(checks, 'api-key:kk')).toEqual({ tone: null, detail: '' })
  })
})

describe('moved', () => {
  it('moves one key to a new place', () => {
    expect(moved(['a', 'b', 'c'], 'c', 0)).toEqual(['c', 'a', 'b'])
    expect(moved(['a', 'b', 'c'], 'a', 9)).toEqual(['b', 'c', 'a'])
  })
})

describe('boxLoginAccount', () => {
  it('reads the account out of the claude.login health check', () => {
    expect(boxLoginAccount('signed in on this box as ace@dimagi-ai.com — claude.ai connectors available'))
      .toBe('ace@dimagi-ai.com')
    expect(boxLoginAccount(undefined)).toBe('')
  })
})

describe('RunnerCredentials', () => {
  it('shows one list of every kind, in order, with no warning when one works', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(MIXED)
    render(<RunnerCredentials runnerId="r1" />)
    await screen.findByTestId('cred-list')
    expect(['kk', 'default', 'tt'].map((k) => screen.getByTestId(`cred-kind-${k}`).textContent))
      .toEqual(['API key', 'Login', 'Token'])
    expect(screen.getByTestId('cred-account-default').textContent).toBe('a@dimagi.com')
    expect(screen.getByTestId('cred-name-kk').textContent).toContain('ZDR org')
    expect(screen.queryByTestId('runner-credentials-warning')).toBeNull()
    expect(screen.queryByText(/last resort/i)).toBeNull()
  })

  it('moves any entry up or down across kinds', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(MIXED)
    api.orderRunnerCredentials.mockResolvedValue(MIXED)
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-down-kk'))
    await waitFor(() => expect(api.orderRunnerCredentials).toHaveBeenCalledWith('r1', ['default', 'kk', 'tt']))
    // The buttons are disabled while that save is in flight: a click then is ignored.
    await waitFor(() => expect((screen.getByTestId('cred-up-tt') as HTMLButtonElement).disabled).toBe(false))
    fireEvent.click(screen.getByTestId('cred-up-tt'))
    await waitFor(() => expect(api.orderRunnerCredentials).toHaveBeenLastCalledWith('r1', ['kk', 'tt', 'default']))
  })

  it('removes an entry after confirming, and shows why when the server refuses', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(MIXED)
    api.removeRunnerCredential.mockRejectedValue(new Error("That is this runner's last token or API key."))
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-remove-kk'))
    await waitFor(() => expect(api.removeRunnerCredential).toHaveBeenCalledWith('r1', 'kk'))
    expect((await screen.findByTestId('runner-credentials-error')).textContent).toMatch(/last token or API key/)
  })

  it('does not remove when the confirm is declined', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(MIXED)
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-remove-tt'))
    expect(api.removeRunnerCredential).not.toHaveBeenCalled()
  })

  it('adds an API key with a name, and clears the field once it lands', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(status([]))
    api.addRunnerCredential.mockResolvedValue(status([KEY]))
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-add-api_key'))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'ZDR org' } })
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: '  sk-ant-api-1 ' } })
    fireEvent.click(screen.getByTestId('cred-new-api-key-save'))
    await waitFor(() => expect(api.addRunnerCredential).toHaveBeenCalledWith('r1', 'api_key', 'sk-ant-api-1', 'ZDR org'))
    await waitFor(() => expect(screen.queryByTestId('cred-new-api-key')).toBeNull())
  })

  it('adds a login by starting its sign-in', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(status([]))
    api.addRunnerLogin.mockResolvedValue(status([entry({ key: 'n1', kind: 'login' })]))
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-add-login'))
    await waitFor(() => expect(api.addRunnerLogin).toHaveBeenCalledWith('r1'))
    expect((await screen.findByTestId('cred-account-n1')).textContent).toBe('not signed in yet')
  })

  it('adds a token by starting a token sign-in straight away', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(status([]))
    api.startRunnerMint.mockResolvedValue({ id: 'm', status: 'requested', slot: 'token', login_key: '' })
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-add-token'))
    await waitFor(() => expect(api.startRunnerMint).toHaveBeenCalledWith('r1', 'token', ''))
  })

  it('renames a token', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(MIXED)
    api.updateRunnerCredential.mockResolvedValue(MIXED)
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-name-tt'))
    const input = screen.getByTestId('cred-name-input-tt')
    fireEvent.change(input, { target: { value: 'jj' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(api.updateRunnerCredential).toHaveBeenCalledWith('r1', 'tt', { name: 'jj' }))
  })

  it('replaces an API key without showing the old one', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(MIXED)
    api.updateRunnerCredential.mockResolvedValue(MIXED)
    render(<RunnerCredentials runnerId="r1" />)
    fireEvent.click(await screen.findByTestId('cred-replace-kk'))
    fireEvent.change(screen.getByLabelText('New API key'), { target: { value: 'sk-ant-new' } })
    fireEvent.click(screen.getByText('Save'))
    await waitFor(() => expect(api.updateRunnerCredential).toHaveBeenCalledWith('r1', 'kk', { value: 'sk-ant-new' }))
  })

  it('offers only API keys on a sensitive-data runner', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(status([KEY]))
    render(<RunnerCredentials runnerId="r1" apiKeysOnly />)
    await screen.findByTestId('cred-add-api_key')
    expect(screen.queryByTestId('cred-add-login')).toBeNull()
    expect(screen.queryByTestId('cred-add-token')).toBeNull()
  })

  it('colours an entry by what the box last proved about it', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(MIXED)
    render(<RunnerCredentials runnerId="r1" healthChecks={{
      'claude.probe.token:tt': { status: 'warn', detail: 'at its usage cap — resets Monday' },
      'claude.probe.api-key:kk': { status: 'fail', detail: 'invalid key' },
    }} />)
    expect((await screen.findByTestId('cred-dot-tt')).className).toContain('text-warning')
    expect(screen.getByTestId('cred-probe-tt').textContent).toMatch(/resets Monday/)
    expect(screen.getByTestId('cred-dot-kk').className).toContain('text-destructive')
  })

  it('shows a box signed in before there was a list, as the account it reports', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(status([]))
    render(<RunnerCredentials runnerId="r1" signedIn signedInAs="ace@dimagi-ai.com" />)
    expect((await screen.findByTestId('cred-login-pending-adopt')).textContent).toContain('ace@dimagi-ai.com')
    expect(screen.queryByTestId('runner-credentials-warning')).toBeNull()
  })
})
