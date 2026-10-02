import type { JSX } from 'react'

import type { RoleOption } from './roles'

// ONE role control for every "who has access" surface — workspace members, an
// agent's roster, a runner's administrators, a chat's people.
//
// They used to be four different shapes (a <select> here, a "Make admin" link
// there, a bare list elsewhere), so the same question — "what is this person's
// role, and can I change it?" — had to be re-learned on every page. Now the role
// always sits in the same column: a dropdown when the viewer may change it,
// otherwise the same words in the same place as plain text, with a short muted
// line saying why it is fixed.

/** The select's look — the members page's, which every surface now shares. */
export const ROLE_SELECT_CLASS =
  'h-8 rounded-lg border border-input bg-input px-2 text-sm text-foreground capitalize disabled:cursor-not-allowed disabled:opacity-50'

/** The fixed role: same height, size and colour as the select, minus the chrome. */
export const ROLE_TEXT_CLASS = 'inline-flex h-8 items-center text-sm text-foreground capitalize'

export function RoleSelect({
  value,
  options,
  onChange,
  ariaLabel,
  disabled = false,
  id,
  describedBy,
}: {
  value: string
  options: RoleOption[]
  onChange: (next: string) => void
  ariaLabel?: string
  disabled?: boolean
  id?: string
  describedBy?: string
}): JSX.Element {
  // A value the viewer may not grant (e.g. an admin looking at an owner they
  // may still demote) must still show as the current selection.
  const all = options.some((o) => o.value === value) ? options : [{ value, label: value }, ...options]
  return (
    <select
      id={id}
      aria-label={ariaLabel}
      aria-describedby={describedBy}
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className={ROLE_SELECT_CLASS}
    >
      {all.map((o) => (
        <option key={o.value} value={o.value} className="capitalize">
          {o.label}
        </option>
      ))}
    </select>
  )
}

/**
 * The role cell: a RoleSelect when `editable` and there is a choice to make,
 * otherwise the role's label as plain text in the same spot. `why` renders
 * as a visible muted line (not a hover-only title — a disabled select is not
 * focusable, so a tooltip would be invisible to screen readers and touch).
 */
export function RoleControl({
  value,
  options,
  editable,
  onChange,
  ariaLabel,
  disabled = false,
  why,
  whyId,
  label,
}: {
  value: string
  options: RoleOption[]
  editable: boolean
  onChange?: (next: string) => void
  ariaLabel?: string
  disabled?: boolean
  why?: string | null
  whyId?: string
  /** Display text when fixed; defaults to the option's label, then the value. */
  label?: string
}): JSX.Element {
  const asSelect = editable && !!onChange && options.length > 1
  const text = label ?? options.find((o) => o.value === value)?.label ?? value
  return (
    <div className="flex flex-col items-start gap-0.5">
      {asSelect ? (
        <RoleSelect
          value={value}
          options={options}
          onChange={onChange}
          ariaLabel={ariaLabel}
          disabled={disabled}
          describedBy={why ? whyId : undefined}
        />
      ) : (
        <span className={ROLE_TEXT_CLASS}>{text}</span>
      )}
      {why && (
        <span id={whyId} className="text-[11px] normal-case text-muted-foreground">
          {why}
        </span>
      )}
    </div>
  )
}
