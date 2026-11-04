import { useId, type JSX } from 'react'
import { Input, Textarea } from 'canopy-ui/ui'
import type { LocalAppSetting, LocalAppSettingsSpec, LocalAppSettingValue } from '@/api/localApps'

export type SettingValues = Record<string, LocalAppSettingValue>
type AgentOption = { readonly [key: string]: string }

const SELECT_CLASS = 'w-full rounded-md border border-input bg-input px-2 py-1.5 text-[13px] text-foreground'

function valueOf(field: LocalAppSetting, raw: unknown): LocalAppSettingValue {
  if (field.type === 'bool') return typeof raw === 'boolean' ? raw : false
  return typeof raw === 'string' ? raw : ''
}

/** What a form opens with: what's saved, else the manifest's default. */
export function initialSettings(spec: LocalAppSettingsSpec, stored: Readonly<Record<string, unknown>> = {}): SettingValues {
  return Object.fromEntries(
    Object.entries(spec).map(([name, field]) => [name, valueOf(field, name in stored ? stored[name] : field.default)]),
  )
}

/**
 * What to send. A blank optional setting is left out when nothing is saved under it, and a blank
 * agent always is, because the server reads "" as an agent named "" and refuses. Configure sends
 * only what changed: an agent the person can no longer use is refused if it's sent again, and that
 * shouldn't stop them changing another setting. A blank required setting is also left out, so
 * installing is refused by the server, in its words.
 */
export function settingsToSend(
  spec: LocalAppSettingsSpec,
  values: SettingValues,
  stored?: Readonly<Record<string, unknown>>,
): SettingValues {
  const out: SettingValues = {}
  for (const [name, field] of Object.entries(spec)) {
    const value = values[name]
    if (value === undefined) continue
    const saved = stored?.[name]
    if (stored && value === valueOf(field, saved) && saved !== undefined) continue
    if (typeof value === 'string' && !value.trim()) {
      if (field.type !== 'agent' && !field.required && saved !== undefined) out[name] = ''
      continue
    }
    out[name] = value
  }
  return out
}

/** A form drawn from a manifest's `settings`: agent, text, longtext or bool. */
export function SettingsFields({
  spec,
  agents,
  values,
  onChange,
}: {
  spec: LocalAppSettingsSpec
  agents: readonly AgentOption[]
  values: SettingValues
  onChange: (next: SettingValues) => void
}): JSX.Element {
  const base = useId()
  const set = (name: string, value: LocalAppSettingValue) => onChange({ ...values, [name]: value })

  return (
    <div className="space-y-3">
      {Object.entries(spec).map(([name, field]) => {
        const id = `${base}-${name}`
        const helpId = field.help ? `${id}-help` : undefined
        const help = field.help && <p id={helpId} className="mt-0.5 text-xs text-muted-foreground">{field.help}</p>
        if (field.type === 'bool') {
          return (
            <div key={name}>
              <label htmlFor={id} className="flex items-center gap-1.5 text-[13px] text-foreground">
                <input id={id} type="checkbox" aria-describedby={helpId} checked={values[name] === true}
                  onChange={(e) => set(name, e.target.checked)} />
                {field.label}
              </label>
              {help}
            </div>
          )
        }
        const text = typeof values[name] === 'string' ? values[name] : ''
        return (
          <div key={name}>
            <label htmlFor={id} className="mb-1 block text-[13px] font-medium text-foreground">
              {field.label}
              {field.required && <span className="ml-1 font-normal text-muted-foreground">(required)</span>}
            </label>
            {field.type === 'agent' ? (
              <AgentSelect id={id} helpId={helpId} required={field.required} agents={agents} value={text}
                onChange={(v) => set(name, v)} />
            ) : field.type === 'longtext' ? (
              <Textarea id={id} aria-describedby={helpId} required={field.required} maxLength={4000} rows={3}
                value={text} onChange={(e) => set(name, e.target.value)} />
            ) : (
              <Input id={id} aria-describedby={helpId} required={field.required} maxLength={4000}
                value={text} onChange={(e) => set(name, e.target.value)} />
            )}
            {help}
          </div>
        )
      })}
    </div>
  )
}

function AgentSelect({
  id,
  helpId,
  required,
  agents,
  value,
  onChange,
}: {
  id: string
  helpId?: string
  required: boolean
  agents: readonly AgentOption[]
  value: string
  onChange: (value: string) => void
}): JSX.Element {
  // A saved agent can drop off the list when the person loses access to it. Show it rather than
  // appear to have nothing chosen; the installation's refusal says why it won't start.
  const gone = value && !agents.some((a) => a.slug === value)
  return (
    <>
      <select id={id} aria-describedby={helpId} required={required} className={SELECT_CLASS} value={value}
        onChange={(e) => onChange(e.target.value)}>
        <option value="">Choose an agent</option>
        {gone && <option value={value}>{value} (you can't use this one now)</option>}
        {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
      </select>
      {agents.length === 0 && (
        <p className="mt-0.5 text-xs text-muted-foreground">You have no agent you can use in this workspace.</p>
      )}
    </>
  )
}
