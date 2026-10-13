import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { clsx } from 'clsx'

import {
  createAdmissionRule,
  deleteAdmissionRule,
  getAdmissionRules,
  setAdmissionSenderNotices,
  updateAdmissionRule,
  WorkspaceApiError,
  type AdmissionRuleIn,
  type AdmissionRuleOut,
  type AdmissionRulesOut,
} from '@/api/workspaces'
import { listAgents, type AgentOut } from '@/api/agents'

// Which turns this workspace refuses before any session starts (apps/admission,
// canopy-web#1400). Any member reads the rules; admins change them, and the
// server says which via `can_manage`. The owner's notice links here with
// `?rule=<id>`, so the rule that fired is highlighted and scrolled to.

const EMPTY: AdmissionRuleIn = {
  agent: '', origin: '', principal: '', sender: '', access: '', capability: '', turn_mode: '', runner_kind: '',
  reach: '', wait_hours: 24, action: 'reject', reason: '', notify_sender: false, notify_owner: true, enabled: true,
}

const select =
  'min-h-11 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0'

function fired(rule: AdmissionRuleOut): string {
  if (!rule.fired_count) return 'never'
  const when = rule.last_fired_at ? new Date(rule.last_fired_at).toLocaleString() : ''
  return `${rule.fired_count.toLocaleString()}× · last ${when}`
}

function notices(rule: AdmissionRuleOut, repliesOn: boolean): string {
  if (rule.action === 'allow') return '—'
  // With replies off a rule's sender notice is recorded, not sent: say so where it is listed.
  const sender = repliesOn ? 'sender' : 'sender (off)'
  const parts = [rule.notify_sender && sender, rule.notify_owner && 'owner'].filter(Boolean)
  return parts.length ? parts.join(' + ') : 'none'
}

export function WorkspaceAdmissionRulesPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const [params] = useSearchParams()
  const highlighted = Number(params.get('rule')) || null
  const [policy, setPolicy] = useState<AdmissionRulesOut | null>(null)
  const [agents, setAgents] = useState<AgentOut[]>([])
  const [draft, setDraft] = useState<AdmissionRuleIn>(EMPTY)
  const [editing, setEditing] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const highlightRef = useRef<HTMLTableRowElement | null>(null)

  const load = useCallback(() => {
    getAdmissionRules(slug)
      .then(setPolicy)
      .catch((e: unknown) => setError(
        e instanceof WorkspaceApiError || e instanceof Error ? e.message : 'Could not load admission rules'))
  }, [slug])

  useEffect(() => {
    load()
    listAgents()
      .then((page) => setAgents(page.items.filter((a) => a.workspace === slug)))
      .catch(() => {})
  }, [slug, load])

  useEffect(() => {
    highlightRef.current?.scrollIntoView?.({ block: 'center' })
  }, [policy, highlighted])

  function startEdit(rule: AdmissionRuleOut) {
    setEditing(rule.id)
    setDraft({
      agent: rule.agent,
      origin: rule.origin as AdmissionRuleIn['origin'],
      principal: rule.principal as AdmissionRuleIn['principal'],
      sender: rule.sender as AdmissionRuleIn['sender'],
      access: rule.access as AdmissionRuleIn['access'],
      capability: rule.capability,
      turn_mode: rule.turn_mode as AdmissionRuleIn['turn_mode'],
      runner_kind: rule.runner_kind as AdmissionRuleIn['runner_kind'],
      reach: rule.reach as AdmissionRuleIn['reach'],
      wait_hours: rule.wait_hours,
      action: rule.action as AdmissionRuleIn['action'],
      reason: rule.reason,
      notify_sender: rule.notify_sender,
      notify_owner: rule.notify_owner,
      enabled: rule.enabled,
    })
    setError(null)
  }

  function reset() {
    setEditing(null)
    setDraft(EMPTY)
  }

  async function run(fn: () => Promise<unknown>, fallback: string) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      load()
      return true
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : fallback)
      return false
    } finally {
      setBusy(false)
    }
  }

  async function save() {
    const ok = await run(
      () => (editing === null ? createAdmissionRule(slug, draft) : updateAdmissionRule(slug, editing, draft)),
      'Could not save the rule',
    )
    if (ok) reset()
  }

  async function toggle(rule: AdmissionRuleOut) {
    await run(() => updateAdmissionRule(slug, rule.id, { enabled: !rule.enabled }), 'Could not switch the rule')
  }

  async function remove(rule: AdmissionRuleOut) {
    if (!window.confirm(`Remove "${rule.summary}"?${rule.seeded ? ' It is a default and will not come back.' : ''}`)) return
    const ok = await run(() => deleteAdmissionRule(slug, rule.id), 'Could not remove the rule')
    if (ok && editing === rule.id) reset()
  }

  if (!policy) {
    return error
      ? <p className="text-[13px] text-destructive" data-testid="admission-error">{error}</p>
      : <p className="text-[13px] text-muted-foreground">Loading…</p>
  }

  const manage = policy.can_manage
  const c = policy.choices
  const set = (patch: Partial<AdmissionRuleIn>) => setDraft({ ...draft, ...patch })

  return (
    <div className="flex max-w-6xl flex-col gap-5" data-testid="workspace-admission">
      <section>
        <h2 className="text-[15px] font-semibold text-foreground">Admission rules</h2>
        <p className="mt-1 max-w-3xl text-[13px] text-foreground-secondary">
          Turns this workspace refuses before any session starts, because they can never complete. A
          rejected turn is closed as cancelled. Its sender can get a fixed automatic reply on their thread,
          and the agent&rsquo;s owner gets a separate notice saying which rule fired, with a link back here.
          An <em>allow</em> rule exempts the turns it matches from every reject rule, which is how one
          agent opts out of a workspace-wide rule. A disabled rule matches nothing.
        </p>
        <p className="mt-1 max-w-3xl text-[13px] text-foreground-secondary">
          A rule on a <em>blocked sender</em> or on <em>access: none</em> (a person the agent offers
          nothing) is different: canopy refuses those turns whatever the rules say. Such a rule only
          decides what the refusal records and who is told, so it cannot be an <em>allow</em>.
        </p>
        <label
          className="mt-3 flex items-start gap-2 rounded border border-border px-3 py-2 text-[12px]"
          data-testid="admission-sender-notices"
        >
          <input
            type="checkbox"
            className="mt-0.5"
            checked={policy.sender_notices_enabled}
            disabled={!manage || busy}
            onChange={() => void run(
              () => setAdmissionSenderNotices(slug, !policy.sender_notices_enabled),
              'Could not switch automatic replies')}
            data-testid="admission-sender-notices-toggle"
          />
          <span>
            <span className="font-medium text-foreground">Send automatic replies to rejected senders</span>
            {' '}
            <span className={policy.sender_notices_enabled ? 'text-success' : 'text-muted-foreground'}>
              {policy.sender_notices_enabled ? 'On' : 'Off'}
            </span>
            <span className="block text-muted-foreground">
              {policy.sender_notices_enabled
                ? 'On in this workspace: a rule that asks for one replies once on the sender’s thread, from the agent’s mailbox. Never to a machine, a no-reply address or another agent.'
                : 'Off in this workspace: replies are recorded, not sent. Owner notices still go out.'}
            </span>
          </span>
        </label>
      </section>

      <section>
        {policy.rules.length === 0 ? (
          <p className="text-[12px] text-muted-foreground" data-testid="admission-no-rules">
            None. Every turn is admitted.
          </p>
        ) : (
          <table className="w-full text-[12px]" data-testid="admission-rules">
            <thead className="text-left text-muted-foreground">
              <tr className="border-b border-border">
                <th className="py-1 pr-3 font-medium">On</th>
                <th className="py-1 pr-3 font-medium">Rule</th>
                <th className="py-1 pr-3 font-medium">Why</th>
                <th className="py-1 pr-3 font-medium">Notices</th>
                <th className="py-1 pr-3 font-medium">Fired</th>
                {manage && <th className="py-1" />}
              </tr>
            </thead>
            <tbody>
              {policy.rules.map((r) => (
                <tr
                  key={r.id}
                  ref={r.id === highlighted ? highlightRef : undefined}
                  className={clsx('border-b border-border align-top',
                    r.id === highlighted && 'bg-primary/10 outline outline-1 outline-primary',
                    !r.enabled && 'opacity-60')}
                  data-testid={`admission-rule-${r.id}`}
                  data-highlighted={r.id === highlighted ? 'true' : undefined}
                >
                  <td className="py-1.5 pr-3">
                    <input
                      type="checkbox"
                      aria-label={`Rule ${r.id} enabled`}
                      checked={r.enabled}
                      disabled={!manage || busy}
                      onChange={() => void toggle(r)}
                      data-testid={`admission-toggle-${r.id}`}
                    />
                  </td>
                  <td className="py-1.5 pr-3 text-foreground">
                    <span className={clsx('mr-1 rounded border px-1 text-[11px]',
                      r.action === 'reject'
                        ? 'border-destructive/30 bg-destructive/10 text-destructive'
                        : 'border-success/30 bg-success/10 text-success')}>
                      {r.action}
                    </span>
                    {r.summary}
                    {r.seeded && <span className="ml-1 text-[11px] text-muted-foreground">(default)</span>}
                    {r.refusal && (
                      <span className="ml-1 text-[11px] text-muted-foreground" data-testid={`admission-refusal-${r.id}`}>
                        · always refused; this sets the notices
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 text-foreground-secondary">{r.reason}</td>
                  <td className="py-1.5 pr-3 text-muted-foreground">{notices(r, policy.sender_notices_enabled)}</td>
                  <td className="whitespace-nowrap py-1.5 pr-3 text-muted-foreground" data-testid={`admission-fired-${r.id}`}>
                    {fired(r)}
                  </td>
                  {manage && (
                    <td className="whitespace-nowrap py-1.5 text-right">
                      <button type="button" className="px-2 text-primary hover:underline" onClick={() => startEdit(r)}>
                        Edit
                      </button>
                      <button
                        type="button"
                        className="px-2 text-destructive hover:underline"
                        onClick={() => void remove(r)}
                        data-testid={`admission-delete-${r.id}`}
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
        <section className="rounded-lg border border-border bg-card p-3" data-testid="admission-form">
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            {editing === null ? 'Add a rule' : `Edit rule ${editing}`}
          </h3>
          <p className="mt-1 text-[12px] text-muted-foreground">
            Leave a condition on &ldquo;Any&rdquo; to match everything; set at least one.
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <select aria-label="Action" className={select} value={draft.action}
              onChange={(e) => set({ action: e.target.value as AdmissionRuleIn['action'] })}>
              {c.actions.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select aria-label="Agent" className={select} value={draft.agent}
              onChange={(e) => set({ agent: e.target.value })}>
              <option value="">Any agent</option>
              {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
            </select>
            <select aria-label="Channel" className={select} value={draft.origin}
              onChange={(e) => set({ origin: e.target.value as AdmissionRuleIn['origin'] })}>
              <option value="">Any channel</option>
              {c.origins.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select aria-label="Asked by" className={select} value={draft.principal}
              onChange={(e) => set({ principal: e.target.value as AdmissionRuleIn['principal'] })}>
              <option value="">Anyone</option>
              {c.principals.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select aria-label="Sender" className={select} value={draft.sender}
              onChange={(e) => set({ sender: e.target.value as AdmissionRuleIn['sender'] })}>
              <option value="">Any sender</option>
              {c.senders.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select aria-label="Access" className={select} value={draft.access}
              onChange={(e) => set({ access: e.target.value as AdmissionRuleIn['access'] })}>
              <option value="">Any access</option>
              {c.access.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <input
              aria-label="Capability"
              placeholder="Any capability"
              value={draft.capability}
              onChange={(e) => set({ capability: e.target.value })}
              className="min-h-11 w-36 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0"
            />
            <select aria-label="Turn mode" className={select} value={draft.turn_mode}
              onChange={(e) => set({ turn_mode: e.target.value as AdmissionRuleIn['turn_mode'] })}>
              <option value="">Any mode</option>
              {c.turn_modes.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select aria-label="Runner" className={select} value={draft.runner_kind}
              onChange={(e) => set({ runner_kind: e.target.value as AdmissionRuleIn['runner_kind'] })}>
              <option value="">Any runner</option>
              {c.runner_kinds.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            <select aria-label="Runners that could take it" className={select} value={draft.reach}
              onChange={(e) => set({ reach: e.target.value as AdmissionRuleIn['reach'] })}>
              <option value="">Whatever its runners are doing</option>
              {c.reaches.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            {draft.reach === 'offline' && (
              <label className="flex items-center gap-1 text-[12px] text-muted-foreground">
                after
                <input
                  aria-label="Wait (hours)"
                  type="number"
                  min={1}
                  value={draft.wait_hours}
                  onChange={(e) => set({ wait_hours: Math.max(1, Number(e.target.value) || 1) })}
                  className="min-h-11 w-16 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0"
                />
                h
              </label>
            )}
          </div>
          <input
            aria-label="Why"
            placeholder="Why — the owner reads this when the rule fires"
            value={draft.reason}
            onChange={(e) => set({ reason: e.target.value })}
            className="mt-2 min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground sm:min-h-0"
          />
          <div className="mt-2 flex flex-wrap items-center gap-4 text-[12px] text-muted-foreground">
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={draft.notify_sender} disabled={draft.action === 'allow'}
                onChange={(e) => set({ notify_sender: e.target.checked })} />
              Automatic reply to the sender
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={draft.notify_owner} disabled={draft.action === 'allow'}
                onChange={(e) => set({ notify_owner: e.target.checked })} />
              Notify the agent&rsquo;s owner
            </label>
            <label className="flex items-center gap-1">
              <input type="checkbox" checked={draft.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
              Enabled
            </label>
          </div>
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              onClick={() => void save()}
              disabled={busy || !draft.reason.trim()}
              data-testid="admission-save"
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

      {error && <p className="text-[12px] text-destructive" data-testid="admission-error">{error}</p>}
    </div>
  )
}
