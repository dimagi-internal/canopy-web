// The pieces of the capabilities editor: the lines field and one capability's card. The editor and its state are `InterfaceEditor`.
import { useState } from 'react'
import {
  CAPABILITY_ROLES,
  fromLines,
  problemsFor,
  toLines,
  type Capability,
  type CapabilityRole,
  type Problem,
} from './interfaceModel'

export const field = 'rounded-md border border-input bg-input px-2 py-1 text-[12px] text-foreground'


export const smallButton = 'rounded-md border border-border bg-input px-2.5 py-1 text-[12px] font-medium text-foreground hover:border-primary'

export function Errors({ messages, testId }: { messages: string[]; testId?: string }) {
  if (messages.length === 0) return null
  return (
    <div data-testid={testId ?? 'field-error'} role="alert" className="text-[11px] text-destructive">
      {messages.map((m) => <div key={m}>{m}</div>)}
    </div>
  )
}

export function LinesField({
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

export function CapabilityCard({
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
      <label className="flex flex-col gap-0.5 text-[12px] font-medium text-foreground">
        <span>Role</span>
        <select
          aria-label={`${name} role`}
          value={cap.role ?? 'user'}
          onChange={(e) => set({ role: e.target.value as CapabilityRole })}
          className={`${field} w-auto max-w-full`}
        >
          {CAPABILITY_ROLES.map((r) => <option key={r.value} value={r.value}>{r.label}</option>)}
        </select>
        <span className="text-[11px] font-normal text-muted-foreground">
          This is the lowest role that may use it. Who has which role is set on the agent&apos;s Access setting.
        </span>
        <Errors messages={err('role')} />
      </label>
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
