import { useEffect, useState } from 'react'

import { getTestVaultKey, setTestVaultKey, type TestVaultKeyOut } from '@/api/vault'
import { relativeAge } from '@/lib/relativeAge'

// An agent login's TEST vault key (apps/agents/credentials/login_test_key.py): off by
// default; while on, the agent's own canopy login may make one passphrase-sealed vault
// key, so a test harness signed in as the agent can read its own private
// conversations. The key opens nothing else. Turning it on asks for a passkey; turning
// it off deletes the key and every conversation copy it was given.

export function AgentTestVaultKeySection({ slug, canEdit }: { slug: string; canEdit: boolean }) {
  const [state, setState] = useState<TestVaultKeyOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getTestVaultKey(slug).then((s) => !cancelled && setState(s), () => !cancelled && setState(null))
    return () => {
      cancelled = true
    }
  }, [slug, canEdit])

  if (!canEdit || state === null) return null

  const toggle = async () => {
    const next = !state.enabled
    if (!next && state.has_key && !window.confirm(
      'Turn the test vault key off? Its key, and its copies of the private conversations it is in, are deleted.')) return
    setBusy(true)
    setError(null)
    try {
      setState(await setTestVaultKey(slug, next))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  const who = state.login_email || 'its canopy login'
  return (
    <section id="cred-test-vault-key" className="mb-6" data-testid="agent-test-vault-key">
      <label className="flex items-center gap-2 text-[12px] text-foreground">
        <input
          type="checkbox"
          checked={state.enabled}
          disabled={busy || !state.login_email}
          onChange={() => void toggle()}
          aria-label="Let this agent's own login hold a test vault key"
          data-testid="test-vault-key-enabled"
        />
        Let {who} hold a test vault key
      </label>
      <p className="mt-1 text-[11px] text-muted-foreground">
        For testing only. A harness signed in as this agent can then read the private conversations it is in
        itself — and nothing else: the key is never given any vault, cannot vouch for a machine, and is shown
        as an &ldquo;agent test key&rdquo; wherever keys are listed. It is made with a passphrase as that
        login. Turning this off deletes the key.
      </p>
      <p className="mt-1 text-[11px] text-muted-foreground" data-testid="test-vault-key-summary">
        {!state.login_email
          ? 'This agent has no canopy login of its own.'
          : state.has_key
            ? `Key ${state.key_jkt.slice(0, 12)}… made ${state.key_made_at ? relativeAge(state.key_made_at) : ''}` +
              ` · holds ${state.conversations} private conversation${state.conversations === 1 ? '' : 's'}`
            : state.enabled ? 'On · no key made yet' : 'Off'}
      </p>
      {error && <p className="mt-1 text-[12px] text-destructive">{error}</p>}
    </section>
  )
}
