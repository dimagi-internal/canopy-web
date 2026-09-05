import { describe, expect, it } from 'vitest'
import {
  SLOTS,
  credentialSummary,
  nextPayload,
  type CredentialStatus,
} from './RunnerCredentials'

// The cloud box warned on every boot: "only ONE Claude credential is set — a
// usage cap will stop every agent on this box with nothing to fail over to."
// The API to fix that (POST /runners/{id}/credential) has existed since the
// runner-credential work; there was simply no page, so the only way to set it
// was a terminal. These are the rules that page runs on.

const NONE: CredentialStatus = {
  has_claude_token: false,
  has_claude_token_secondary: false,
  has_claude_api_key: false,
  has_github_token: false,
  has_op_sa_token: false,
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
      'github_token',
      'op_sa_token',
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
    expect(nextPayload({ claude_token: '', github_token: '   ', op_sa_token: 'ops_x' })).toEqual({
      op_sa_token: 'ops_x',
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
    expect(s.warning).toMatch(/cap/i)
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
    expect(s.warning).toMatch(/metered|billed|spend/i)
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
    expect(s.unset).toContain('github_token')
    expect(s.unset).toContain('op_sa_token')
    expect(s.unset).not.toContain('claude_token')
  })
})
