import { useEffect, useState } from 'react'

import { getTestPagePermission, setTestPagePermission, type TestPagePermissionOut } from '@/api/vault'
import { LoadFailed } from '@/components/vault/LoadFailed'

// An agent login's TEST page permissions (apps/agents/credentials/login_test_page.py): off
// by default; while on, the agent's own canopy login may confirm its OWN act-as-you
// permission on a host site's page without a passkey, so a headless test can run the
// page-agent flow as the agent itself. Never a person's permission. Turning it on asks
// for a passkey; turning it off stops every page permission the login holds.

export function AgentTestPagePermissionSection({ slug, canEdit }: { slug: string; canEdit: boolean }) {
  const [state, setState] = useState<TestPagePermissionOut | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    if (!canEdit) return
    getTestPagePermission(slug).then((s) => !cancelled && setState(s), () => !cancelled && setFailed(true))
    return () => {
      cancelled = true
    }
  }, [slug, canEdit])

  if (!canEdit) return null
  if (state === null) return failed ? <LoadFailed what="the test page permissions" /> : null

  const toggle = async () => {
    const next = !state.enabled
    if (!next && state.live_grants > 0 && !window.confirm(
      'Turn test page permissions off? The page permissions its login holds now are stopped.')) return
    setBusy(true)
    setError(null)
    try {
      setState(await setTestPagePermission(slug, next))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed')
    } finally {
      setBusy(false)
    }
  }

  const who = state.login_email || 'its canopy login'
  return (
    <section id="cred-test-page-permission" className="mb-6" data-testid="agent-test-page-permission">
      <label className="flex items-center gap-2 text-[12px] text-foreground">
        <input
          type="checkbox"
          checked={state.enabled}
          disabled={busy || !state.login_email}
          onChange={() => void toggle()}
          aria-label="Let this agent's own login confirm its own page permissions without a passkey"
          data-testid="test-page-permission-enabled"
        />
        Let {who} confirm its own page permissions without a passkey
      </label>
      <p className="mt-1 text-[11px] text-muted-foreground">
        For testing: this agent&rsquo;s own login can confirm its OWN act-as-you permission on a host site&rsquo;s
        page without a passkey, so a headless test can run the page-agent flow as the agent itself. It never
        confirms a person&rsquo;s permission. Turning this off stops what it confirmed.
      </p>
      <p className="mt-1 text-[11px] text-muted-foreground" data-testid="test-page-permission-summary">
        {!state.login_email
          ? 'This agent has no canopy login of its own.'
          : state.enabled
            ? `On · ${state.live_grants} live page permission${state.live_grants === 1 ? '' : 's'}`
            : 'Off'}
      </p>
      {error && <p className="mt-1 text-[12px] text-destructive">{error}</p>}
    </section>
  )
}
