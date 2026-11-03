import { Fragment, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { Plus, Search } from 'lucide-react'

import { Button, Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from 'canopy-ui/ui'
import type { StatusTone } from 'canopy-ui/lib'
import { relativeAge } from '@/lib/relativeAge'

import { VaultRow } from './VaultEditorRow'

/**
 * A vault's secrets as one table you edit in place: find a key, Change it,
 * paste (or load a file), Save. Reveal and Delete sit on the same row.
 *
 * It knows nothing about WHOSE vault it is. The page that owns the vault (an
 * agent's Security tab today) loads the rows and hands in the four operations;
 * how a value is stored — sealed in this browser, or posted behind a passkey
 * step-up — is the caller's business, so the same table can sit on any page
 * that holds secrets.
 *
 * Never holds a value longer than it must: a draft is dropped the moment it
 * lands, and a revealed value (at most one) is dropped after `revealMs`.
 */

export interface VaultEntry {
  name: string
  /** Whether a value is stored. */
  set: boolean
  updatedAt?: string | null
  updatedBy?: string | null
  lastReadAt?: string | null
  readCount?: number | null
  /** Something to say about this row. A warning or destructive flag sorts it to the top. */
  flag?: { label: string; tone: StatusTone }
  /** Readable by no person (a credential a person delegated to the agent): never
   * changed or revealed here — only its person replaces it, from where they gave it. */
  sealed?: boolean
}

/** How long a revealed value stays on screen. */
export const REVEAL_MS = 60_000

// A credential file is a few KB; anything far larger is the wrong file.
const MAX_UPLOAD_BYTES = 64 * 1024

// Env-var style, plus the hyphen `gog-token` carries.
const NAME_RE = /^[A-Za-z_][A-Za-z0-9_-]*$/

const needsYou = (e: VaultEntry) => e.flag?.tone === 'warning' || e.flag?.tone === 'destructive'

export function VaultEditor({
  entries,
  canEdit,
  onSave,
  onReveal,
  onDelete,
  usage,
  details,
  badge,
  describeError = (e, fallback) => (e instanceof Error ? e.message : fallback),
  revealMs = REVEAL_MS,
}: {
  entries: readonly VaultEntry[]
  /** Display hint only; the server still decides. */
  canEdit: boolean
  /** Store `value` under `name` — a new secret or a changed one. Throw to show an error. */
  onSave: (name: string, value: string) => Promise<void>
  /** Return the stored value. Omit to offer no Reveal. */
  onReveal?: (name: string) => Promise<string>
  /** Omit to offer no Delete. */
  onDelete?: (name: string) => Promise<void>
  /** The "Last used" cell. Defaults to when it was last read. */
  usage?: (entry: VaultEntry) => ReactNode
  /** What a row's Activity button opens beneath it, full width (who reads it, alerts).
   * Return null for a row with none. Never inline in the row: a click on the row is for the value. */
  details?: (entry: VaultEntry) => ReactNode
  /** Shown at the right of the toolbar, e.g. how values are held. */
  badge?: ReactNode
  describeError?: (e: unknown, fallback: string) => string
  revealMs?: number
}) {
  const [query, setQuery] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [revealed, setRevealed] = useState<{ name: string; value: string } | null>(null)
  const [detailsFor, setDetailsFor] = useState<string | null>(null)

  useEffect(() => {
    if (!revealed) return
    const t = window.setTimeout(() => setRevealed(null), revealMs)
    return () => window.clearTimeout(t)
  }, [revealed, revealMs])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    return entries
      .filter((e) => !q || e.name.toLowerCase().includes(q))
      .sort((a, b) => Number(needsYou(b)) - Number(needsYou(a)) || a.name.localeCompare(b.name))
  }, [entries, query])

  // Closing a row also drops its error: a message left behind would read as
  // being about whatever the table shows now.
  const close = () => {
    setEditing(null)
    setAdding(false)
    setNewName('')
    setDraft('')
    setError(null)
  }

  const run = async (fallback: string, op: () => Promise<void>) => {
    setBusy(true)
    setError(null)
    try {
      await op()
    } catch (e) {
      setError(describeError(e, fallback))
    } finally {
      setBusy(false)
    }
  }

  const save = (name: string) => (ev?: FormEvent) => {
    ev?.preventDefault()
    const value = draft.trim()
    if (!value || busy) return
    void run('Could not save', async () => {
      await onSave(name, value)
      close()
    })
  }

  const add = (ev?: FormEvent) => {
    ev?.preventDefault()
    const name = newName.trim()
    if (!NAME_RE.test(name)) {
      setError('A name is letters, digits, _ or -, and does not start with a digit.')
      return
    }
    if (entries.some((e) => e.name === name && e.set)) {
      setError(`${name} already has a value — use Change on its row.`)
      return
    }
    save(name)()
  }

  // A key file (a service-account JSON, a PEM) is loaded into the draft, to be
  // stored by Save like a pasted value. Storing it on the file picker's own
  // event would open the passkey check outside a click, which browsers refuse.
  // Only a trailing newline is dropped.
  const load = async (file: File | undefined) => {
    if (!file || busy) return
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(`${file.name} is larger than ${MAX_UPLOAD_BYTES / 1024} KB — not a credential file?`)
      return
    }
    setError(null)
    setDraft((await file.text()).replace(/\r?\n$/, ''))
  }

  const uploadButton = (name: string, testId = `upload-${name}`) => (
    <label
      title="Load the contents of a file from your computer (e.g. a PEM or a service-account JSON key), then Save"
      className={`inline-flex h-7 cursor-pointer items-center rounded-md border border-border px-2.5 text-[0.8rem] font-medium text-foreground hover:bg-muted ${busy ? 'pointer-events-none opacity-50' : ''}`}
    >
      Upload file
      <input
        type="file"
        className="sr-only"
        disabled={busy}
        aria-label={`Upload a file as the value of ${name}`}
        data-testid={testId}
        onChange={(e) => {
          const file = e.target.files?.[0]
          e.target.value = ''
          void load(file)
        }}
      />
    </label>
  )

  const reveal = (name: string) => {
    if (!onReveal) return
    if (revealed?.name === name) return setRevealed(null)
    void run('Could not reveal', async () => setRevealed({ name, value: await onReveal(name) }))
  }

  const remove = (name: string) => {
    if (!onDelete) return
    if (!window.confirm(`Delete the stored value for "${name}"? This cannot be undone.`)) return
    void run('Could not delete', async () => {
      await onDelete(name)
      if (revealed?.name === name) setRevealed(null)
    })
  }

  // Plain, multi-line text: a PEM or a JSON key keeps its line breaks, and you
  // can see what you pasted. ⌘/Ctrl+Enter saves, Escape cancels.
  const valueInput = (label: string, onSubmit: () => void, autoFocus = true) => (
    <textarea
      autoComplete="off"
      spellCheck={false}
      autoFocus={autoFocus}
      rows={draft.includes('\n') ? Math.min(12, draft.split('\n').length) : 1}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Escape') close()
        else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          e.preventDefault()
          onSubmit()
        }
      }}
      placeholder="Paste the value, or Upload file"
      aria-label={label}
      className="min-h-11 w-full resize-y rounded-md border border-primary bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground sm:min-h-0"
    />
  )

  const lastUsed = (e: VaultEntry) =>
    usage ? usage(e) : e.lastReadAt ? relativeAge(e.lastReadAt) : e.set ? 'never' : ''

  const detailsNode = (e: VaultEntry) => (details && e.set ? details(e) : null)

  return (
    <div data-testid="vault-editor">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <label className="relative min-w-48 flex-1">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={`Search ${entries.length} secret${entries.length === 1 ? '' : 's'}…`}
            aria-label="Search secrets"
            className="min-h-11 w-full rounded-md border border-input bg-input py-1 pl-7 pr-2 text-[12px] text-foreground placeholder:text-muted-foreground sm:min-h-0"
          />
        </label>
        {badge}
        {canEdit && (
          <Button size="sm" onClick={() => { close(); setAdding(true) }} disabled={busy || adding} data-testid="vault-add">
            <Plus aria-hidden="true" /> Add secret
          </Button>
        )}
      </div>

      {error && <p className="mb-2 text-[12px] text-destructive" role="alert" data-testid="vault-error">{error}</p>}

      <Table className="text-[12px]">
        <TableHeader>
          <TableRow>
            <TableHead>Name</TableHead>
            <TableHead className="hidden sm:table-cell">Value</TableHead>
            {/* On a phone the row keeps what you act on: name, value, buttons. */}
            <TableHead className="hidden sm:table-cell">Changed</TableHead>
            <TableHead className="hidden sm:table-cell">Last used</TableHead>
            <TableHead><span className="sr-only">Actions</span></TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {adding && (
            <TableRow className="bg-muted/40" data-testid="vault-add-row">
              <TableCell colSpan={5}>
                <form onSubmit={add} className="flex flex-wrap items-center gap-2">
                  <input
                    autoFocus
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => e.key === 'Escape' && close()}
                    placeholder="NAME"
                    aria-label="New secret name"
                    className="min-h-11 w-full rounded-md border border-input bg-input px-2 py-1 font-mono text-[12px] text-foreground placeholder:text-muted-foreground sm:min-h-0 sm:w-56"
                  />
                  <div className="min-w-48 flex-1">
                    {valueInput('New secret value', () => add(), false)}
                  </div>
                  <Button size="sm" type="submit" disabled={busy || !newName.trim() || !draft.trim()}>Save</Button>
                  {uploadButton(newName.trim() || 'the new secret', 'upload-new')}
                  <Button size="sm" variant="ghost" type="button" onClick={close}>Cancel</Button>
                </form>
              </TableCell>
            </TableRow>
          )}

          {shown.map((e) => (
            editing === e.name ? (
              <TableRow key={e.name} className="bg-muted/40" data-testid={`vault-row-${e.name}`}>
                <TableCell className="font-mono text-foreground">{e.name}</TableCell>
                <TableCell colSpan={3}>
                  <form onSubmit={save(e.name)}>{valueInput(`New value for ${e.name}`, () => save(e.name)())}</form>
                </TableCell>
                <TableCell className="text-right">
                  <div className="flex justify-end gap-1">
                    <Button size="sm" onClick={() => save(e.name)()} disabled={busy || !draft.trim()} data-testid={`vault-save-${e.name}`}>
                      {busy ? 'Saving…' : 'Save'}
                    </Button>
                    {uploadButton(e.name)}
                    <Button size="sm" variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
                  </div>
                </TableCell>
              </TableRow>
            ) : (
              <Fragment key={e.name}>
                <VaultRow
                  entry={e}
                  lastUsed={lastUsed(e)}
                  revealed={revealed?.name === e.name ? revealed.value : null}
                  canEdit={canEdit}
                  busy={busy}
                  detailsOpen={detailsFor === e.name}
                  onChange={() => { close(); setEditing(e.name) }}
                  onReveal={onReveal && e.set && !e.sealed ? () => reveal(e.name) : undefined}
                  onDelete={onDelete && e.set ? () => remove(e.name) : undefined}
                  onDetails={detailsNode(e) ? () => setDetailsFor(detailsFor === e.name ? null : e.name) : undefined}
                />
                {detailsFor === e.name && (
                  <TableRow data-testid={`vault-details-row-${e.name}`}>
                    <TableCell colSpan={5} className="bg-muted/30">{detailsNode(e)}</TableCell>
                  </TableRow>
                )}
              </Fragment>
            )
          ))}

          {shown.length === 0 && !adding && (
            <TableRow>
              <TableCell colSpan={5} className="py-4 text-center text-muted-foreground">
                {entries.length === 0 ? 'No secrets yet.' : `Nothing matches “${query}”.`}
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  )
}

export { EncryptedBadge } from './VaultEditorRow'
