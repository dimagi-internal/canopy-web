import { useEffect, useRef, useState } from 'react'
import {
  saveAgentInterface,
  saveAgentInterfaceModel,
  validateAgentInterface,
  type AgentInterfaceOut,
} from '@/api/agents'
import {
  askCapability,
  clone,
  renamedCapability,
  siteCapability,
  uniqueName,
  withCapability,
  withoutCapability,
  type InterfaceModel,
  type Problem,
} from './interfaceModel'
import { smallButton, Errors, CapabilityCard } from './InterfaceEditorParts'


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
          <section className="flex flex-col gap-2">
            <h3 className="m-0 text-[13px] font-medium text-foreground">Capabilities</h3>
            <p className="m-0 text-[11px] text-muted-foreground">
              What viewers and users may do. Each capability names the lowest role that may use it; who has which role is set on the Access setting. Anything not listed here is refused.
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
            {names.length === 0 && <p className="m-0 text-[12px] text-muted-foreground">No capabilities: only people the access list makes editors, agent admins or the owner can use this agent.</p>}
            <div className="flex flex-wrap gap-2">
              {!caps.ask && (
                <button type="button" onClick={() => edit(withCapability(model, 'ask', askCapability(agentSlug)))} className={smallButton}>
                  Add an ask capability for colleagues
                </button>
              )}
              {sites.map((s) => (
                <button key={s} type="button" onClick={() => edit(withCapability(model, uniqueName(model, s.replace(/-.*$/, '')), siteCapability(s)))} className={smallButton}>
                  Add a {s} site capability
                </button>
              ))}
              <button type="button" onClick={() => edit(withCapability(model, uniqueName(model, 'capability'), { ...askCapability(agentSlug), description: '', entry: null, role: 'user' }))} className={smallButton}>
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
