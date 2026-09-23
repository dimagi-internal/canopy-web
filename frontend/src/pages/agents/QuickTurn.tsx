import { useState } from 'react'

import { enqueueTurn } from '@/api/harness'

// Dispatch a prompt straight to THIS agent from its own page — the per-agent
// counterpart to /supervisor's cross-fleet composer, so "act on this agent" doesn't
// require bouncing to the supervisor. Enqueues a turn; the runner claims + runs it
// (it waits in the queue if the runner is paused/offline).
export function QuickTurn({ slug }: { slug: string }) {
  const [prompt, setPrompt] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)

  const send = async () => {
    const p = prompt.trim()
    if (!p) return
    setBusy(true)
    setError(null)
    try {
      await enqueueTurn({ agentSlug: slug, prompt: p })
      setPrompt('')
      setSent(true)
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Failed to dispatch')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-lg border border-border bg-card p-3">
      <div className="flex gap-2">
        <input
          value={prompt}
          onChange={(e) => {
            setPrompt(e.target.value)
            setSent(false)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void send()
          }}
          placeholder={`Dispatch a prompt to ${slug}…`}
          data-testid="quickturn-input"
          className="min-w-0 flex-1 rounded-md border border-input bg-input px-3 py-2 text-[13px] text-foreground"
        />
        <button
          type="button"
          disabled={busy || prompt.trim() === ''}
          onClick={() => void send()}
          className="shrink-0 rounded-md bg-primary px-4 py-2 text-[13px] font-medium text-primary-foreground transition-colors hover:bg-primary/90 disabled:opacity-50"
        >
          {busy ? 'Sending…' : 'Send'}
        </button>
      </div>
      {error && <p className="mt-1 text-[11px] text-destructive">{error}</p>}
      {sent && !error && (
        <p className="mt-1 text-[11px] text-success">Dispatched — the runner will pick it up.</p>
      )}
    </div>
  )
}
