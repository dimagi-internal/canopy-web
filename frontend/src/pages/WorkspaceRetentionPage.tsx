import { useCallback, useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'

import {
  deleteRetentionRule,
  getRetention,
  previewRetention,
  saveRetentionRule,
  WorkspaceApiError,
  type RetentionOut,
  type RetentionPreviewOut,
  type RetentionRuleIn,
  type RetentionRuleOut,
} from '@/api/workspaces'
import { listAgents, type AgentOut } from '@/api/agents'

// How long this workspace keeps the CONTENT of its chats and turns
// (apps/retention). Any member can read the policy, since it decides when their
// own conversations go; admins and owners change it. The server answers
// `can_manage`, so this page never guesses at a role.

const EMPTY: RetentionRuleIn = { kind: '', source: '', principal: '', agent: '', keep_days: 30, note: '' }

const COUNT_LABELS: Record<string, string> = {
  turns_scrubbed: 'turns scrubbed',
  chat_turns_scrubbed: 'chat turns scrubbed',
  chat_messages: 'chat messages',
  chat_attachments: 'attachments',
  chat_drafts: 'drafts',
  chat_page_actions: 'page actions',
  chat_transfer_briefs: 'transfer briefs',
  chats_closed: 'chats emptied + archived',
  chats_touched: 'chats touched',
}

function countsText(counts: Record<string, number>): string {
  const parts = Object.entries(counts)
    .filter(([k, n]) => n > 0 && k !== 'chats_touched')
    .map(([k, n]) => `${n.toLocaleString()} ${COUNT_LABELS[k] ?? k}`)
  return parts.length ? parts.join(' · ') : 'nothing'
}

const select =
  'min-h-11 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0'

export function WorkspaceRetentionPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const [policy, setPolicy] = useState<RetentionOut | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [draft, setDraft] = useState<RetentionRuleIn>(EMPTY)
  const [editing, setEditing] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<RetentionPreviewOut | null>(null)
  const [previewing, setPreviewing] = useState(false)

  const load = useCallback(() => {
    getRetention(slug)
      .then(setPolicy)
      .catch((e: unknown) => setError(
        e instanceof WorkspaceApiError || e instanceof Error ? e.message : 'Could not load retention'))
  }, [slug])

  useEffect(() => {
    load()
    listAgents()
      .then((page) => setAgents(page.items.filter((a) => a.workspace === slug)))
      .catch(() => {})
  }, [slug, load])

  function startEdit(rule: RetentionRuleOut) {
    setEditing(rule.id)
    setDraft({
      kind: rule.kind as RetentionRuleIn['kind'],
      source: rule.source as RetentionRuleIn['source'],
      principal: rule.principal as RetentionRuleIn['principal'],
      agent: rule.agent,
      keep_days: rule.keep_days,
      note: rule.note,
    })
    setError(null)
  }

  function reset() {
    setEditing(null)
    setDraft(EMPTY)
  }

  async function save() {
    setBusy(true)
    setError(null)
    try {
      await saveRetentionRule(slug, draft, editing ?? undefined)
      reset()
      setPreview(null)
      load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save the rule')
    } finally {
      setBusy(false)
    }
  }

  async function remove(rule: RetentionRuleOut) {
    if (!window.confirm(`Remove "${rule.summary}"?`)) return
    setError(null)
    try {
      await deleteRetentionRule(slug, rule.id)
      if (editing === rule.id) reset()
      setPreview(null)
      load()
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not remove the rule')
    }
  }

  async function runPreview() {
    setPreviewing(true)
    setError(null)
    try {
      setPreview(await previewRetention(slug))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not preview')
    } finally {
      setPreviewing(false)
    }
  }

  if (!policy) {
    return error
      ? <p className="text-[13px] text-destructive" data-testid="retention-error">{error}</p>
      : <p className="text-[13px] text-muted-foreground">Loading…</p>
  }

  const manage = policy.can_manage
  const forever = draft.keep_days === null

  return (
    <div className="flex max-w-5xl flex-col gap-5" data-testid="workspace-retention">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">Retention</h2>
        <p className="mt-1 max-w-3xl text-[13px] text-foreground-secondary">
          How long canopy keeps what was <em>said</em> in this workspace: chat messages, attachments,
          and each turn&rsquo;s prompt, event log and raw transcript. When a rule&rsquo;s days pass, that
          content is deleted. The record that a chat or turn happened stays (who, when, which agent and
          box), so activity and agent stats are unaffected.
        </p>
        <p
          className={`mt-2 text-[12px] ${policy.enforced ? 'text-warning' : 'text-muted-foreground'}`}
          data-testid="retention-enforced"
        >
          {policy.enforced
            ? '● Enforced: content past its rule is deleted hourly, and cannot be recovered.'
            : '○ Not enforced on this deployment yet. Rules are saved and previewable; nothing is deleted.'}
        </p>
      </section>

      <section>
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          This workspace&rsquo;s rules
        </h3>
        {policy.rules.length === 0 ? (
          <p className="mt-2 text-[12px] text-muted-foreground" data-testid="retention-no-rules">
            None. {policy.inherited.length ? 'The inherited rules below apply.' : 'Everything is kept forever.'}
          </p>
        ) : (
          <table className="mt-2 w-full text-[12px]" data-testid="retention-rules">
            <thead className="text-left text-muted-foreground">
              <tr className="border-b border-border">
                <th className="py-1 pr-3 font-medium">Rule</th>
                <th className="py-1 pr-3 font-medium">Why</th>
                <th className="py-1 pr-3 font-medium">Set by</th>
                {manage && <th className="py-1" />}
              </tr>
            </thead>
            <tbody>
              {policy.rules.map((r) => (
                <tr key={r.id} className="border-b border-border" data-testid={`retention-rule-${r.id}`}>
                  <td className="py-1.5 pr-3 text-foreground">{r.summary}</td>
                  <td className="py-1.5 pr-3 text-foreground-secondary">{r.note}</td>
                  <td className="py-1.5 pr-3 text-muted-foreground">{r.created_by}</td>
                  {manage && (
                    <td className="whitespace-nowrap py-1.5 text-right">
                      <button type="button" className="px-2 text-primary hover:underline" onClick={() => startEdit(r)}>
                        Edit
                      </button>
                      <button
                        type="button"
                        className="px-2 text-destructive hover:underline"
                        onClick={() => void remove(r)}
                        data-testid={`retention-delete-${r.id}`}
                      >
                        Remove
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {manage && (
        <section className="rounded-lg border border-border bg-card p-3" data-testid="retention-form">
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            {editing === null ? 'Add a rule' : 'Edit rule'}
          </h3>
          <p className="mt-1 text-[12px] text-muted-foreground">Leave a filter on &ldquo;Any&rdquo; to match everything.</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <select aria-label="Kind" className={select} value={draft.kind}
              onChange={(e) => setDraft({ ...draft, kind: e.target.value as RetentionRuleIn['kind'] })}>
              <option value="">Chats and turns</option>
              {policy.choices.kinds.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
            <select aria-label="Source" className={select} value={draft.source}
              onChange={(e) => setDraft({ ...draft, source: e.target.value as RetentionRuleIn['source'] })}>
              <option value="">Any source</option>
              {policy.choices.sources.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
            <select aria-label="Started by" className={select} value={draft.principal}
              onChange={(e) => setDraft({ ...draft, principal: e.target.value as RetentionRuleIn['principal'] })}>
              <option value="">Anyone</option>
              {policy.choices.principals.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
            </select>
            <select aria-label="Agent" className={select} value={draft.agent}
              onChange={(e) => setDraft({ ...draft, agent: e.target.value })}>
              <option value="">Any agent</option>
              {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
            </select>
            <label className="flex items-center gap-1 text-[12px] text-muted-foreground">
              Keep
              <input
                type="number"
                min={1}
                aria-label="Days to keep"
                disabled={forever}
                value={forever ? '' : draft.keep_days ?? ''}
                onChange={(e) => setDraft({ ...draft, keep_days: e.target.value ? Number(e.target.value) : 1 })}
                className="min-h-11 w-20 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground disabled:opacity-40 sm:min-h-0"
              />
              days
            </label>
            <label className="flex items-center gap-1 text-[12px] text-muted-foreground">
              <input
                type="checkbox"
                checked={forever}
                onChange={(e) => setDraft({ ...draft, keep_days: e.target.checked ? null : 30 })}
                data-testid="retention-forever"
              />
              Keep forever
            </label>
          </div>
          <input
            aria-label="Why"
            placeholder="Why (optional) — e.g. contacts' conversations are not ours to keep"
            value={draft.note}
            onChange={(e) => setDraft({ ...draft, note: e.target.value })}
            className="mt-2 min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0"
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              onClick={() => void save()}
              disabled={busy}
              data-testid="retention-save"
              className="min-h-11 rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-40 sm:min-h-0"
            >
              {busy ? 'Saving…' : editing === null ? 'Add rule' : 'Save rule'}
            </button>
            {editing !== null && (
              <button type="button" onClick={reset} className="px-2 text-[12px] text-muted-foreground hover:underline">
                Cancel
              </button>
            )}
          </div>
        </section>
      )}

      {error && <p className="text-[12px] text-destructive" data-testid="retention-error">{error}</p>}

      {manage && (
        <section data-testid="retention-preview">
          <div className="flex items-center gap-3">
            <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              What these rules would delete now
            </h3>
            <button
              type="button"
              onClick={() => void runPreview()}
              disabled={previewing}
              data-testid="retention-preview-run"
              className="rounded-md border border-border px-2 py-0.5 text-[12px] text-foreground hover:bg-muted disabled:opacity-40"
            >
              {previewing ? 'Counting…' : preview ? 'Recount' : 'Preview'}
            </button>
          </div>
          <p className="mt-1 text-[12px] text-muted-foreground">
            Counts only, for this workspace&rsquo;s own chats and turns. Nothing is deleted by previewing.
          </p>
          {preview && (
            preview.by_rule.length === 0 ? (
              <p className="mt-2 text-[12px] text-foreground-secondary" data-testid="retention-preview-empty">
                Nothing here is past its rule yet.
              </p>
            ) : (
              <table className="mt-2 w-full text-[12px]" data-testid="retention-preview-table">
                <tbody>
                  {preview.by_rule.map((row) => (
                    <tr key={row.rule_id} className="border-b border-border">
                      <td className="py-1.5 pr-3 text-foreground">{row.summary}</td>
                      <td className="py-1.5 text-foreground-secondary">{countsText(row.counts)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          )}
        </section>
      )}

      <section>
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Inherited</h3>
        {policy.inherited.length === 0 ? (
          <p className="mt-2 text-[12px] text-muted-foreground">No rules above this workspace.</p>
        ) : (
          <table className="mt-2 w-full text-[12px]" data-testid="retention-inherited">
            <tbody>
              {policy.inherited.map((r) => (
                <tr key={r.id} className="border-b border-border">
                  <td className="w-40 py-1.5 pr-3 font-mono text-muted-foreground">{r.workspace || 'deployment'}</td>
                  <td className="py-1.5 text-foreground-secondary">{r.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <p className="mt-2 max-w-3xl text-[12px] text-muted-foreground">
          Which rule applies: this workspace&rsquo;s rules first, then each workspace above it, then the
          deployment&rsquo;s. The first level with any matching rule decides, so a rule here overrides one
          above. Within a level the most specific rule wins, and a tie goes to the shorter retention. A
          chat is one unit: its messages and the turns it ran expire together. Content no rule matches
          is kept forever.
        </p>
      </section>
    </div>
  )
}
