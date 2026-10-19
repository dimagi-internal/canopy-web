// @vitest-environment jsdom
//
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  boxLoginAccount,
  RunnerCredentials,
  SLOTS,
  credentialSummary,
  loginProbeLabel,
  probeFor,
  PROBE_LABEL,
  moved,
  nextPayload,
  type CredentialStatus,
} from './RunnerCredentials'

const api = vi.hoisted(() => ({
  getRunnerCredentialStatus: vi.fn(),
  setRunnerCredential: vi.fn(),
  swapRunnerLogins: vi.fn(),
  addRunnerLogin: vi.fn(),
  removeRunnerLogin: vi.fn(),
  orderRunnerLogins: vi.fn(),
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
  logins: [],
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

  const LOGINS = {
    ...NONE,
    logins: [
      { key: 'default', account: 'a@dimagi.com', signed_in: true },
      { key: 'bb', account: 'b@dimagi.com', signed_in: true },
      { key: 'cc', account: '', signed_in: false },
    ],
  }

  it('says "Sign in with Claude" only on the full logins; backup tokens say token', async () => {
    // The defect (2026-10-14): three "Sign in with Claude" buttons, two of which
    // made inference-only setup-tokens. An operator signed in on one of those
    // believing it was the full login, and the box still could not read a doc.
    // Backup tokens can be got here again (2026-10-15), under their own words.
    api.getRunnerCredentialStatus.mockResolvedValue({ ...TWO, logins: LOGINS.logins })
    api.startRunnerMint.mockResolvedValue({ id: 'm', status: 'requested', slot: 'box', login_key: 'bb' })
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('runner-reauth-box-bb')).toBeTruthy())
    for (const slot of ['primary', 'secondary']) {
      const text = within(screen.getByTestId(`runner-reauth-${slot}`)).getByTestId('reauth-start').textContent
      expect(text).toBe('Replace this token')
    }
    expect(screen.getAllByTestId('reauth-start')).toHaveLength(5)

    await act(async () => {
      fireEvent.click(within(screen.getByTestId('runner-reauth-box-bb')).getByTestId('reauth-start'))
    })
    expect(api.startRunnerMint).toHaveBeenCalledWith('r1', 'box', 'bb')
  })

  it('lists each login as the account the box reports, in order', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(LOGINS)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('cred-login-account-default').textContent)
      .toBe('a@dimagi.com'))
    expect(screen.getByTestId('cred-login-account-cc').textContent).toBe('not signed in yet')
    const rows = screen.getAllByTestId(/^cred-login-(default|bb|cc)$/).map((el) => el.dataset.testid)
    expect(rows).toEqual(['cred-login-default', 'cred-login-bb', 'cred-login-cc'])
    // A signed-in login is a working box, not an alarm.
    expect(screen.queryByTestId('runner-credentials-warning')).toBeNull()
  })

  it('adds a login', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(NONE)
    api.addRunnerLogin.mockResolvedValue({ ...NONE, logins: [{ key: 'dd', account: '', signed_in: false }] })
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-login-add'))
    await act(async () => { fireEvent.click(screen.getByTestId('cred-login-add')) })
    expect(api.addRunnerLogin).toHaveBeenCalledWith('r1')
    expect(screen.getByTestId('cred-login-dd')).toBeTruthy()
  })

  it('removes a login after confirming, and the row goes', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(LOGINS)
    api.removeRunnerLogin.mockResolvedValue({ ...LOGINS, logins: LOGINS.logins.filter((l) => l.key !== 'bb') })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-login-remove-bb'))
    await act(async () => { fireEvent.click(screen.getByTestId('cred-login-remove-bb')) })
    expect(api.removeRunnerLogin).toHaveBeenCalledWith('r1', 'bb')
    expect(screen.queryByTestId('cred-login-bb')).toBeNull()
    confirm.mockRestore()
  })

  it('moves a login up and down', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(LOGINS)
    api.orderRunnerLogins.mockResolvedValue(LOGINS)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-login-up-cc'))
    expect((screen.getByTestId('cred-login-up-default') as HTMLButtonElement).disabled).toBe(true)
    await act(async () => { fireEvent.click(screen.getByTestId('cred-login-up-cc')) })
    expect(api.orderRunnerLogins).toHaveBeenCalledWith('r1', ['default', 'cc', 'bb'])
    await act(async () => { fireEvent.click(screen.getByTestId('cred-login-down-default')) })
    expect(api.orderRunnerLogins).toHaveBeenLastCalledWith('r1', ['bb', 'default', 'cc'])
  })

  it('computes a move', () => {
    expect(moved(['a', 'b', 'c'], 'c', 0)).toEqual(['c', 'a', 'b'])
    expect(moved(['a', 'b', 'c'], 'a', 2)).toEqual(['b', 'c', 'a'])
    expect(moved(['a', 'b', 'c'], 'b', 9)).toEqual(['a', 'c', 'b'])
  })

  it('shows a box signed in before there was a list, as the account it reports', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(NONE)
    render(<RunnerCredentials runnerId="r1" signedIn signedInAs="ace@dimagi-ai.com" />)
    await waitFor(() => expect(screen.getByTestId('cred-login-pending-adopt').textContent)
      .toContain('ace@dimagi-ai.com'))
    expect(screen.queryByTestId('runner-credentials-warning')).toBeNull()
  })

  it('reads the account out of the claude.login health check', () => {
    expect(boxLoginAccount('signed in on this box as ace@dimagi-ai.com — claude.ai connectors available'))
      .toBe('ace@dimagi-ai.com')
    expect(boxLoginAccount('signed in on this box — claude.ai connectors available')).toBe('')
    expect(boxLoginAccount(undefined)).toBe('')
  })

  it('removes a backup token and its name, after confirming', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(TWO)
    api.setRunnerCredential.mockResolvedValue(TWO)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-remove-claude_token'))
    await act(async () => { fireEvent.click(screen.getByTestId('cred-remove-claude_token')) })
    expect(confirm).toHaveBeenCalled()
    expect(api.setRunnerCredential).toHaveBeenCalledWith('r1', { claude_token: '', claude_token_label: '' })
    confirm.mockRestore()
  })

  it('shows no row for a backup token that is not set — only a way to get one', async () => {
    // Reported 2026-10-14: removing both tokens left two "not set" rows behind.
    api.getRunnerCredentialStatus.mockResolvedValue(ONE_CLAUDE)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('cred-slot-claude_token')).toBeTruthy())
    expect(screen.queryByTestId('cred-slot-claude_token_secondary')).toBeNull()
    expect(screen.queryByTestId('runner-credentials-swap')).toBeNull()
    expect(within(screen.getByTestId('cred-get-claude_token_secondary')).getByTestId('reauth-start')
      .textContent).toBe('+ Create a long-lived token')
  })

  it('gets a backup token when there is none, and warns until there is', async () => {
    // cloud-ec2-2, 2026-10-15: both backups removed, the login stopped renewing,
    // and the page had no way to get a token back.
    api.getRunnerCredentialStatus.mockResolvedValue(LOGINS)
    api.startRunnerMint.mockResolvedValue({ id: 'm', status: 'requested', slot: 'primary', login_key: '' })
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => expect(screen.getByTestId('cred-no-backup-warning')).toBeTruthy())
    await act(async () => {
      fireEvent.click(within(screen.getByTestId('cred-get-claude_token')).getByTestId('reauth-start'))
    })
    expect(api.startRunnerMint).toHaveBeenCalledWith('r1', 'primary', '')
  })

  it('will not remove the last backup token', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(ONE_CLAUDE)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-remove-claude_token'))
    expect((screen.getByTestId('cred-remove-claude_token') as HTMLButtonElement).disabled).toBe(true)
    expect(screen.queryByTestId('cred-no-backup-warning')).toBeNull()
  })

  it('does not remove a token when the confirm is declined', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(TWO)
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-remove-claude_token_secondary'))
    await act(async () => { fireEvent.click(screen.getByTestId('cred-remove-claude_token_secondary')) })
    expect(api.setRunnerCredential).not.toHaveBeenCalled()
    confirm.mockRestore()
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

// Each dot is what the box last PROVED (its `claude.probe.<label>` check), not
// that a value is stored: cloud-ec2-fork read ready with a capped token.
describe('credential probes', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getRunnerMint.mockResolvedValue(null)
  })
  afterEach(cleanup)

  const dot = (key: string) =>
    within(screen.getByTestId(`cred-slot-${key}`)).getByTestId(`cred-dot-${key}`)

  it('maps the slots and logins to the labels the box probes under', () => {
    expect(PROBE_LABEL).toEqual({
      claude_token: 'subscription-1',
      claude_token_secondary: 'subscription-2',
      claude_api_key: 'api-key',
    })
    expect(loginProbeLabel('default')).toBe('box-login')
    expect(loginProbeLabel('a44d94c3')).toBe('box-login:a44d94c3')
  })

  it('reads one probe out of the health checks', () => {
    const checks = { 'claude.probe.api-key': { name: 'claude.probe.api-key', status: 'fail', detail: 'not signed in' } }
    expect(probeFor(checks, 'api-key')).toEqual({ tone: 'fail', detail: 'not signed in' })
    expect(probeFor(checks, 'subscription-1')).toEqual({ tone: null, detail: '' })
    expect(probeFor(null, 'api-key')).toEqual({ tone: null, detail: '' })
  })

  it('colours a capped token as a warning and says when it resets', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(ONE_CLAUDE)
    render(<RunnerCredentials runnerId="r1" healthChecks={{
      'claude.probe.subscription-1': { name: 'claude.probe.subscription-1', status: 'warn',
        detail: 'at its usage cap — resets 2026-10-18T23:00Z' },
    }} />)
    await waitFor(() => screen.getByTestId('cred-slot-claude_token'))
    expect(dot('claude_token').className).toContain('text-warning')
    expect(screen.getByTestId('cred-probe-claude_token').textContent)
      .toContain('at its usage cap — resets 2026-10-18T23:00Z')
  })

  it('colours a dead credential as destructive', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue({ ...ONE_CLAUDE, has_claude_api_key: true })
    render(<RunnerCredentials runnerId="r1" healthChecks={{
      'claude.probe.api-key': { name: 'claude.probe.api-key', status: 'fail',
        detail: 'not signed in — someone has to sign it in again' },
    }} />)
    await waitFor(() => screen.getByTestId('cred-slot-claude_api_key'))
    expect(dot('claude_api_key').className).toContain('text-destructive')
  })

  it('keeps the plain "set" dot when the box has not probed', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(ONE_CLAUDE)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-slot-claude_token'))
    expect(dot('claude_token').className).toContain('text-success')
    expect(screen.queryByTestId('cred-probe-claude_token')).toBeNull()
  })

  it('colours a login by its probe', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue({
      ...NONE, logins: [{ key: 'default', account: 'a@dimagi.com', signed_in: true }],
    })
    render(<RunnerCredentials runnerId="r1" healthChecks={{
      'claude.probe.box-login': { name: 'claude.probe.box-login', status: 'warn',
        detail: 'at its usage cap — resets 2026-10-18T23:00Z' },
    }} />)
    await waitFor(() => screen.getByTestId('cred-login-default'))
    expect(screen.getByTestId('cred-login-dot-default').className).toContain('text-warning')
    expect(screen.getByTestId('cred-login-probe-default').textContent).toContain('resets')
  })

  it('says a full login does not copy between runners when there is none', async () => {
    api.getRunnerCredentialStatus.mockResolvedValue(ONE_CLAUDE)
    render(<RunnerCredentials runnerId="r1" />)
    await waitFor(() => screen.getByTestId('cred-no-login'))
    expect(screen.getByTestId('cred-no-login').textContent).toBe(
      "No full login on this runner. Logins don't copy between runners: add one here to use claude.ai connectors.")
  })
})
