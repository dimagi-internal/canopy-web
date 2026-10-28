/**
 * The components a dynamic view's code gets as globals (no imports — the code is
 * not a module). Small on purpose: canopy's look, dense tables, a number, a bar
 * chart. Named in the authoring guide (apps/dynamic_views/guide.py); keep the two
 * in step.
 */
import { Component, createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'

type Tone = 'default' | 'success' | 'warning' | 'destructive'

const TONE_TEXT: Record<Tone, string> = {
  default: 'text-foreground',
  success: 'text-success',
  warning: 'text-warning',
  destructive: 'text-destructive',
}

const TONE_BADGE: Record<Tone, string> = {
  default: 'bg-muted text-foreground',
  success: 'bg-success/15 text-success',
  warning: 'bg-warning/15 text-warning',
  destructive: 'bg-destructive/15 text-destructive',
}

/** How a `Link` (and a plain `<a href>`) leaves the frame: through the page. */
export const NavigateContext = createContext<(path: string) => void>(() => {})

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children?: ReactNode }) {
  return (
    <section className="rounded-lg border border-border bg-card text-card-foreground">
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-border px-4 py-2">
          <h2 className="text-sm font-semibold">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  )
}

export function Stat({ label, value, hint, tone = 'default' }: { label: ReactNode; value: ReactNode; hint?: ReactNode; tone?: Tone }) {
  return (
    <div className="rounded-lg border border-border bg-card px-4 py-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${TONE_TEXT[tone] ?? TONE_TEXT.default}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}

export function Badge({ tone = 'default', children }: { tone?: Tone; children?: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium ${TONE_BADGE[tone] ?? TONE_BADGE.default}`}>
      {children}
    </span>
  )
}

export interface Column<Row> {
  key: string
  label: ReactNode
  render?: (row: Row) => ReactNode
  align?: 'left' | 'right'
}

function cell(row: Record<string, unknown>, key: string): unknown {
  return key.split('.').reduce<unknown>((v, k) => (v && typeof v === 'object' ? (v as Record<string, unknown>)[k] : undefined), row)
}

function show(v: unknown): ReactNode {
  if (v == null) return ''
  if (typeof v === 'object') return Array.isArray(v) ? v.map(String).join(', ') : JSON.stringify(v)
  return String(v)
}

export function Table<Row extends Record<string, unknown>>({ rows, columns, empty = 'Nothing to show.' }: {
  rows: Row[] | null | undefined
  columns: Column<Row>[]
  empty?: ReactNode
}) {
  const [sort, setSort] = useState<{ key: string; dir: 1 | -1 } | null>(null)
  const list = useMemo(() => (Array.isArray(rows) ? rows : []), [rows])
  const sorted = useMemo(() => {
    if (!sort) return list
    return [...list].sort((a, b) => {
      const x = cell(a, sort.key)
      const y = cell(b, sort.key)
      if (x == null) return 1
      if (y == null) return -1
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sort.dir
    })
  }, [list, sort])
  if (list.length === 0) return <Empty>{empty}</Empty>
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs text-muted-foreground">
            {columns.map((c) => (
              <th key={c.key} className={`px-2 py-1.5 font-medium ${c.align === 'right' ? 'text-right' : ''}`}>
                <button
                  type="button"
                  className="hover:text-foreground"
                  onClick={() => setSort((s) => (s?.key === c.key ? { key: c.key, dir: s.dir === 1 ? -1 : 1 } : { key: c.key, dir: 1 }))}
                >
                  {c.label}
                  {sort?.key === c.key ? (sort.dir === 1 ? ' ↑' : ' ↓') : ''}
                </button>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row, i) => (
            <tr key={i} className="border-b border-border last:border-0">
              {columns.map((c) => (
                <td key={c.key} className={`px-2 py-1.5 align-top ${c.align === 'right' ? 'text-right tabular-nums' : ''}`}>
                  {c.render ? c.render(row) : show(cell(row, c.key))}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function BarChart({ data, height }: { data: { label: ReactNode; value: number }[] | null | undefined; height?: number }) {
  const list = Array.isArray(data) ? data : []
  if (list.length === 0) return <Empty>No data.</Empty>
  const max = Math.max(...list.map((d) => Number(d.value) || 0), 1)
  return (
    <div className="flex flex-col gap-1.5" style={height ? { maxHeight: height, overflowY: 'auto' } : undefined}>
      {list.map((d, i) => (
        <div key={i} className="grid grid-cols-[minmax(6rem,12rem)_1fr_3rem] items-center gap-2 text-sm">
          <div className="truncate text-muted-foreground">{d.label}</div>
          <div className="h-3 rounded bg-muted">
            <div className="h-3 rounded bg-primary" style={{ width: `${((Number(d.value) || 0) / max) * 100}%` }} />
          </div>
          <div className="text-right tabular-nums">{d.value}</div>
        </div>
      ))}
    </div>
  )
}

export function Link({ to, children }: { to: string; children?: ReactNode }) {
  const navigate = useContext(NavigateContext)
  return (
    <a
      href={to}
      className="text-primary underline-offset-2 hover:underline"
      onClick={(e) => {
        e.preventDefault()
        navigate(to)
      }}
    >
      {children}
    </a>
  )
}

export function Empty({ children }: { children?: ReactNode }) {
  return <div className="py-3 text-sm text-muted-foreground">{children ?? 'Nothing here.'}</div>
}

export function Loading() {
  return <div className="py-3 text-sm text-muted-foreground">Loading…</div>
}

const GRID_COLS: Record<number, string> = {
  1: 'sm:grid-cols-1', 2: 'sm:grid-cols-2', 3: 'sm:grid-cols-3', 4: 'sm:grid-cols-4', 5: 'sm:grid-cols-5', 6: 'sm:grid-cols-6',
}
const GAPS: Record<number, string> = { 1: 'gap-1', 2: 'gap-2', 3: 'gap-3', 4: 'gap-4', 6: 'gap-6', 8: 'gap-8' }

export function Grid({ cols = 2, gap = 3, children }: { cols?: number; gap?: number; children?: ReactNode }) {
  return <div className={`grid grid-cols-1 ${GRID_COLS[cols] ?? GRID_COLS[2]} ${GAPS[gap] ?? GAPS[3]}`}>{children}</div>
}

export function Stack({ gap = 3, children }: { gap?: number; children?: ReactNode }) {
  return <div className={`flex flex-col ${GAPS[gap] ?? GAPS[3]}`}>{children}</div>
}

// --- form controls: what an app page (apps/workspace_apps) writes with. Each takes
// `value` and calls `onChange` with the new VALUE, not the event, so a page's code
// stays one line per field.

const CONTROL = 'w-full rounded-md border border-border bg-background px-2.5 py-1.5 text-sm text-foreground'

/** `hideLabel`: the label is for screen readers only (a control in a table row
 * whose column already says what it is) — it must still have one. */
function Labelled({ label, hideLabel, children }: { label?: ReactNode; hideLabel?: boolean; children: ReactNode }) {
  if (!label) return <>{children}</>
  return (
    <label className="block text-sm">
      <span className={hideLabel ? 'sr-only' : 'mb-1 block font-medium text-foreground'}>{label}</span>
      {children}
    </label>
  )
}

const FormContext = createContext<(() => void) | null>(null)

/**
 * A form for the sandbox. A real <form> never submits here — the frame is
 * sandboxed WITHOUT `allow-forms`, so the browser drops the submit event — so this
 * is a <div> that does the two things a form does: on a `type="submit"` Button,
 * or Enter in a one-line field, it checks every field's own validity
 * (`required`, `type="email"`, …), shows the browser's message on the first bad
 * one, and otherwise calls `onSubmit()`.
 */
export function Form({ onSubmit, children }: { onSubmit: () => void; children?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  const submit = useCallback(() => {
    const fields = ref.current?.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>('input, textarea, select') ?? []
    for (const f of Array.from(fields)) {
      if (!f.checkValidity()) {
        f.reportValidity()
        return
      }
    }
    onSubmit()
  }, [onSubmit])
  return (
    <FormContext.Provider value={submit}>
      <div ref={ref} role="form" onKeyDown={(e) => {
        if (e.key === 'Enter' && (e.target as HTMLElement).tagName === 'INPUT') {
          e.preventDefault()
          submit()
        }
      }}>
        {children}
      </div>
    </FormContext.Provider>
  )
}

const BUTTON_TONE: Record<string, string> = {
  default: 'border border-border bg-card text-foreground hover:bg-muted',
  primary: 'border border-primary bg-primary text-primary-foreground hover:opacity-75',
  destructive: 'border border-destructive/40 bg-destructive/10 text-destructive hover:bg-destructive/20',
}

/** `confirm`: ask first, inline. The frame has no allow-modals, so `window.confirm`
 * returns false without showing anything — a button guarded by it never acts. */
export function Button({ tone = 'default', size = 'md', type = 'button', disabled, onClick, confirm, children }: {
  tone?: 'default' | 'primary' | 'destructive'
  size?: 'sm' | 'md'
  type?: 'button' | 'submit'
  disabled?: boolean
  onClick?: () => void
  confirm?: ReactNode
  children?: ReactNode
}) {
  const pad = size === 'sm' ? 'px-2 py-0.5 text-xs' : 'px-3 py-1.5 text-sm'
  const formSubmit = useContext(FormContext)
  const [asking, setAsking] = useState(false)
  const cls = (t: string) => `rounded-md font-medium ${pad} ${BUTTON_TONE[t] ?? BUTTON_TONE.default}`
  if (confirm && asking) {
    return (
      <span role="alertdialog" aria-label="Confirm" className="inline-flex flex-wrap items-center gap-2 rounded-md border bg-muted px-2 py-1 text-sm">
        <span>{confirm}</span>
        <button type="button" autoFocus className={cls(tone === 'default' ? 'primary' : tone)}
          onClick={() => { setAsking(false); (type === 'submit' && formSubmit ? formSubmit : onClick)?.() }}>Yes</button>
        <button type="button" className={cls('default')} onClick={() => setAsking(false)}>Cancel</button>
      </span>
    )
  }
  if (confirm) {
    return (
      <button type="button" disabled={disabled} onClick={() => setAsking(true)}
        className={`${cls(tone)} ${disabled ? 'opacity-50' : ''}`}>
        {children}
      </button>
    )
  }
  return (
    <button type="button" disabled={disabled} onClick={type === 'submit' && formSubmit ? formSubmit : onClick}
      className={`rounded-md font-medium ${pad} ${BUTTON_TONE[tone] ?? BUTTON_TONE.default} ${disabled ? 'opacity-50' : ''}`}>
      {children}
    </button>
  )
}

export function Input({ label, value, onChange, type = 'text', placeholder, required }: {
  label?: ReactNode
  value?: string
  onChange?: (value: string) => void
  type?: 'text' | 'email' | 'url' | 'number' | 'date' | 'tel'
  placeholder?: string
  required?: boolean
}) {
  return (
    <Labelled label={label}>
      <input className={CONTROL} type={type} value={value ?? ''} placeholder={placeholder} required={required}
        onChange={(e) => onChange?.(e.target.value)} />
    </Labelled>
  )
}

export function TextArea({ label, value, onChange, rows = 4, placeholder, required }: {
  label?: ReactNode
  value?: string
  onChange?: (value: string) => void
  rows?: number
  placeholder?: string
  required?: boolean
}) {
  return (
    <Labelled label={label}>
      <textarea className={CONTROL} rows={rows} value={value ?? ''} placeholder={placeholder} required={required}
        onChange={(e) => onChange?.(e.target.value)} />
    </Labelled>
  )
}

export function Select({ label, hideLabel, value, onChange, options }: {
  label?: ReactNode
  hideLabel?: boolean
  value?: string
  onChange?: (value: string) => void
  /** Plain strings, or {value, label}. */
  options: (string | { value: string; label: ReactNode })[]
}) {
  return (
    <Labelled label={label} hideLabel={hideLabel}>
      <select className={CONTROL} value={value ?? ''} onChange={(e) => onChange?.(e.target.value)}>
        {options.map((o) => {
          const opt = typeof o === 'string' ? { value: o, label: o } : o
          return <option key={opt.value} value={opt.value}>{opt.label}</option>
        })}
      </select>
    </Labelled>
  )
}

// --- app-aware components: a workspace app's page draws the fields and slot
// contents a workspace added after the page was written, without knowing them.

export interface FieldSpec {
  type: string
  label?: string
  help?: string
  required?: boolean
  hidden?: boolean
  options?: string[]
  option_labels?: Record<string, string>
  hidden_options?: string[]
  collection?: string
  accept?: string[]
  max_mb?: number
  origin?: string
}

type Opts = { where?: Record<string, unknown>; order?: string; limit?: number }

/** The `app` prop (frame/main.tsx `appApi`), also reachable by the kit below. */
export interface AppApi {
  key: string
  name: string
  page: string
  params: Record<string, string>
  roles: string[]
  public: boolean
  draft: boolean
  dataModel: Record<string, unknown>
  settings: Record<string, unknown>
  fills: Record<string, { key: string; code: string }[]>
  query: (collection: string, opts?: Opts) => Promise<Record<string, unknown>[]>
  act: (action: string, opts?: { record?: number | null; input?: Record<string, unknown>; params?: Record<string, unknown> }) => Promise<Record<string, unknown>>
  bulk: (action: string, opts: { records: number[]; input?: Record<string, unknown>; params?: Record<string, unknown> }) => Promise<{ done: number[]; failed: { id: number; error: string }[] }>
  link: (link: string, record: number) => Promise<{ url: string; days: number }>
  history: (record: number) => Promise<Record<string, unknown>[]>
  upload: (collection: string, field: string, file: File) => Promise<{ id: number; name: string }>
  openFile: (id: number) => void
  go: (page: string, params?: Record<string, unknown>) => void
  refresh: () => void
  field: (collection: string, name: string) => FieldSpec | undefined
  fieldLabel: (collection: string, name: string) => string
  options: (collection: string, name: string) => { value: string; label: string }[]
  optionLabel: (collection: string, name: string, value: unknown) => string
}

export const AppKitContext = createContext<AppApi | null>(null)

function useApp(component: string): AppApi {
  const app = useContext(AppKitContext)
  if (!app) throw new Error(`<${component}> works only on a workspace app's page`)
  return app
}

export function FileInput({ label, collection, field, value, onChange }: {
  label?: ReactNode
  collection: string
  field: string
  value?: { id: number; name: string } | null
  onChange?: (value: { id: number; name: string } | null) => void
}) {
  const app = useApp('FileInput')
  const spec = app.field(collection, field)
  const [state, setState] = useState<{ busy: boolean; error: string | null }>({ busy: false, error: null })
  return (
    <Labelled label={label ?? spec?.label ?? field}>
      {value ? (
        <div className="flex items-center gap-2 text-sm">
          <span className="truncate">{value.name}</span>
          <Button size="sm" onClick={() => onChange?.(null)}>Remove</Button>
        </div>
      ) : (
        <input className={CONTROL} type="file" accept={(spec?.accept ?? []).join(',')} disabled={state.busy}
          onChange={async (e) => {
            const file = e.target.files?.[0]
            if (!file) return
            setState({ busy: true, error: null })
            try {
              onChange?.(await app.upload(collection, field, file))
              setState({ busy: false, error: null })
            } catch (err) {
              setState({ busy: false, error: err instanceof Error ? err.message : String(err) })
            }
          }} />
      )}
      {state.busy && <span className="mt-1 block text-xs text-muted-foreground">Uploading…</span>}
      {state.error && <span className="mt-1 block text-xs text-destructive">{state.error}</span>}
    </Labelled>
  )
}

/** One input for any field, from its declaration. */
export function FieldInput({ collection, name, value, onChange }: {
  collection: string
  name: string
  value: unknown
  onChange: (value: unknown) => void
}) {
  const app = useApp('FieldInput')
  const f = app.field(collection, name)
  if (!f || f.hidden) return null
  const label = <>{f.label ?? name}{f.help && <span className="block text-xs font-normal text-muted-foreground">{f.help}</span>}</>
  const text = value == null ? '' : String(value)
  switch (f.type) {
    case 'longtext':
      return <TextArea label={label} required={f.required} value={text} onChange={onChange} />
    case 'bool':
      return (
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={value === true} onChange={(e) => onChange(e.target.checked)} />
          <span className="font-medium">{f.label ?? name}</span>
        </label>
      )
    case 'enum':
      return <Select label={label} value={text} onChange={onChange}
        options={[{ value: '', label: 'Choose…' }, ...app.options(collection, name)]} />
    case 'file':
      return <FileInput label={label} collection={collection} field={name}
        value={value as { id: number; name: string } | null} onChange={onChange} />
    case 'number':
      return <Input label={label} type="number" required={f.required} value={text}
        onChange={(v) => onChange(v === '' ? null : Number(v))} />
    case 'email': case 'url': case 'date':
      return <Input label={label} type={f.type} required={f.required} value={text} onChange={onChange} />
    case 'ref':
      return null // a ref is chosen by the page that knows what it points at
    default:
      return <Input label={label} required={f.required} value={text} onChange={onChange} />
  }
}

/**
 * Inputs for an action's fields — by default only the ones the WORKSPACE added
 * (`origin: workspace`), so a template page shows a question it was never
 * written for. `exclude` drops fields the page draws itself.
 */
export function FieldInputs({ collection, action, values, onChange, only = 'extensions', exclude = [] }: {
  collection: string
  action: string
  values: Record<string, unknown>
  onChange: (values: Record<string, unknown>) => void
  only?: 'extensions' | 'all'
  exclude?: string[]
}) {
  const app = useApp('FieldInputs')
  const actions = (app.dataModel as { _actions?: Record<string, { input: string[] }> })._actions ?? {}
  const names = (actions[action]?.input ?? []).filter((n) => !exclude.includes(n)
    && (only === 'all' || app.field(collection, n)?.origin === 'workspace'))
  if (names.length === 0) return null
  return (
    <Stack gap={3}>
      {names.map((n) => (
        <FieldInput key={n} collection={collection} name={n} value={values[n]}
          onChange={(v) => onChange({ ...values, [n]: v })} />
      ))}
    </Stack>
  )
}

function shown(f: FieldSpec | undefined, v: unknown, app: AppApi, collection: string, name: string): ReactNode {
  if (v == null || v === '') return null
  if (f?.type === 'bool') return v ? 'Yes' : 'No'
  if (f?.type === 'enum') return app.optionLabel(collection, name, v)
  if (f?.type === 'url') return <a href={String(v)}>{String(v)}</a>
  if (f?.type === 'file') return <Button size="sm" onClick={() => app.openFile(Number(v))}>Open file</Button>
  return String(v)
}

/** A record's fields as a list — by default only the ones the workspace added. */
export function FieldValues({ collection, record, only = 'extensions', exclude = [] }: {
  collection: string
  record: Record<string, unknown> | null | undefined
  only?: 'extensions' | 'all'
  exclude?: string[]
}) {
  const app = useApp('FieldValues')
  const fields = ((app.dataModel as Record<string, { fields?: Record<string, FieldSpec> }>)[collection]?.fields) ?? {}
  const rows = Object.entries(fields)
    .filter(([n, f]) => !f.hidden && !exclude.includes(n) && (only === 'all' || f.origin === 'workspace')
      && record && record[n] != null && record[n] !== '')
  if (!record || rows.length === 0) return null
  return (
    <dl className="mt-3 grid gap-x-4 gap-y-1 text-sm" style={{ gridTemplateColumns: 'max-content 1fr' }}>
      {rows.map(([n, f]) => (
        <div key={n} className="contents">
          <dt className="text-muted-foreground">{f.label ?? n}</dt>
          <dd>{shown(f, record[n], app, collection, n)}</dd>
        </div>
      ))}
    </dl>
  )
}

type FillComponent = (props: Record<string, unknown>) => ReactNode
let fillCompiler: ((code: string) => FillComponent) | null = null
/** main.tsx registers the compiler (compile.ts imports this kit, so not the reverse). */
export function setFillCompiler(fn: (code: string) => FillComponent) {
  fillCompiler = fn
}

class FillBoundary extends Component<{ children: ReactNode; name: string }, { error: string | null }> {
  state = { error: null as string | null }
  static getDerivedStateFromError(error: Error) {
    return { error: error.message }
  }
  render() {
    if (this.state.error) {
      return <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">
        “{this.props.name}” could not be drawn: {this.state.error}</div>
    }
    return this.props.children
  }
}

/**
 * Where the WORKSPACE's own components go on a template page. The template
 * declares the slot; the workspace fills it (overlay `fill_slot`); the page keeps
 * receiving template updates. Each fill draws in its own boundary, so a broken
 * one never takes the page down. Its props are the slot's, plus `app`.
 */
export function Slot({ name, ...props }: { name: string } & Record<string, unknown>) {
  const app = useApp('Slot')
  const fills = app.fills?.[name]
  const compiled = useMemo(() => (fills ?? []).map((f) => {
    try {
      return { key: f.key, Fill: fillCompiler ? fillCompiler(f.code) : null, error: null as string | null }
    } catch (e) {
      return { key: f.key, Fill: null, error: e instanceof Error ? e.message : String(e) }
    }
  }), [fills])
  if (compiled.length === 0) return null
  return (
    <div className="my-2 flex flex-col gap-2" data-slot={name}>
      {compiled.map(({ key, Fill, error }) => (
        <FillBoundary key={key} name={key}>
          {Fill ? <Fill {...props} app={app} /> : (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-xs text-destructive">“{key}” could not be compiled: {error}</div>
          )}
        </FillBoundary>
      ))}
    </div>
  )
}

export const KIT = {
  Card, Stat, Badge, Table, BarChart, Link, Empty, Loading, Grid, Stack, Form, Button, Input, TextArea, Select,
  FileInput, FieldInput, FieldInputs, FieldValues, Slot,
}
