import { useCallback, useEffect, useState, type FormEvent, type JSX } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Button, StatusBadge, Textarea } from 'canopy-ui/ui'
import {
  diffOf,
  factsOf,
  getLocalAppSubmission,
  revokeLocalAppVersion,
  type LocalAppReview,
} from '@/api/localApps'
import { ApiError, errorText } from '@/api/problem'
import { Td, Th, Tr } from '@/components/DataTable'
import { LocalAppDiff } from '@/components/localApps/LocalAppDiff'
import { useScopedPath } from '@/lib/scopedLinks'

type Tone = 'neutral' | 'success' | 'warning' | 'destructive'
const STATUS: Record<string, { label: string; tone: Tone }> = {
  submitted: { label: 'Submitted', tone: 'neutral' },
  pending: { label: 'Waiting for approval', tone: 'warning' },
  done: { label: 'Approved', tone: 'success' },
  released: { label: 'Released', tone: 'success' },
  revoked: { label: 'Revoked', tone: 'destructive' },
  declined: { label: 'Declined', tone: 'neutral' },
  cancelled: { label: 'Withdrawn', tone: 'neutral' },
  expired: { label: 'Expired', tone: 'neutral' },
  failed: { label: 'Approved, but the release failed', tone: 'destructive' },
}

function size(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`
}

/** One kind of fact: highlighted when it found anything, because that is what a reviewer reads first. */
function FactLine({ label, found }: { label: string; found: Record<string, string[]> }): JSX.Element {
  const names = Object.keys(found)
  if (names.length === 0) {
    return (
      <div className="px-3 py-1.5 text-[13px]" data-testid="local-app-fact">
        <span className="font-medium">{label}</span> <span className="text-muted-foreground">none found</span>
      </div>
    )
  }
  return (
    <div className="bg-warning/10 px-3 py-1.5 text-[13px]" data-testid="local-app-fact" data-found="true">
      <div className="font-medium">{label}</div>
      <ul className="mt-0.5 space-y-0.5">
        {names.map((n) => (
          <li key={n}><code className="font-mono text-xs">{n}</code> <span className="text-xs text-muted-foreground">in {found[n].join(', ')}</span></li>
        ))}
      </ul>
    </div>
  )
}

/**
 * A submitted version, for its reviewer and its author: what it says it does, what reading its code
 * found, and every changed line. Approving happens on the approval's own page and nowhere else, so
 * there's one audited decision and no second button for it. Design: docs/architecture/local-apps.md.
 */
export default function LocalAppReviewPage(): JSX.Element {
  const { id = '' } = useParams()
  const scoped = useScopedPath()
  const submissionId = /^\d+$/.test(id) ? Number(id) : null
  const [sub, setSub] = useState<LocalAppReview | null>(null)
  const [missing, setMissing] = useState(submissionId === null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (submissionId !== null) setSub(await getLocalAppSubmission(submissionId))
  }, [submissionId])

  useEffect(() => {
    setSub(null)
    setError(null)
    setMissing(submissionId === null)
    load().catch((e: unknown) => {
      if (e instanceof ApiError && e.status === 404) setMissing(true)
      else setError(errorText(e, 'Failed to load the submission'))
    })
  }, [load, submissionId])

  const back = <Link to={scoped('/apps')} className="text-sm text-primary hover:underline">← Apps</Link>
  if (missing) return <div className="mx-auto max-w-5xl p-6">{back}<p className="mt-4 text-sm text-muted-foreground" data-testid="local-app-review-missing">Not found, or not yours.</p></div>
  if (error && !sub) return <div className="mx-auto max-w-5xl p-6">{back}<p className="mt-4 text-sm text-destructive">{error}</p></div>
  if (!sub) return <div className="mx-auto max-w-5xl p-6">{back}<p className="mt-4 text-sm text-muted-foreground">Loading…</p></div>

  const status = STATUS[sub.status] ?? { label: sub.status, tone: 'neutral' as const }
  const facts = factsOf(sub)
  const diff = diffOf(sub)
  const cut = diff.filter((f) => f.truncated).length
  const disclosure = sub.manifest.disclosure as { reads?: string[]; sends?: string[] } | undefined
  const notice = typeof sub.manifest.notice === 'string' ? sub.manifest.notice : ''

  return (
    <div className="mx-auto max-w-5xl p-6">
      {back}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <h1 className="text-2xl font-semibold">{sub.name} {sub.version}</h1>
        <StatusBadge tone={status.tone} data-testid="local-app-review-status">{status.label}</StatusBadge>
      </div>

      <table className="mt-3 text-[13px]">
        <tbody>
          <Tr><Th density="flush" className="font-medium text-muted-foreground">Package sha256</Th><Td density="flush" mono className="break-all">{sub.sha256}</Td></Tr>
          <Tr><Th density="flush" className="font-medium text-muted-foreground">Size</Th><Td density="flush">{size(sub.size)}</Td></Tr>
          <Tr><Th density="flush" className="font-medium text-muted-foreground">Submitted by</Th><Td density="flush">{sub.submitted_by || 'Someone no longer here'}, {new Date(sub.submitted_at).toLocaleString()}</Td></Tr>
          <Tr><Th density="flush" className="font-medium text-muted-foreground">Compared with</Th><Td density="flush">{sub.prior_version ? `version ${sub.prior_version}` : 'nothing: this is its first release'}</Td></Tr>
        </tbody>
      </table>

      {sub.approval_id !== null && sub.approval_id !== undefined && (
        <p className="mt-3 text-sm" data-testid="local-app-review-approval">
          <Link to={scoped(`/approvals/${sub.approval_id}`)} className="font-medium text-primary hover:underline">
            {sub.status === 'pending' ? 'Approve or decline on the approval page' : 'See the approval'}
          </Link>
        </p>
      )}

      {sub.revoked_reason && (
        <p className="mt-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-[13px]" data-testid="local-app-review-revoked">
          Revoked: {sub.revoked_reason}
        </p>
      )}

      {sub.note && (
        <section className="mt-4 rounded-md border border-border bg-muted/40 p-3" data-testid="local-app-review-note">
          <h2 className="text-[12px] font-semibold uppercase tracking-wide text-muted-foreground">
            In {sub.submitted_by || 'their'} own words
          </h2>
          <p className="mt-1 whitespace-pre-wrap text-[13px]">{sub.note}</p>
        </section>
      )}

      <section className="mt-6">
        <h2 className="text-sm font-semibold">What it says it does</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">The author's statement. Nothing enforces it, so check it against the facts and the code.</p>
        {notice && <p className="mt-2 text-[13px]"><span className="font-medium">Notice:</span> {notice}</p>}
        {disclosure?.reads?.map((r) => <p key={r} className="mt-1 text-[13px]"><span className="font-medium">Reads:</span> {r}</p>)}
        {disclosure?.sends?.map((s) => <p key={s} className="mt-1 text-[13px]"><span className="font-medium">Sends:</span> {s}</p>)}
      </section>

      {sub.trust_changes.length > 0 && (
        <section className="mt-6" data-testid="local-app-review-trust">
          <h2 className="text-sm font-semibold">What changed in what it promises</h2>
          <p className="mt-0.5 text-xs text-muted-foreground">People who have this app are asked to accept these before the new version runs for them.</p>
          <ul className="mt-1 list-disc pl-5 text-[13px]">{sub.trust_changes.map((c) => <li key={c}>{c}</li>)}</ul>
        </section>
      )}

      <section className="mt-6">
        <h2 className="text-sm font-semibold">What reading the code found</h2>
        <p className="mt-0.5 text-xs text-muted-foreground">From the imports and calls in its Python files. A place to start reading, not a guarantee.</p>
        <div className="mt-2 divide-y divide-border rounded-lg border border-border bg-card">
          <FactLine label="Reaches the network" found={facts.network} />
          <FactLine label="Starts processes" found={facts.process} />
          <FactLine label="Uses risky features" found={facts.risky} />
          {facts.unparsed.length > 0 && (
            <div className="bg-warning/10 px-3 py-1.5 text-[13px]" data-testid="local-app-fact" data-found="true">
              <span className="font-medium">Could not read</span>{' '}
              <span className="font-mono text-xs">{facts.unparsed.join(', ')}</span>
            </div>
          )}
        </div>
        <details className="mt-2 text-[13px]">
          <summary className="cursor-pointer">{facts.files.length} {facts.files.length === 1 ? 'file' : 'files'}</summary>
          <table className="mt-1">
            <thead><Tr head="xs"><Th density="flush">Path</Th><Th density="flush" align="right">Size</Th></Tr></thead>
            <tbody>
              {facts.files.map((f) => (
                <Tr key={f.path}><Td density="flush" mono>{f.path}</Td><Td density="flush" tone="muted" align="right" nowrap>{size(f.size)}</Td></Tr>
              ))}
            </tbody>
          </table>
        </details>
      </section>

      <section className="mt-6">
        <h2 className="text-sm font-semibold">{sub.prior_version ? `Changes since ${sub.prior_version}` : 'The code'}</h2>
        {cut > 0 && (
          <p className="mt-1 text-[13px] text-warning" data-testid="local-app-review-cut">
            Part of this change isn't shown: {cut} {cut === 1 ? 'file is' : 'files are'} cut short by the limit on how much a review shows.
          </p>
        )}
        <div className="mt-2"><LocalAppDiff files={diff} /></div>
      </section>

      {sub.can_revoke && <RevokeForm submissionId={sub.id} title={`${sub.name} ${sub.version}`} reload={load} />}
    </div>
  )
}

function RevokeForm({ submissionId, title, reload }: { submissionId: number; title: string; reload: () => Promise<void> }): JSX.Element {
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!window.confirm(`Revoke ${title} on every computer? Running copies stop at their next check-in, and it can't be started again.`)) return
    setBusy(true)
    setError(null)
    try {
      await revokeLocalAppVersion(submissionId, reason.trim())
      await reload()
    } catch (err) {
      setError(errorText(err, 'Failed to revoke the version'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="mt-8 rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold">Revoke this version</h2>
      <p className="mt-1 text-[13px] text-muted-foreground">Stops it everywhere. People whose copy stops are shown your reason.</p>
      <form className="mt-2 space-y-2" onSubmit={(e) => void submit(e)} aria-label="Revoke">
        <Textarea aria-label="Why" placeholder="Why" required maxLength={500} rows={2} value={reason} onChange={(e) => setReason(e.target.value)} />
        {error && <p className="text-sm text-destructive" role="alert">{error}</p>}
        <Button type="submit" variant="destructive" disabled={busy || !reason.trim()}>Revoke</Button>
      </form>
    </section>
  )
}
