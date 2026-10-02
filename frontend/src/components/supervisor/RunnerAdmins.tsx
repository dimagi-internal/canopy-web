import { useCallback, useEffect, useRef, useState, type JSX } from 'react'
import { AddPersonForm, PeopleTable, type PersonRow } from '@/components/people/PeopleTable'
import type { RoleOption } from '@/components/people/roles'
import {
  grantRunnerAdmin,
  listRunnerAdmins,
  revokeRunnerAdmin,
  type RunnerAdmin,
} from '@/api/harness'

// A box has one grantable role; the pairer is shown, never granted.
const ADMIN_OPTIONS: RoleOption[] = [{ value: 'admin', label: 'Admin' }]
const PAIRER_OPTIONS: RoleOption[] = [{ value: 'pairer', label: 'Pairer' }]

// Who may administer this box, and the form to say so.
//
// `GET/POST/DELETE /runners/{id}/admins` shipped with the grant itself and had no
// page, so the only way to add someone was a curl with the pairer's token — which
// is exactly the shape of thing that does not happen, and then a box has one
// human who can fix it. The grant exists because that single point of failure
// already bit once (2026-09-08, a signed-out cloud runner).
//
// Two tiers, mirrored here rather than guessed at:
//   * LISTING is open to anyone who already administers the box. A non-admin gets
//     a 404 from the server, which this renders as "you can't see this", never as
//     "nobody administers it" — an empty list and a refused read must not look
//     the same.
//   * GRANTING and REVOKING stay with the PAIRER (`can_manage`), so a grantee
//     cannot mint more grantees. The add row and Remove are simply absent
//     otherwise. Roles render through the shared PeopleTable, like every other
//     people surface.
export function RunnerAdmins({
  runnerId,
  canManage,
  pairedByEmail,
}: {
  runnerId: string
  /** Is the viewer the pairer — the only tier that may grant. */
  canManage: boolean
  pairedByEmail?: string | null
}): JSX.Element {
  const [admins, setAdmins] = useState<RunnerAdmin[] | null>(null)
  const [refused, setRefused] = useState(false)
  const alive = useRef(true)
  useEffect(() => () => { alive.current = false }, [])

  const load = useCallback(() => {
    listRunnerAdmins(runnerId)
      .then((rows) => { if (alive.current) { setAdmins(rows); setRefused(false) } })
      .catch(() => { if (alive.current) { setAdmins([]); setRefused(true) } })
  }, [runnerId])

  useEffect(load, [load])

  // Both reject with the server's own words, which the shared table / add row
  // show inline: "no account with email …" and "not a member of the workspace
  // this runner belongs to" are the two real failures, and both are fixable by
  // the person reading them. Paraphrasing would lose that.
  const grant = async (email: string) => {
    await grantRunnerAdmin(runnerId, email)
    if (alive.current) load()
  }

  const revoke = async (admin: RunnerAdmin) => {
    await revokeRunnerAdmin(runnerId, admin.user_id)
    if (alive.current) load()
  }

  // The pairer heads the table as a fixed row: they are why the others exist,
  // and the only one who may change the list.
  const rows: PersonRow[] = [
    ...(pairedByEmail
      ? [{
          key: 'pairer',
          testId: 'runner-admin-pairer',
          name: pairedByEmail,
          role: 'pairer',
          roleLabel: 'Pairer',
          options: PAIRER_OPTIONS,
          editable: false,
          why: 'paired this runner',
        } satisfies PersonRow]
      : []),
    ...(admins ?? []).map((a): PersonRow => ({
      key: a.user_id,
      testId: `runner-admin-${a.user_id}`,
      name: a.email,
      role: 'admin',
      options: ADMIN_OPTIONS,
      editable: false,
      why: a.granted_by_email ? `granted by ${a.granted_by_email}` : null,
      onRemove: canManage ? () => revoke(a) : undefined,
    })),
  ]

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3" data-testid="runner-admins">
      <span className="text-[11px] uppercase tracking-wide text-muted-foreground">Administrators</span>

      {refused ? (
        <p className="text-[12px] text-muted-foreground" data-testid="runner-admins-refused">
          Only someone who already administers this box can see its list.
        </p>
      ) : admins === null ? (
        <div className="h-6 w-40 animate-pulse rounded-md bg-muted" data-testid="runner-admins-loading" />
      ) : (
        <>
          {rows.length > 0 && <PeopleTable rows={rows} actions={canManage} />}
          {admins.length === 0 && (
            <p className="text-[12px] text-muted-foreground" data-testid="runner-admins-empty">
              Nobody but the pairer. If they lose access to this box, nobody can re-authenticate it.
            </p>
          )}
        </>
      )}

      {canManage && (
        <AddPersonForm
          options={ADMIN_OPTIONS}
          onAdd={(email) => grant(email)}
          placeholder="colleague@dimagi.com"
          busyLabel="Saving…"
          submitTestId="runner-admins-grant"
          errorTestId="runner-admins-error"
        />
      )}

      <p className="text-[11px] text-foreground-subtle">
        An administrator can set this box's credentials, sign it back in, and send work to it — on a
        cloud runner, that is what lets someone move their own queued work here when their laptop is
        offline. They cannot add or remove administrators; only the pairer can.
      </p>
    </div>
  )
}
