import { useState, type FormEvent, type JSX } from 'react'
import { Badge, Button, Input, Textarea } from 'canopy-ui/ui'
import { WorkbenchSubHeader } from 'canopy-ui'
import {
  addSystemSender,
  createSystemAccount,
  deleteSystemAccount,
  removeSystemSender,
  updateSystemAccount,
  type SystemAccountOut,
  type SystemRole,
} from '@/api/workspaces'

// System accounts (apps/workspaces/system_accounts.py, canopy-web#1253): an
// automated sender — CloudWatch alarms, CI — given a member's standing so its
// mail makes agents do work like a person with that role would. It can never
// sign in. Aligned mail from a bound address, in THIS workspace, whose subject
// matches the pattern, becomes its turn; anything else stays an outside contact.

const ROLE_TEXT: Record<SystemRole, string> = {
  editor: 'Editor — its mail runs in an agent’s whole profile, always manual.',
  viewer: 'Viewer — its mail gets only what each agent offers members.',
}

function errorText(e: unknown, fallback: string): string {
  return e instanceof Error && e.message ? e.message : fallback
}

interface Props {
  slug: string
  accounts: SystemAccountOut[] | null
  canManage: boolean
  onChange: (next: SystemAccountOut[]) => void
}

export function SystemAccountsSection({ slug, accounts, canManage, onChange }: Props): JSX.Element {
  const [error, setError] = useState<string | null>(null)
  const list = accounts ?? []

  function replace(updated: SystemAccountOut) {
    onChange(list.map((a) => (a.id === updated.id ? updated : a)))
  }

  async function run(fn: () => Promise<void>, fallback: string) {
    setError(null)
    try {
      await fn()
    } catch (e) {
      setError(errorText(e, fallback))
    }
  }

  return (
    <div className="mb-8" data-testid="system-accounts">
      <WorkbenchSubHeader title="System accounts" count={accounts?.length} />
      <p className="mb-3 text-[12px] text-muted-foreground">
        Automated senders, such as alarm mail, that act as a member here. They can never sign in. Mail
        becomes theirs only when it is verified (DKIM/DMARC aligned), from a bound address, and matches
        the subject pattern.
      </p>
      {error && <div className="mb-3 text-sm text-destructive">{error}</div>}
      {accounts === null ? (
        <div className="h-16 animate-pulse rounded-lg bg-muted" />
      ) : list.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No system accounts.</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-3 p-0">
          {list.map((a) => (
            <li key={a.id} className="rounded-lg border border-border bg-card p-4" data-testid={`system-account-${a.id}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-foreground">{a.name}</span>
                <Badge variant="secondary">System</Badge>
                <span className="text-[12px] capitalize text-muted-foreground">{a.role ?? 'not a member'}</span>
                {a.disabled && <Badge variant="outline">Disabled</Badge>}
                {canManage && (
                  <div className="ml-auto flex gap-1">
                    <select
                      aria-label={`Role for ${a.name}`}
                      className="h-8 rounded-md border border-border bg-background px-2 text-[12px] capitalize"
                      value={a.role ?? 'editor'}
                      onChange={(e) =>
                        void run(async () => {
                          replace(await updateSystemAccount(slug, a.id, { role: e.target.value as SystemRole }))
                        }, 'Failed to change role')
                      }
                    >
                      <option value="editor">editor</option>
                      <option value="viewer">viewer</option>
                    </select>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        void run(async () => {
                          replace(await updateSystemAccount(slug, a.id, { disabled: !a.disabled }))
                        }, 'Failed to update')
                      }
                    >
                      {a.disabled ? 'Enable' : 'Disable'}
                    </Button>
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        void run(async () => {
                          if (!window.confirm(`Delete system account “${a.name}”? Disabling keeps its name on past turns.`)) return
                          await deleteSystemAccount(slug, a.id)
                          onChange(list.filter((x) => x.id !== a.id))
                        }, 'Failed to delete')
                      }
                    >
                      Delete
                    </Button>
                  </div>
                )}
              </div>
              {a.description && <p className="mt-1 text-[12px] text-foreground-secondary">{a.description}</p>}
              <div className="mt-3">
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                  Bound senders
                </div>
                {a.senders.length === 0 ? (
                  <p className="text-[12px] text-warning">No senders bound — no mail reaches this account yet.</p>
                ) : (
                  <ul className="m-0 flex list-none flex-col gap-1 p-0">
                    {a.senders.map((s) => (
                      <li key={s.id} className="flex items-center gap-2 text-[12px]">
                        <code className="rounded bg-muted px-1.5 py-0.5 text-foreground">{s.address}</code>
                        <span className="text-muted-foreground">
                          {s.subject_pattern ? (
                            <>
                              subject matches <code className="rounded bg-muted px-1.5 py-0.5">{s.subject_pattern}</code>
                            </>
                          ) : (
                            'any subject'
                          )}
                        </span>
                        {canManage && (
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            className="ml-auto"
                            aria-label={`Unbind ${s.address} from ${a.name}`}
                            onClick={() =>
                              void run(async () => {
                                await removeSystemSender(slug, a.id, s.id)
                                replace({ ...a, senders: a.senders.filter((x) => x.id !== s.id) })
                              }, 'Failed to remove sender')
                            }
                          >
                            Unbind
                          </Button>
                        )}
                      </li>
                    ))}
                  </ul>
                )}
                {canManage && (
                  <AddSenderForm
                    accountName={a.name}
                    onAdd={async (address, pattern) => {
                      const s = await addSystemSender(slug, a.id, address, pattern)
                      replace({ ...a, senders: [...a.senders.filter((x) => x.id !== s.id), s] })
                    }}
                  />
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {canManage && (
        <CreateSystemAccountForm
          onCreate={async (body) => {
            const created = await createSystemAccount(slug, body)
            onChange([...list, created].sort((x, y) => x.name.localeCompare(y.name)))
          }}
        />
      )}
    </div>
  )
}

function AddSenderForm({
  accountName,
  onAdd,
}: {
  accountName: string
  onAdd: (address: string, pattern: string) => Promise<void>
}): JSX.Element {
  const [address, setAddress] = useState('')
  const [pattern, setPattern] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await onAdd(address.trim(), pattern)
      setAddress('')
      setPattern('')
    } catch (err) {
      setError(errorText(err, 'Failed to add sender'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="mt-2 flex flex-wrap items-center gap-2" onSubmit={(e) => void submit(e)}>
      <Input
        aria-label={`Sender address for ${accountName}`}
        className="h-8 w-64 text-[12px]"
        type="email"
        placeholder="no-reply@sns.amazonaws.com"
        value={address}
        onChange={(e) => setAddress(e.target.value)}
        required
      />
      <Input
        aria-label={`Subject pattern for ${accountName}`}
        className="h-8 w-64 font-mono text-[12px]"
        placeholder='optional regex, e.g. ^(ALARM|OK): "labs-'
        value={pattern}
        onChange={(e) => setPattern(e.target.value)}
      />
      <Button type="submit" size="sm" variant="outline" disabled={busy || !address.trim()}>
        {busy ? 'Binding…' : 'Bind sender'}
      </Button>
      {error && <span className="text-[12px] text-destructive">{error}</span>}
    </form>
  )
}

function CreateSystemAccountForm({
  onCreate,
}: {
  onCreate: (body: { name: string; description: string; role: SystemRole; senders: { address: string; subject_pattern: string }[] }) => Promise<void>
}): JSX.Element {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [role, setRole] = useState<SystemRole>('editor')
  const [address, setAddress] = useState('')
  const [pattern, setPattern] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await onCreate({
        name: name.trim(),
        description,
        role,
        senders: address.trim() ? [{ address: address.trim(), subject_pattern: pattern }] : [],
      })
      setName('')
      setDescription('')
      setRole('editor')
      setAddress('')
      setPattern('')
    } catch (err) {
      setError(errorText(err, 'Failed to create system account'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      className="mt-4 rounded-lg border border-border bg-card p-5"
      onSubmit={(e) => void submit(e)}
      data-testid="create-system-account"
    >
      <h2 className="mb-3 text-sm font-semibold text-foreground">Add a system account</h2>
      <div className="grid gap-2 sm:grid-cols-2">
        <Input aria-label="Name" placeholder="AWS CloudWatch alarms" value={name} onChange={(e) => setName(e.target.value)} required />
        <select
          aria-label="Role"
          className="h-9 rounded-md border border-border bg-background px-2 text-[13px]"
          value={role}
          onChange={(e) => setRole(e.target.value as SystemRole)}
        >
          <option value="editor">{ROLE_TEXT.editor}</option>
          <option value="viewer">{ROLE_TEXT.viewer}</option>
        </select>
        <Input
          aria-label="Sender address"
          type="email"
          placeholder="no-reply@sns.amazonaws.com (optional now)"
          value={address}
          onChange={(e) => setAddress(e.target.value)}
        />
        <Input
          aria-label="Subject pattern"
          className="font-mono"
          placeholder='^(ALARM|OK): "labs-   (optional regex)'
          value={pattern}
          onChange={(e) => setPattern(e.target.value)}
        />
      </div>
      <Textarea
        aria-label="Description"
        className="mt-2"
        rows={2}
        placeholder="What sends this mail, and which agent handles it"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <p className="mt-2 text-[12px] text-muted-foreground">
        A shared address (every AWS customer’s alarms come from the same one) only becomes this account’s inside
        this workspace — narrow it with a subject pattern so unrelated mail stays an outside contact.
      </p>
      <div className="mt-3 flex items-center gap-2">
        <Button type="submit" size="sm" disabled={busy || !name.trim()}>
          {busy ? 'Creating…' : 'Create system account'}
        </Button>
        {error && <span className="text-[12px] text-destructive">{error}</span>}
      </div>
    </form>
  )
}
