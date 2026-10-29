import { useEffect, useState, type JSX } from 'react'
import {
  addOperator,
  listOperators,
  removeOperator,
  setOperatorAlertMail,
  type AlertMail,
  type OperatorOut,
} from '@/api/harness'

// Superusers (apps/system/operators.py): the people who hold the fleet controls above.
// Being one gives NO workspace access — that is still a membership, set on a
// workspace's Members page. Which alert mail reaches each person is a third, separate
// choice, set here for them (or by them, beside their notification settings):
// "All" is everything their roles make them a recipient of; "Only what's handed to me"
// skips the role fan-out (blocking alerts, access requests) unless nobody else would
// be told. Adding and removing ask for a fresh passkey (the API client prompts).
// Shown only to a superuser; the page passes `show` from the fleet hold's flags.
export function OperatorsCard({ show }: { show: boolean }): JSX.Element | null {
  const [ops, setOps] = useState<OperatorOut[] | null>(null)
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!show) return
    let cancelled = false
    listOperators()
      .then((o) => !cancelled && setOps(o))
      .catch(() => !cancelled && setOps(null))
    return () => {
      cancelled = true
    }
  }, [show])

  if (!show || ops === null) return null

  const run = (call: Promise<unknown>, then: () => Promise<void> | void) => {
    setBusy(true)
    setErr(null)
    call
      .then(then)
      .catch((e: unknown) => setErr(e instanceof Error ? e.message : 'could not change superusers'))
      .finally(() => setBusy(false))
  }
  const reload = async () => setOps(await listOperators())

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="operators-card">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Superusers</span>
      <p className="text-[12px] text-muted-foreground">
        Superusers can hold the fleet and stop everything. It gives no access to any workspace — add them on a
        workspace&apos;s Members page for that. Alert mail is per person.
      </p>
      <ul className="flex flex-col divide-y divide-border">
        {ops.map((o) => (
          <li key={o.user_id} className="flex flex-wrap items-center gap-2 py-1.5" data-testid="operator-row">
            <span className="min-w-0 basis-full truncate text-[13px] text-foreground sm:basis-auto sm:flex-1">
              {o.name || o.email}
              {o.name && <span className="text-muted-foreground"> · {o.email}</span>}
              {o.is_you && <span className="text-muted-foreground"> (you)</span>}
            </span>
            <label className="flex items-center gap-1 text-[12px] text-muted-foreground">
              Alert mail
              <select
                value={o.alert_mail}
                disabled={busy}
                data-testid="operator-alert-mail"
                onChange={(e) =>
                  run(setOperatorAlertMail(o.user_id, e.target.value as AlertMail), reload)
                }
                className="rounded-md border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
              >
                <option value="all">All</option>
                <option value="assigned">Only what&apos;s handed to me</option>
              </select>
            </label>
            {ops.length > 1 && (
              <button
                type="button"
                disabled={busy}
                data-testid="operator-remove"
                onClick={() => run(removeOperator(o.user_id), reload)}
                className="rounded-md border border-border px-2 py-0.5 text-[12px] text-foreground hover:bg-muted disabled:opacity-50"
              >
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>
      <form
        className="flex gap-1.5"
        onSubmit={(e) => {
          e.preventDefault()
          if (!email.trim()) return
          run(addOperator(email.trim()), async () => {
            setEmail('')
            await reload()
          })
        }}
      >
        <input
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="Add by email (they must have signed in once)"
          data-testid="operator-email"
          className="min-w-0 flex-1 rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground placeholder:text-placeholder"
        />
        <button
          type="submit"
          disabled={busy || !email.trim()}
          data-testid="operator-add"
          className="rounded-md bg-primary px-2.5 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
        >
          Add
        </button>
      </form>
      {err && <p className="text-[12px] text-destructive" data-testid="operators-error">{err}</p>}
    </div>
  )
}
