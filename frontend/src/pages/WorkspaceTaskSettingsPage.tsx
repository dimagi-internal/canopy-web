// /w/:workspace/settings/tasks — how this workspace's tasks read: the key
// prefix (DIM in DIM-142), what each status category is called, how many days
// an active project may go without an update, and its labels. Editors change
// them; everyone else reads them. The server is the gate — a prefix some task
// already carries is refused (422), and that refusal is shown as it is said.
import { useCallback, useEffect, useState, type JSX } from 'react'
import { useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { cn } from 'canopy-ui/lib'

import {
  createLabel,
  deleteLabel,
  getTaskSettings,
  listLabels,
  setTaskSettings,
  updateLabel,
  type Label,
  type TaskSettings,
} from '@/api/work'
import { LabelChip } from '@/components/work/bits'
import { DEFAULT_CATEGORY_NAMES, LABEL_COLORS, type Category } from '@/components/work/meta'
import { invalidateDirectory } from '@/components/work/useDirectory'
import { roleAllows } from '@/lib/workspaceRoles'
import { useWorkspaceRole } from '@/workspace/WorkspaceProvider'

const CATEGORIES = Object.keys(DEFAULT_CATEGORY_NAMES) as Category[]

const CATEGORY_HINT: Record<Category, string> = {
  triage: 'suggested, waiting for a decision',
  backlog: 'accepted, not now',
  unstarted: 'accepted, to do',
  started: 'being worked on',
  completed: 'finished',
  canceled: 'declined or a duplicate',
}

const input =
  'rounded border border-input bg-input px-2 py-1 text-[13px] text-foreground placeholder:text-placeholder disabled:opacity-60'

const message = (e: unknown, fallback: string) => (e instanceof Error && e.message ? e.message : fallback)

export function WorkspaceTaskSettingsPage(): JSX.Element {
  const { workspace: slug = '' } = useParams()
  const canEdit = roleAllows(useWorkspaceRole(slug), 'agent.work')
  const [saved, setSaved] = useState<TaskSettings | null>(null)
  const [prefix, setPrefix] = useState('')
  const [names, setNames] = useState<Record<string, string>>({})
  const [cadence, setCadence] = useState('7')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const take = useCallback((s: TaskSettings) => {
    setSaved(s)
    setPrefix(s.prefix)
    setNames({ ...(s.status_names ?? {}) })
    setCadence(String(s.update_cadence_days))
  }, [])

  useEffect(() => {
    getTaskSettings(slug)
      .then(take)
      .catch((e: unknown) => setError(message(e, 'Could not load the task settings')))
  }, [slug, take])

  async function save() {
    if (!saved) return
    setBusy(true)
    setError(null)
    setNotice(null)
    const days = Number.parseInt(cadence, 10)
    const body = {
      // Only when it changed: an unchanged prefix must not trip the "in use" refusal.
      ...(prefix.trim().toUpperCase() !== saved.prefix ? { prefix: prefix.trim().toUpperCase() } : {}),
      // Every category, blanks included: a blank name restores the default.
      status_names: Object.fromEntries(CATEGORIES.map((c) => [c, (names[c] ?? '').trim()])),
      update_cadence_days: Number.isFinite(days) && days > 0 ? days : saved.update_cadence_days,
    }
    try {
      take(await setTaskSettings(slug, body))
      setNotice('Saved.')
    } catch (e) {
      setError(message(e, 'Could not save the task settings'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <section>
        <h2 className="text-sm font-semibold text-foreground">Tasks</h2>
        <p className="mt-1 text-[13px] text-muted-foreground">
          How this workspace&apos;s tasks read, for everyone in it.
          {!canEdit && ' Only an editor can change these.'}
        </p>
      </section>

      {error && (
        <p role="alert" className="rounded border border-destructive/30 bg-destructive/10 px-3 py-1.5 text-[12.5px] text-destructive">
          {error}
        </p>
      )}
      {!saved && !error && <p className="text-[13px] text-muted-foreground">Loading…</p>}

      {saved && (
        <form
          className="flex flex-col gap-5 rounded-lg border border-border bg-card p-4"
          onSubmit={(e) => {
            e.preventDefault()
            void save()
          }}
        >
          <fieldset disabled={!canEdit || busy} className="flex flex-col gap-5">
            <label className="flex flex-col gap-1 text-[13px] text-foreground">
              <span className="font-medium">Key prefix</span>
              <span className="text-[12px] text-muted-foreground">
                The letters before every task&apos;s number ({saved.prefix}-142). It can change only while no task carries it.
              </span>
              <input
                value={prefix}
                onChange={(e) => setPrefix(e.target.value.toUpperCase())}
                aria-label="Key prefix"
                maxLength={10}
                className={cn(input, 'w-32 font-mono uppercase')}
              />
            </label>

            <div className="flex flex-col gap-1">
              <span className="text-[13px] font-medium text-foreground">Status names</span>
              <span className="text-[12px] text-muted-foreground">What each kind of status is called here. Blank uses the default.</span>
              <table className="mt-1 w-full max-w-lg text-[13px]">
                <tbody>
                  {CATEGORIES.map((c) => (
                    <tr key={c} className="border-b border-border last:border-0">
                      <td className="w-40 py-1 pr-3 text-foreground-secondary">
                        {DEFAULT_CATEGORY_NAMES[c]}
                        <div className="text-[11px] text-muted-foreground">{CATEGORY_HINT[c]}</div>
                      </td>
                      <td className="py-1">
                        <input
                          value={names[c] ?? ''}
                          onChange={(e) => setNames((n) => ({ ...n, [c]: e.target.value }))}
                          placeholder={DEFAULT_CATEGORY_NAMES[c]}
                          aria-label={`Name for ${DEFAULT_CATEGORY_NAMES[c]}`}
                          maxLength={40}
                          className={cn(input, 'w-full')}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <label className="flex flex-col gap-1 text-[13px] text-foreground">
              <span className="font-medium">Project update cadence</span>
              <span className="text-[12px] text-muted-foreground">
                Days an active project may go without an update before it shows &ldquo;update missing&rdquo;.
              </span>
              <span className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={365}
                  value={cadence}
                  onChange={(e) => setCadence(e.target.value)}
                  aria-label="Update cadence in days"
                  className={cn(input, 'w-20')}
                />
                <span className="text-[12px] text-muted-foreground">days</span>
              </span>
            </label>
          </fieldset>
          {canEdit && (
            <div className="flex items-center gap-3">
              <Button type="submit" size="sm" disabled={busy}>
                {busy ? 'Saving…' : 'Save'}
              </Button>
              {notice && <span className="text-[12px] text-success">{notice}</span>}
            </div>
          )}
        </form>
      )}

      <Labels slug={slug} canEdit={canEdit} />
    </div>
  )
}

function Labels({ slug, canEdit }: { slug: string; canEdit: boolean }): JSX.Element {
  const [labels, setLabels] = useState<Label[] | null>(null)
  const [draft, setDraft] = useState('')
  const [color, setColor] = useState<string>('muted')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    listLabels(slug)
      .then((ls) => setLabels([...ls].sort((a, b) => a.name.localeCompare(b.name))))
      .catch((e: unknown) => setError(message(e, 'Could not load labels')))
  }, [slug])
  useEffect(() => load(), [load])

  async function run(fn: () => Promise<unknown>): Promise<boolean> {
    setError(null)
    try {
      await fn()
      invalidateDirectory(slug)
      load()
      return true
    } catch (e) {
      setError(message(e, 'The server refused'))
      return false
    }
  }

  return (
    <section className="flex flex-col gap-2">
      <div>
        <h2 className="text-sm font-semibold text-foreground">Labels</h2>
        <p className="mt-1 text-[13px] text-muted-foreground">The labels anyone here can put on a task.</p>
      </div>
      {error && (
        <p role="alert" className="text-[12.5px] text-destructive">
          {error}
        </p>
      )}
      <div className="overflow-hidden rounded-lg border border-border bg-card">
        <table className="w-full text-[13px]">
          <thead>
            <tr className="border-b border-border text-left text-[11px] uppercase tracking-wide text-muted-foreground">
              <th className="px-3 py-1.5 font-medium">Label</th>
              <th className="py-1.5 font-medium">Colour</th>
              <th className="py-1.5 pr-3 text-right font-medium">Tasks</th>
              {canEdit && <th className="w-16" />}
            </tr>
          </thead>
          <tbody>
            {labels === null && (
              <tr>
                <td colSpan={4} className="px-3 py-3 text-muted-foreground">
                  Loading…
                </td>
              </tr>
            )}
            {labels?.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-3 text-muted-foreground">
                  No labels yet.
                </td>
              </tr>
            )}
            {labels?.map((l) => (
              <LabelRow key={l.id} label={l} canEdit={canEdit} run={run} />
            ))}
          </tbody>
        </table>
        {canEdit && (
          <form
            className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2"
            onSubmit={(e) => {
              e.preventDefault()
              const name = draft.trim()
              if (!name) return
              void run(() => createLabel(slug, name, color)).then((ok) => ok && setDraft(''))
            }}
          >
            <input
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder="New label…"
              aria-label="New label name"
              className={cn(input, 'w-48')}
            />
            <ColorPicker value={color} onChange={setColor} label="New label colour" />
            <Button type="submit" size="sm" variant="outline" disabled={!draft.trim()}>
              Add label
            </Button>
          </form>
        )}
      </div>
    </section>
  )
}

function LabelRow({
  label,
  canEdit,
  run,
}: {
  label: Label
  canEdit: boolean
  run: (fn: () => Promise<unknown>) => Promise<boolean>
}): JSX.Element {
  const [name, setName] = useState(label.name)
  const rename = () => {
    const next = name.trim()
    if (!next || next === label.name) return setName(label.name)
    void run(() => updateLabel(label.id, { name: next }))
  }
  return (
    <tr className="border-b border-border last:border-0">
      <td className="px-3 py-1">
        {canEdit ? (
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            onBlur={rename}
            onKeyDown={(e) => e.key === 'Enter' && (e.preventDefault(), rename())}
            aria-label={`Rename ${label.name}`}
            className={cn(input, 'w-48')}
          />
        ) : (
          <LabelChip name={label.name} color={label.color} />
        )}
      </td>
      <td className="py-1">
        {canEdit ? (
          <ColorPicker
            value={label.color}
            onChange={(c) => void run(() => updateLabel(label.id, { color: c }))}
            label={`Colour of ${label.name}`}
          />
        ) : (
          <span className="text-[12px] capitalize text-muted-foreground">{label.color}</span>
        )}
      </td>
      <td className="py-1 pr-3 text-right tabular-nums text-muted-foreground">{label.task_count}</td>
      {canEdit && (
        <td className="py-1 pr-2 text-right">
          <button
            type="button"
            onClick={() => {
              const used = label.task_count ? ` It comes off ${label.task_count} task${label.task_count === 1 ? '' : 's'}.` : ''
              if (window.confirm(`Delete the label “${label.name}”?${used}`)) void run(() => deleteLabel(label.id))
            }}
            className="rounded px-1.5 py-0.5 text-[12px] text-destructive hover:bg-destructive/10"
          >
            Delete
          </button>
        </td>
      )}
    </tr>
  )
}

function ColorPicker({ value, onChange, label }: { value: string; onChange: (c: string) => void; label: string }): JSX.Element {
  return (
    <span role="radiogroup" aria-label={label} className="inline-flex items-center gap-1">
      {LABEL_COLORS.map((c) => (
        <button
          key={c}
          type="button"
          role="radio"
          aria-checked={value === c}
          aria-label={c}
          title={c}
          onClick={() => c !== value && onChange(c)}
          className={cn('rounded p-0.5', value === c ? 'ring-1 ring-primary' : 'opacity-70 hover:opacity-100')}
        >
          <LabelChip name={c.charAt(0).toUpperCase()} color={c} />
        </button>
      ))}
    </span>
  )
}
