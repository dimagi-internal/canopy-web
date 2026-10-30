import { useEffect, useRef, useState } from 'react'

import {
  saveAgentInterface,
  saveAgentInterfaceModel,
  validateAgentInterface,
  type AgentInterfaceOut,
} from '@/api/agents'
import {
  askCapability,
  CALLER_BASES,
  clone,
  formatCaller,
  fromLines,
  parseCaller,
  previewCapability,
  previewFull,
  problemsFor,
  renamedCapability,
  siteDoor,
  toLines,
  uniqueName,
  withCapability,
  withoutCapability,
  type Capability,
  type InterfaceModel,
  type Problem,
} from './interfaceModel'

const field = 'rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground'
const smallButton = 'rounded-md border border-border bg-input px-2.5 py-1 text-[12px] font-medium text-foreground hover:border-primary'

type Summary = { readonly who: string; readonly reaches: string }

function Errors({ messages, testId }: { messages: string[]; testId?: string }) {
  if (messages.length === 0) return null
  return (
    <div data-testid={testId ?? 'field-error'} role="alert" className="text-[11px] text-destructive">
      {messages.map((m) => <div key={m}>{m}</div>)}
    </div>
  )
}

function DomainInput({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  const [text, setText] = useState(value)
  // A row removed above this one hands it another rule's domain.
  useEffect(() => setText((t) => (t.trim().toLowerCase() === value ? t : value)), [value])
  return (
    <input
      aria-label={label}
      placeholder="any domain"
      value={text}
      onChange={(e) => {
        setText(e.target.value)
        onChange(e.target.value)
      }}
      className={`${field} w-40`}
    />
  )
}

// Rows of "who": a class picker, an optional domain and a verified box, each with its sentence.
function CallersEditor({
  label,
  rules,
  onChange,
  preview,
  errors,
}: {
  label: string
  rules: string[]
  onChange: (rules: string[]) => void
  preview: (rule: string) => string
  errors: string[]
}) {
  const set = (i: number, rule: string) => onChange(rules.map((r, j) => (j === i ? rule : r)))
  return (
    <div className="flex flex-col gap-1.5">
      {rules.map((rule, i) => {
        const parts = parseCaller(rule)
        return (
          <div key={i} data-testid="caller-row" className="flex flex-col gap-0.5">
            <div className="flex flex-wrap items-center gap-2">
              <select
                aria-label={`${label} class ${i + 1}`}
                value={parts.base}
                onChange={(e) => set(i, formatCaller({ ...parts, base: e.target.value }))}
                className={`${field} w-auto`}
              >
                {CALLER_BASES.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
              </select>
              <DomainInput
                label={`${label} domain ${i + 1}`}
                value={parts.domain}
                onChange={(domain) => set(i, formatCaller({ ...parts, domain }))}
              />
              <label className="flex items-center gap-1 text-[12px] text-foreground-secondary">
                <input
                  type="checkbox"
                  aria-label={`${label} verified ${i + 1}`}
                  checked={parts.verified}
                  onChange={(e) => set(i, formatCaller({ ...parts, verified: e.target.checked }))}
                />
                verified
              </label>
              <button
                type="button"
                aria-label={`Remove ${label} ${i + 1}`}
                onClick={() => onChange(rules.filter((_, j) => j !== i))}
                className="text-[12px] text-muted-foreground underline hover:text-destructive"
              >
                Remove
              </button>
            </div>
            <div data-testid="caller-preview" className="text-[11px] text-muted-foreground">{preview(rule)}</div>
          </div>
        )
      })}
      <Errors messages={errors} />
      <div>
        <button type="button" onClick={() => onChange([...rules, 'member'])} className={smallButton}>
          Add who
        </button>
      </div>
    </div>
  )
}

function LinesField({
  label,
  hint,
  value,
  onChange,
  errors,
  rows = 3,
}: {
  label: string
  hint?: string
  value: string[] | undefined
  onChange: (v: string[]) => void
  errors: string[]
  rows?: number
}) {
  // The text is the field's own while it is typed in (blank lines and all); the list is what the model keeps.
  const [text, setText] = useState(toLines(value))
  return (
    <label className="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
      <span>{label}{hint ? <span className="font-normal"> — {hint}</span> : null}</span>
      <textarea
        aria-label={label}
        value={text}
        rows={Math.min(10, Math.max(rows, text.split('\n').length))}
        spellCheck={false}
        onChange={(e) => {
          setText(e.target.value)
          onChange(fromLines(e.target.value))
        }}
        className={`${field} w-full font-mono`}
      />
      <Errors messages={errors} />
    </label>
  )
}

function CapabilityCard({
  name,
  cap,
  problems,
  onChange,
  onRename,
  onRemove,
}: {
  name: string
  cap: Capability
  problems: readonly Problem[]
  onChange: (cap: Capability) => void
  onRename: (to: string) => void
  onRemove: () => void
}) {
  const set = (patch: Partial<Capability>) => onChange({ ...cap, ...patch })
  const err = (f: string) => problemsFor(problems, `${name}.${f}`)
  const inputs = Object.entries(cap.input ?? {})
  return (
    <div data-testid="capability-card" className="flex flex-col gap-2 rounded-lg border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          aria-label={`Capability name ${name}`}
          defaultValue={name}
          onBlur={(e) => e.target.value !== name && onRename(e.target.value.trim())}
          className={`${field} w-40 font-mono`}
        />
        <input
          aria-label={`${name} description`}
          placeholder="What is it for?"
          value={cap.description ?? ''}
          onChange={(e) => set({ description: e.target.value })}
          className={`${field} flex-1`}
        />
        <button type="button" onClick={onRemove} className="text-[12px] text-muted-foreground underline hover:text-destructive">
          Remove capability
        </button>
      </div>
      <Errors messages={problemsFor(problems, name)} testId="card-error" />
      <div className="flex flex-col gap-1">
        <div className="text-[12px] font-medium text-foreground">Who may use it</div>
        <CallersEditor
          label={`${name} caller`}
          rules={cap.callers ?? []}
          onChange={(callers) => set({ callers })}
          preview={(rule) => previewCapability(rule, name)}
          errors={err('callers')}
        />
      </div>
      <details open className="flex flex-col gap-2">
        <summary className="cursor-pointer text-[12px] font-medium text-foreground">What it may use</summary>
        <div className="mt-2 flex flex-col gap-2">
          <label className="flex flex-col gap-0.5 text-[11px] text-muted-foreground">
            <span>Entry — the slash command a session starts with (optional)</span>
            <input
              aria-label={`${name} entry`}
              value={cap.entry ?? ''}
              onChange={(e) => set({ entry: e.target.value || null })}
              className={`${field} w-full font-mono`}
            />
            <Errors messages={err('entry')} />
          </label>
          <LinesField label={`${name} tools`} hint="one per line; everything not listed is denied" value={cap.tools} onChange={(tools) => set({ tools })} errors={err('tools')} />
          <LinesField label={`${name} bash`} hint="shell commands it may run, one per line" value={cap.bash} onChange={(bash) => set({ bash })} errors={err('bash')} />
          <LinesField label={`${name} read paths`} value={cap.read_paths} onChange={(read_paths) => set({ read_paths })} errors={err('read_paths')} rows={2} />
          <LinesField label={`${name} write paths`} hint="none → it cannot write" value={cap.write_paths} onChange={(write_paths) => set({ write_paths })} errors={err('write_paths')} rows={2} />
          <LinesField label={`${name} sites`} hint="Connected sites whose pages use this capability" value={cap.sites} onChange={(sites) => set({ sites })} errors={err('sites')} rows={2} />
          <LinesField label={`${name} ceiling`} hint="optional: narrows a site's tools" value={cap.ceiling} onChange={(ceiling) => set({ ceiling })} errors={err('ceiling')} rows={2} />
          <div className="flex flex-col gap-1">
            <div className="text-[11px] text-muted-foreground">Input — typed fields (declared and shown; no capability passes them today)</div>
            {inputs.map(([k, t]) => (
              <div key={k} className="flex items-center gap-2 text-[12px]">
                <span className="font-mono">{k}</span>
                <select
                  aria-label={`${name} input ${k} type`}
                  value={t}
                  onChange={(e) => set({ input: { ...cap.input, [k]: e.target.value } })}
                  className={`${field} w-auto`}
                >
                  {['string', 'integer', 'number', 'boolean'].map((x) => <option key={x}>{x}</option>)}
                </select>
                <button
                  type="button"
                  onClick={() => {
                    const next = { ...cap.input }
                    delete next[k]
                    set({ input: next })
                  }}
                  className="text-muted-foreground underline"
                >
                  Remove
                </button>
              </div>
            ))}
            <div>
              <button
                type="button"
                className={smallButton}
                onClick={() => {
                  const taken = new Set(Object.keys(cap.input ?? {}))
                  let n = 'field'
                  for (let i = 2; taken.has(n); i++) n = `field_${i}`
                  set({ input: { ...cap.input, [n]: 'string' } })
                }}
              >
                Add input
              </button>
            </div>
            <Errors messages={err('input')} />
          </div>
          <label className="flex items-center gap-2 text-[12px] text-foreground">
            <input
              type="checkbox"
              aria-label={`${name} handles sensitive data`}
              checked={cap.data === 'sensitive'}
              onChange={(e) => {
                const next = { ...cap }
                if (e.target.checked) next.data = 'sensitive'
                else delete next.data
                onChange(next)
              }}
            />
            Handles sensitive data
          </label>
          <Errors messages={err('data')} />
        </div>
      </details>
    </div>
  )
}

// The structured editor for an agent's interface, with the YAML one click away. Every problem
// comes from the SERVER's parser (`POST /interface/validate`) and is shown beside its field; the
// form never decides what is valid.
export function InterfaceEditor({
  agentSlug,
  saved,
  onSaved,
  onCancel,
}: {
  agentSlug: string
  saved: AgentInterfaceOut
  onSaved: (out: AgentInterfaceOut) => void
  onCancel: () => void
}) {
  const [model, setModel] = useState<InterfaceModel>(() => clone(saved.interface as InterfaceModel))
  const [mode, setMode] = useState<'form' | 'yaml'>('form')
  const [yamlText, setYamlText] = useState(saved.yaml ?? saved.source ?? '')
  const [problems, setProblems] = useState<readonly Problem[]>([])
  const [summary, setSummary] = useState<readonly Summary[]>(saved.summary ?? [])
  const [validYaml, setValidYaml] = useState<string | null>(saved.yaml ?? null)
  const [validModel, setValidModel] = useState<InterfaceModel | null>(model)
  const [checking, setChecking] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [dirty, setDirty] = useState(false)
  const seq = useRef(0)

  const savedSensitive = Object.entries((saved.interface as InterfaceModel).capabilities ?? {})
    .filter(([, c]) => c.data === 'sensitive').map(([n]) => n)
  const draftSensitive = mode === 'form'
    ? Object.entries(model.capabilities ?? {}).filter(([, c]) => c.data === 'sensitive').map(([n]) => n)
    : (validModel ? Object.entries(validModel.capabilities ?? {}).filter(([, c]) => c.data === 'sensitive').map(([n]) => n) : savedSensitive)

  // Ask the server's parser about the draft shortly after it stops changing.
  useEffect(() => {
    const mine = ++seq.current
    setChecking(true)
    const t = setTimeout(() => {
      validateAgentInterface(agentSlug, mode === 'form' ? { interface: model } : { source: yamlText })
        .then((r) => {
          if (mine !== seq.current) return
          setProblems(r.errors)
          if (r.errors.length === 0) {
            setSummary(r.summary)
            setValidYaml(r.yaml)
            setValidModel((r.interface ?? null) as InterfaceModel | null)
          } else {
            setValidYaml(null)
            setValidModel(null)
          }
        })
        .catch((e: unknown) => mine === seq.current && setProblems([{ path: '', message: e instanceof Error ? e.message : 'Could not check' }]))
        .finally(() => mine === seq.current && setChecking(false))
    }, 250)
    return () => clearTimeout(t)
  }, [agentSlug, mode, model, yamlText])

  const edit = (next: InterfaceModel) => {
    setModel(next)
    setDirty(true)
  }

  const toYaml = () => {
    if (validYaml === null) return
    setYamlText(validYaml)
    setMode('yaml')
  }
  const toForm = () => {
    if (validModel === null) return
    setModel(clone(validModel))
    setMode('form')
  }

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      onSaved(mode === 'form' ? await saveAgentInterfaceModel(agentSlug, model) : await saveAgentInterface(agentSlug, yamlText))
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : 'Could not save')
    } finally {
      setBusy(false)
    }
  }

  const caps = model.capabilities ?? {}
  const names = Object.keys(caps)
  const sites = ['connect-labs', 'canopy-web']
  const rootProblems = problems.filter((p) => p.path === '')

  return (
    <div data-testid="interface-editor" className="flex flex-col gap-3">
      <div className="flex gap-1 text-[12px]" role="tablist">
        <button type="button" role="tab" aria-selected={mode === 'form'} onClick={() => mode === 'yaml' && toForm()}
          disabled={mode === 'yaml' && validModel === null}
          className={`rounded-md px-2.5 py-1 ${mode === 'form' ? 'bg-primary text-primary-foreground' : 'border border-border bg-input text-foreground'}`}>
          Form
        </button>
        <button type="button" role="tab" aria-selected={mode === 'yaml'} onClick={() => mode === 'form' && toYaml()}
          disabled={mode === 'form' && validYaml === null}
          className={`rounded-md px-2.5 py-1 ${mode === 'yaml' ? 'bg-primary text-primary-foreground' : 'border border-border bg-input text-foreground'}`}>
          Advanced: YAML
        </button>
      </div>

      <div data-testid="draft-summary" className="rounded-md border border-border bg-card px-3 py-2">
        <div className="mb-1 text-[11px] font-medium text-muted-foreground">With these settings</div>
        <SummaryLines lines={summary} />
      </div>

      {mode === 'yaml' ? (
        <div className="flex flex-col gap-2">
          <label htmlFor={`iface-${agentSlug}`} className="text-[12px] text-muted-foreground">Interface (YAML)</label>
          <textarea
            id={`iface-${agentSlug}`}
            value={yamlText}
            onChange={(e) => { setYamlText(e.target.value); setDirty(true) }}
            rows={Math.min(30, Math.max(10, yamlText.split('\n').length + 1))}
            spellCheck={false}
            className="w-full rounded-md border border-input bg-input p-2 font-mono text-[12px] text-foreground"
          />
          <Errors messages={problems.map((p) => p.message)} testId="yaml-error" />
        </div>
      ) : (
        <>
          <section className="flex flex-col gap-1.5">
            <h3 className="m-0 text-[13px] font-medium text-foreground">Whole agent</h3>
            <p className="m-0 text-[11px] text-muted-foreground">
              Who gets everything the agent&apos;s admins get. Leave empty to keep it to the owner, admins and editors.
            </p>
            <CallersEditor
              label="Whole-agent rule"
              rules={model.full ?? []}
              onChange={(full) => edit({ ...model, full })}
              preview={previewFull}
              errors={problemsFor(problems, 'full')}
            />
          </section>
          <section className="flex flex-col gap-2">
            <h3 className="m-0 text-[13px] font-medium text-foreground">Capabilities</h3>
            <p className="m-0 text-[11px] text-muted-foreground">
              What everyone else may do. A caller reaches a capability only if it lists them; everything not listed is refused.
            </p>
            {names.map((n) => (
              <CapabilityCard
                key={n}
                name={n}
                cap={caps[n]}
                problems={problems}
                onChange={(c) => edit(withCapability(model, n, c))}
                onRename={(to) => to && !caps[to] && edit(renamedCapability(model, n, to))}
                onRemove={() => edit(withoutCapability(model, n))}
              />
            ))}
            {names.length === 0 && <p className="m-0 text-[12px] text-muted-foreground">No capabilities: nobody outside the owner, admins and editors can use this agent.</p>}
            <div className="flex flex-wrap gap-2">
              {!caps.ask && (
                <button type="button" onClick={() => edit(withCapability(model, 'ask', askCapability(agentSlug)))} className={smallButton}>
                  Add an ask capability for colleagues
                </button>
              )}
              {sites.map((s) => (
                <button key={s} type="button" onClick={() => edit(withCapability(model, uniqueName(model, s.replace(/-.*$/, '')), siteDoor(s)))} className={smallButton}>
                  Add a {s} site capability
                </button>
              ))}
              <button type="button" onClick={() => edit(withCapability(model, uniqueName(model, 'capability'), { ...askCapability(agentSlug), description: '', entry: null }))} className={smallButton}>
                Add a blank capability
              </button>
            </div>
          </section>
        </>
      )}

      <Errors messages={rootProblems.map((p) => p.message)} testId="interface-error" />
      {draftSensitive.length > 0 && savedSensitive.length === 0 && (
        <p data-testid="sensitive-on" className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-foreground">
          {draftSensitive.join(', ')} will handle sensitive data: you vouch that it reads a host&apos;s data only through
          canopy&apos;s gateway and keeps none of it. Saving is logged with the agent&apos;s other setting changes.
        </p>
      )}
      {draftSensitive.length === 0 && savedSensitive.length > 0 && (
        <p data-testid="sensitive-off" className="m-0 rounded-md border border-warning/30 bg-warning/10 px-2 py-1 text-[12px] text-warning">
          No capability handles sensitive data any more: saving turns it off for {savedSensitive.join(', ')}, and hosts stop giving it their data.
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy || checking || problems.length > 0 || !dirty}
          onClick={() => void save()}
          className="rounded-md bg-primary px-3 py-1 text-[12px] font-medium text-primary-foreground disabled:opacity-50"
        >
          {busy ? 'Saving…' : 'Save'}
        </button>
        <button type="button" onClick={onCancel} className="text-[12px] text-muted-foreground underline">Cancel</button>
        {problems.length > 0 && <span className="text-[11px] text-destructive">Fix the problems above to save.</span>}
      </div>
      {error && <p role="alert" className="m-0 whitespace-pre-wrap text-[12px] text-destructive">{error}</p>}
    </div>
  )
}

// One line per caller class: who, and what they reach.
export function SummaryLines({ lines }: { lines: readonly Summary[] }) {
  return (
    <ul className="m-0 flex list-none flex-col gap-0.5 p-0 text-[13px]">
      {lines.map((l) => (
        <li key={l.who} data-testid="summary-line" className="text-foreground">
          <span className="font-medium">{l.who}</span>
          <span className="text-foreground-secondary">: {l.reaches}</span>
        </li>
      ))}
    </ul>
  )
}
