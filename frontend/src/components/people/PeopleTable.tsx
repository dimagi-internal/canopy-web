import { useId, useState, type FormEvent, type JSX, type ReactNode } from 'react'
import { Button, Input, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'

import { RoleControl, RoleSelect, ROLE_TEXT_CLASS } from './RoleSelect'
import type { RoleOption } from './roles'

// The shared "who has access" table: person · role · [extra columns] · remove.
// Every surface that lists people with a role renders through this, so the role
// is always in the same column, changed the same way (pick from the dropdown;
// it saves immediately), and explained the same way when it cannot be changed.
//
// Presentation only. Each caller decides who may change what (the server is
// still the authority) and passes the result in as `editable` / `onRemove`.

export interface PersonRow {
  key: string | number
  /** Primary line — a name, or the email when there is no name. */
  name: string
  /** Shown muted beside the name when it differs from it. */
  email?: string | null
  /** A small muted line under the person (e.g. "workspace editor"). */
  detail?: ReactNode
  role: string
  /** The roles this row may be changed to. Ignored when not editable. */
  options: RoleOption[]
  /** May the viewer change this row's role? */
  editable: boolean
  /** Show the dropdown but disable it (e.g. the only owner). */
  roleDisabled?: boolean
  /** Fixed-role display text; defaults to the matching option's label. */
  roleLabel?: string
  /** Why the role is what it is / why it is fixed. Short, muted. */
  why?: string | null
  onRoleChange?: (next: string) => Promise<unknown>
  /** Present ⇒ a Remove button on the row. */
  onRemove?: () => Promise<unknown>
  /** Cells for the table's `extraColumns`, in order. */
  extra?: ReactNode[]
  testId?: string
}

function errorText(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

export function PeopleTable({
  rows,
  extraColumns = [],
  actions,
  roleHeader = 'Role',
}: {
  rows: PersonRow[]
  extraColumns?: string[]
  /** Show the trailing actions column; defaults to "any row is removable". */
  actions?: boolean
  roleHeader?: string
}): JSX.Element {
  const uid = useId()
  const [saving, setSaving] = useState<string | number | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const showActions = actions ?? rows.some((r) => r.onRemove)

  const run = async (row: PersonRow, fn: () => Promise<unknown>, fallback: string) => {
    setSaving(row.key)
    setErrors((prev) => {
      const next = { ...prev }
      delete next[String(row.key)]
      return next
    })
    try {
      await fn()
    } catch (e) {
      setErrors((prev) => ({ ...prev, [String(row.key)]: errorText(e, fallback) }))
    } finally {
      setSaving(null)
    }
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Person</TableHead>
          <TableHead>{roleHeader}</TableHead>
          {extraColumns.map((c) => (
            <TableHead key={c}>{c}</TableHead>
          ))}
          {showActions && <TableHead className="text-right">Actions</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((r) => {
          const label = r.email ?? r.name
          const err = errors[String(r.key)]
          const busy = saving === r.key
          return (
            <TableRow key={r.key} data-testid={r.testId}>
              <TableCell className="whitespace-normal align-top text-foreground">
                {r.name}
                {r.email && r.email !== r.name && <span className="text-muted-foreground"> ({r.email})</span>}
                {r.detail && <div className="text-[11px] text-muted-foreground">{r.detail}</div>}
              </TableCell>
              <TableCell className="whitespace-normal align-top">
                <RoleControl
                  value={r.role}
                  options={r.options}
                  editable={r.editable}
                  label={r.roleLabel}
                  ariaLabel={`Change role for ${label}`}
                  disabled={r.roleDisabled || busy}
                  onChange={
                    r.onRoleChange
                      ? (next) => {
                          const change = r.onRoleChange as (n: string) => Promise<unknown>
                          void run(r, () => change(next), 'Failed to change role')
                        }
                      : undefined
                  }
                  why={r.why}
                  whyId={`${uid}-why-${r.key}`}
                />
                {err && (
                  <p role="alert" className="m-0 mt-0.5 text-[11px] normal-case text-destructive">
                    {err}
                  </p>
                )}
              </TableCell>
              {extraColumns.map((c, i) => (
                <TableCell key={c} className="whitespace-normal align-top">
                  {r.extra?.[i]}
                </TableCell>
              ))}
              {showActions && (
                <TableCell className="align-top text-right">
                  {r.onRemove && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      disabled={busy}
                      onClick={() => {
                        const remove = r.onRemove as () => Promise<unknown>
                        void run(r, remove, 'Failed to remove')
                      }}
                      aria-label={`Remove ${label}`}
                    >
                      Remove
                    </Button>
                  )}
                </TableCell>
              )}
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}

/**
 * The add row: email + role + button, using the same role control as the
 * table. With a single possible role it shows that role as fixed text rather
 * than a one-option dropdown. `onAdd` rejecting shows the server's own words.
 */
export function AddPersonForm({
  options,
  defaultRole,
  onAdd,
  submitLabel = 'Add',
  busyLabel = 'Adding…',
  placeholder = 'teammate@dimagi.com',
  submitTestId,
  errorTestId,
  autoFocus = false,
}: {
  options: RoleOption[]
  defaultRole?: string
  onAdd: (email: string, role: string) => Promise<unknown>
  submitLabel?: string
  busyLabel?: string
  placeholder?: string
  submitTestId?: string
  errorTestId?: string
  autoFocus?: boolean
}): JSX.Element {
  const uid = useId()
  const [email, setEmail] = useState('')
  const [role, setRole] = useState(defaultRole ?? options[0]?.value ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The chosen role may have dropped out of the options (the viewer's own
  // role changed); fall back rather than submit something they cannot grant.
  const current = options.some((o) => o.value === role) ? role : (options[0]?.value ?? '')

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    const value = email.trim()
    if (!value || busy) return
    setBusy(true)
    setError(null)
    try {
      await onAdd(value, current)
      setEmail('')
    } catch (e2) {
      setError(errorText(e2, 'That did not work.'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="flex flex-col gap-1">
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-[12rem] flex-1">
          <label htmlFor={`${uid}-email`} className="mb-1 block text-[11px] text-muted-foreground">
            Email
          </label>
          <Input
            id={`${uid}-email`}
            type="email"
            required
            autoFocus={autoFocus}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={placeholder}
          />
        </div>
        <div>
          {options.length > 1 ? (
            <label htmlFor={`${uid}-role`} className="mb-1 block text-[11px] text-muted-foreground">
              Role
            </label>
          ) : (
            <span className="mb-1 block text-[11px] text-muted-foreground">Role</span>
          )}
          {options.length > 1 ? (
            <RoleSelect id={`${uid}-role`} value={current} options={options} onChange={setRole} />
          ) : (
            <span className={ROLE_TEXT_CLASS}>
              {options[0]?.label ?? ''}
            </span>
          )}
        </div>
        <Button type="submit" disabled={busy || !email.trim()} data-testid={submitTestId}>
          {busy ? busyLabel : submitLabel}
        </Button>
      </div>
      {error && (
        <p role="alert" className="m-0 text-[12px] text-destructive" data-testid={errorTestId}>
          {error}
        </p>
      )}
    </form>
  )
}
