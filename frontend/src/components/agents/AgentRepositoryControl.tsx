import { useState, type FormEvent } from 'react'

import { setAgentRepository } from '@/api/agents'

/** The agent's GitHub repository — what its runners clone and run. Edited in
 *  place (the setup checklist's first step links here), never through the
 *  create wizard, which makes a whole new agent. */
export function AgentRepositoryControl({ slug, initialUrl, initialRef, canEdit }: {
  slug: string
  initialUrl: string
  initialRef: string
  canEdit: boolean
}) {
  const [saved, setSaved] = useState({ url: initialUrl, ref: initialRef })
  const [url, setUrl] = useState(initialUrl)
  const [ref, setRef] = useState(initialRef)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const dirty = url.trim() !== saved.url || (ref.trim() || saved.ref) !== saved.ref

  const save = async (e: FormEvent) => {
    e.preventDefault()
    if (!dirty || busy) return
    setBusy(true)
    setError('')
    setDone(false)
    try {
      const a = await setAgentRepository(slug, url.trim(), ref.trim())
      setSaved({ url: a.definition?.repo_url ?? '', ref: a.definition?.repo_ref ?? '' })
      setUrl(a.definition?.repo_url ?? '')
      setRef(a.definition?.repo_ref ?? '')
      setDone(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not save the repository')
    } finally {
      setBusy(false)
    }
  }

  if (!canEdit) {
    return saved.url
      ? <a href={saved.url} target="_blank" rel="noreferrer" className="font-mono text-[13px] text-primary hover:underline">{saved.url} ↗</a>
      : <p className="text-[13px] text-muted-foreground">None set.</p>
  }
  return (
    <form onSubmit={save} className="flex flex-wrap items-center gap-2" data-testid="agent-repository">
      <input value={url} onChange={(e) => { setUrl(e.target.value); setDone(false) }} aria-label="Repository URL"
             placeholder="https://github.com/dimagi/muse"
             className="min-h-9 min-w-0 flex-1 rounded-md border border-border bg-input px-2.5 font-mono text-[13px]" />
      <input value={ref} onChange={(e) => { setRef(e.target.value); setDone(false) }} aria-label="Branch"
             placeholder="main" className="min-h-9 w-28 rounded-md border border-border bg-input px-2.5 font-mono text-[13px]" />
      <button type="submit" disabled={!dirty || busy || !url.trim()}
              className="min-h-9 rounded-md border border-border px-3 text-[13px] text-foreground disabled:opacity-50">
        {busy ? 'Saving…' : 'Save'}
      </button>
      {done && <span className="text-[12px] text-success">Saved. Its runners pick up the new repository on their next setup.</span>}
      {error && <span className="w-full text-[12px] text-destructive">{error}</span>}
    </form>
  )
}
