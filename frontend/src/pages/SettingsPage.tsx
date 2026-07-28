import { useEffect, useState, useRef, useCallback } from 'react'
import { mintDebugSession, type MintDebugSessionResponse } from '@/api/debug'
import { aiStatus as fetchAiStatus, aiAuthStart, aiAuthComplete, aiAuthPoll } from '@/api/ai'
import { getPresencePreference, setPresencePreference } from '@/api/presence'
import { notifyPresencePreferenceChanged } from '@/presence/events'
import { Button } from 'canopy-ui/ui'
import { Input } from 'canopy-ui/ui'

type Step = 'idle' | 'loading' | 'awaiting_code' | 'submitting' | 'complete' | 'error'

export function SettingsPage() {
  const [aiStatus, setAiStatus] = useState<{
    backend: string; ready: boolean; detail: string
  } | null>(null)
  const [step, setStep] = useState<Step>('idle')
  const [authUrl, setAuthUrl] = useState<string | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [tokenPreview, setTokenPreview] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Presence visibility. Defaults to visible (matches the backend default
  // when no preference row exists yet) so the checkbox never flashes
  // unchecked before the fetch resolves.
  const [showPresence, setShowPresence] = useState(true)

  const refreshStatus = useCallback(() => {
    fetchAiStatus().then(setAiStatus).catch(() => {})
  }, [])

  useEffect(() => {
    refreshStatus()
  }, [refreshStatus])

  useEffect(() => {
    getPresencePreference()
      .then((pref) => setShowPresence(pref.show_presence))
      .catch(() => {
        // Presence is an enhancement — a failed fetch just leaves the
        // default (visible) in place rather than surfacing an error here.
      })
  }, [])

  async function togglePresence(next: boolean) {
    setShowPresence(next) // optimistic
    try {
      await setPresencePreference(next)
      // The presence socket only re-reads visibility on its next
      // `presence.enter` (i.e. the next navigation) — tell every open tab's
      // socket to reconnect right now so opting out takes effect
      // immediately instead of leaving the user visible until each tab
      // happens to navigate. See AppLayout's PresenceHeaderBadge +
      // usePresenceReconnectNonce.
      notifyPresencePreferenceChanged()
    } catch {
      // Revert the optimistic update — the change didn't actually persist.
      setShowPresence(!next)
    }
  }

  // Poll for auth completion while awaiting code (browser might complete it)
  useEffect(() => {
    if (step !== 'awaiting_code') return
    pollRef.current = setInterval(async () => {
      try {
        const result = await aiAuthPoll()
        if (result.authenticated) {
          setStep('complete')
          refreshStatus()
        }
      } catch { /* ignore */ }
    }, 2000)
    return () => { if (pollRef.current) clearInterval(pollRef.current) }
  }, [step, refreshStatus])

  async function handleStartLogin() {
    setStep('loading')
    setError(null)
    setAuthUrl(null)
    setCode('')
    setTokenPreview(null)
    try {
      const result = await aiAuthStart()
      if (result.status === 'complete') {
        setStep('complete')
        refreshStatus()
      } else {
        setAuthUrl(result.auth_url)
        setStep('awaiting_code')
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start login')
      setStep('error')
    }
  }

  async function handleSubmitCode() {
    if (!code.trim()) return
    setStep('submitting')
    setError(null)
    try {
      const result = await aiAuthComplete(code.trim())
      setTokenPreview(result.token_preview)
      setStep('complete')
      refreshStatus()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to complete login')
      setStep('awaiting_code')
    }
  }

  return (
    <div className="space-y-5 max-w-xl">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Settings</h1>
        <p className="mt-0.5 text-xs text-muted-foreground">
          Manage your AI backend and authentication.
        </p>
      </div>

      {/* Current status */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-3">
        <h2 className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">AI Backend</h2>
        {aiStatus ? (
          <div className="flex items-center gap-3">
            <span className={
              aiStatus.ready
                ? 'inline-flex items-center gap-1.5 text-xs font-medium text-success bg-success/10 border border-success/30 px-2 py-1 rounded'
                : 'inline-flex items-center gap-1.5 text-xs font-medium text-warning bg-warning/10 border border-warning/30 px-2 py-1 rounded'
            }>
              <span className={`w-1.5 h-1.5 rounded-full ${aiStatus.ready ? 'bg-success shadow-[0_0_6px_rgba(74,222,128,0.5)]' : 'bg-warning shadow-[0_0_6px_rgba(251,191,36,0.5)]'}`} />
              {aiStatus.ready ? 'Connected' : 'Not connected'}
            </span>
            <span className="text-sm text-foreground-secondary">{aiStatus.detail}</span>
          </div>
        ) : (
          <span className="text-sm text-muted-foreground">Loading...</span>
        )}
      </div>

      {/* Login flow */}
      {aiStatus && !aiStatus.ready && (
        <div className="rounded-xl border border-border bg-card p-5 space-y-4">
          <div>
            <h2 className="text-sm font-semibold text-foreground">Connect Claude Subscription</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Sign in with your Anthropic account to use your Claude subscription for AI calls.
            </p>
          </div>

          {step === 'idle' || step === 'error' || step === 'loading' ? (
            <div className="space-y-3">
              <Button size="sm" onClick={handleStartLogin} disabled={step === 'loading'}>
                {step === 'loading' ? (
                  <span className="flex items-center gap-2">
                    <svg className="animate-spin h-4 w-4" viewBox="0 0 24 24" fill="none">
                      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                    </svg>
                    Starting login...
                  </span>
                ) : 'Start Login'}
              </Button>
              {step === 'loading' && (
                <p className="text-xs text-muted-foreground">Generating authorization link (a few seconds)...</p>
              )}
              {error && (
                <p className="text-sm text-destructive">{error}</p>
              )}
            </div>
          ) : step === 'awaiting_code' ? (
            <div className="space-y-4">
              <div className="space-y-2">
                <p className="text-sm text-foreground-secondary">
                  <span className="text-primary font-semibold">1.</span> Click the link below to authorize:
                </p>
                <a
                  href={authUrl!}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block text-sm text-primary hover:text-primary underline underline-offset-2 break-all"
                >
                  Open Anthropic Login ↗
                </a>
              </div>
              <div className="space-y-2">
                <p className="text-sm text-foreground-secondary">
                  <span className="text-primary font-semibold">2.</span> After authorizing, paste the code shown on the callback page:
                </p>
                <div className="flex gap-2">
                  <Input
                    placeholder="Paste authorization code..."
                    value={code}
                    onChange={(e) => setCode(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') handleSubmitCode() }}
                    className="font-mono text-sm"
                  />
                  <Button size="sm" onClick={handleSubmitCode} disabled={!code.trim()}>
                    Submit
                  </Button>
                </div>
              </div>
              {error && (
                <p className="text-sm text-destructive">{error}</p>
              )}
            </div>
          ) : step === 'submitting' ? (
            <p className="text-sm text-foreground-secondary">Completing authentication...</p>
          ) : null}
        </div>
      )}

      {/* Success state — only after completing CLI auth flow, not for general readiness */}
      {step === 'complete' && aiStatus?.backend === 'cli' && (
        <div className="rounded-xl border border-success/30 bg-success/10 p-4 space-y-1">
          <p className="text-sm font-semibold text-success">Authenticated</p>
          {tokenPreview && (
            <p className="text-xs text-success/80 font-mono">{tokenPreview}</p>
          )}
          <p className="text-xs text-success/70">
            Claude CLI is connected via your subscription. Token persists across container restarts.
          </p>
        </div>
      )}

      {/* Presence */}
      <div className="rounded-xl border border-border bg-card p-5 space-y-3">
        <h2 className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">Presence</h2>
        <div className="flex items-start gap-3">
          <input
            id="presence-toggle"
            type="checkbox"
            checked={showPresence}
            onChange={(e) => void togglePresence(e.target.checked)}
            aria-describedby="presence-toggle-help"
            className="mt-0.5 h-4 w-4 rounded border-input text-primary focus:ring-primary"
          />
          <div>
            {/* The <label> holds ONLY the control's name — its content is
                what screen readers announce as the checkbox's accessible
                name. The help sentence lives in its own <p>, wired in via
                aria-describedby instead of nesting inside the label, so the
                accessible name stays "Show me as viewing" rather than the
                whole two-sentence blob. */}
            <label htmlFor="presence-toggle" className="block text-sm text-foreground-secondary cursor-pointer">
              Show me as viewing
            </label>
            <p id="presence-toggle-help" className="text-xs text-muted-foreground">
              When off, you can still see who else is viewing a page, but they cannot see you.
            </p>
          </div>
        </div>
      </div>

      <DebugAccessPanel />
    </div>
  )
}

type Mint = MintDebugSessionResponse

const TTL_OPTIONS: Array<{ label: string; seconds: number }> = [
  { label: '1 hour', seconds: 60 * 60 },
  { label: '24 hours', seconds: 24 * 60 * 60 },
  { label: '1 week', seconds: 7 * 24 * 60 * 60 },
]

function DebugAccessPanel() {
  const [mint, setMint] = useState<Mint | null>(null)
  const [minting, setMinting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [ttl, setTtl] = useState<number>(24 * 60 * 60)
  const [copied, setCopied] = useState<string | null>(null)

  async function handleMint() {
    setMinting(true)
    setError(null)
    setCopied(null)
    try {
      const result = await mintDebugSession(ttl)
      setMint(result)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to mint session')
    } finally {
      setMinting(false)
    }
  }

  async function copy(text: string, key: string) {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500)
    } catch {
      // ignore
    }
  }

  const expiresRelative = mint
    ? formatExpiry(new Date(mint.expires_at))
    : null

  return (
    <div className="rounded-xl border border-border bg-card p-5 space-y-4">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Debug access</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          Mint a short-lived session cookie you can hand to an AI assistant
          (or any HTTP client). It authenticates as you, for the TTL you pick.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">Valid for:</span>
        <div className="flex gap-1">
          {TTL_OPTIONS.map((opt) => (
            <button
              key={opt.seconds}
              type="button"
              onClick={() => setTtl(opt.seconds)}
              className={`text-xs px-2.5 py-1 rounded border transition-colors ${
                ttl === opt.seconds
                  ? 'bg-primary/10 border-primary/30 text-primary'
                  : 'bg-background border-border text-muted-foreground hover:text-foreground-secondary hover:border-input'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <div className="ml-auto">
          <Button size="sm" onClick={handleMint} disabled={minting}>
            {minting ? 'Minting…' : mint ? 'Mint another' : 'Mint session cookie'}
          </Button>
        </div>
      </div>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {mint && (
        <div className="space-y-3">
          <div className="rounded-lg border border-warning/20 bg-warning/5 p-3 text-xs text-warning/80">
            <strong className="text-warning">Treat this like a password.</strong>{' '}
            Anyone with this cookie has your access until {expiresRelative}.
          </div>

          <CopyBlock
            label="Cookie"
            value={`${mint.cookie_name}=${mint.cookie_value}`}
            copied={copied === 'cookie'}
            onCopy={() =>
              copy(`${mint.cookie_name}=${mint.cookie_value}`, 'cookie')
            }
          />

          <CopyBlock
            label="curl example"
            value={mint.curl_example}
            copied={copied === 'curl'}
            onCopy={() => copy(mint.curl_example, 'curl')}
          />

          <div className="text-[11px] text-muted-foreground">
            Minted for {mint.email} · expires {expiresRelative} ({new Date(mint.expires_at).toLocaleString()})
          </div>
        </div>
      )}
    </div>
  )
}

function CopyBlock({
  label, value, copied, onCopy,
}: { label: string; value: string; copied: boolean; onCopy: () => void }) {
  return (
    <div>
      <div className="flex items-center justify-between mb-1">
        <span className="text-[10px] uppercase tracking-wider text-muted-foreground font-semibold">{label}</span>
        <button
          type="button"
          onClick={onCopy}
          className="text-xs text-primary hover:text-primary transition-colors"
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre className="bg-background border border-border rounded-lg p-3 text-xs text-foreground-secondary font-mono overflow-x-auto whitespace-pre-wrap break-all">
        {value}
      </pre>
    </div>
  )
}

function formatExpiry(date: Date): string {
  const diffMs = date.getTime() - Date.now()
  if (diffMs <= 0) return 'expired'
  const hours = Math.round(diffMs / (1000 * 60 * 60))
  if (hours < 1) return 'in <1 hour'
  if (hours < 48) return `in ${hours}h`
  const days = Math.round(hours / 24)
  return `in ${days}d`
}
