import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { type ProjectDetail, type ProjectPatch } from '@/api/work'
import { PriorityPicker } from '@/components/work/bits'
import { PROJECT_STATUSES, displayAgent, priorityLabel } from '@/components/work/meta'
import {
  AgentPicker,
  AgentsPicker,
  HealthBadge,
  PersonPicker,
  ProjectStatusBadge,
} from '@/components/work/projectBits'
import { useDirectory } from '@/components/work/useDirectory'
import { ProjectKbLink, ProjectMcpInstall } from '@/pages/projects/WorkspaceProjects'

export function PropertyHeader({
  detail,
  workspace,
  onSave,
}: {
  detail: ProjectDetail
  workspace: string
  onSave: (body: ProjectPatch) => Promise<void>
}): JSX.Element {
  const editable = detail.may_edit
  const directory = useDirectory(workspace)
  const lead = detail.agent_slug || detail.steward_slug || ''
  const members = detail.member_agents ?? []
  const links = detail.links ?? []
  return (
    <header className="mb-8 mt-3 border-b border-border pb-5" data-testid="project-header">
      <EditableText
        value={detail.name}
        disabled={!editable}
        label="Project name"
        onSave={(name) => void onSave({ name })}
        className="text-xl font-semibold text-foreground"
      />
      <dl className="mt-3 grid grid-cols-[7rem_1fr] gap-x-3 gap-y-1.5 text-[13px] sm:grid-cols-[7rem_1fr_7rem_1fr]" data-testid="project-properties">
        <Prop label="Status">
          <StatusControl detail={detail} onChange={(status) => void onSave({ status })} />
        </Prop>
        <Prop label="Health">
          <HealthBadge project={detail} />
          {!detail.health && !detail.update_missing && <span className="text-foreground-subtle">No update yet</span>}
        </Prop>
        <Prop label="Lead">
          <PersonPicker email={detail.owner_email} directory={directory} disabled={!editable} onChange={(owner_email) => void onSave({ owner_email })} />
        </Prop>
        <Prop label="Lead agent">
          <AgentPicker slug={lead} directory={directory} disabled={!editable} onChange={(steward) => void onSave({ steward })} />
        </Prop>
        <Prop label="Agents">
          <AgentsPicker
            slugs={members}
            lead={lead}
            directory={directory}
            disabled={!editable}
            onChange={(member_agents) => void onSave({ member_agents })}
          />
        </Prop>
        <Prop label="Priority">
          <PriorityPicker priority={detail.priority ?? 0} disabled={!editable} onChange={(priority) => void onSave({ priority })}>
            <span>{priorityLabel(detail.priority ?? 0)}</span>
          </PriorityPicker>
        </Prop>
        <Prop label="Start">
          <DateField label="Start date" value={detail.start_date} disabled={!editable} onChange={(start_date) => void onSave({ start_date })} />
        </Prop>
        <Prop label="Target">
          <DateField label="Target date" value={detail.target_date} disabled={!editable} onChange={(target_date) => void onSave({ target_date })} />
        </Prop>
      </dl>
      <div className="mt-3">
        {editable ? (
          <EditableText
            value={detail.outcome}
            multiline
            disabled={false}
            label="Outcome"
            placeholder="What done looks like"
            onSave={(outcome) => void onSave({ outcome })}
            className="text-[14px] leading-relaxed text-foreground-secondary"
          />
        ) : (
          detail.outcome && (
            <p className="text-[14px] leading-relaxed text-foreground-secondary" data-testid="project-outcome">
              {detail.outcome}
            </p>
          )
        )}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[12px] text-muted-foreground" data-testid="project-where">
        <span className="font-mono">{detail.address}</span>
        {detail.ext_id && lead && (
          <span>
            {displayAgent(lead)}’s {detail.ext_id}
          </span>
        )}
        {detail.drive_folder_url && (
          <a href={detail.drive_folder_url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            Drive folder
          </a>
        )}
        {detail.repo_slug && <span>Repo: {detail.repo_slug}</span>}
        {links.map((l) => (
          <a key={`${l.label}|${l.url}`} href={l.url} target="_blank" rel="noreferrer" className="text-primary hover:underline">
            {l.label || l.url}
          </a>
        ))}
        {detail.spawned_from && (
          <span data-testid="spawned-from">
            From{' '}
            <Link to={detail.spawned_from.url} className="text-primary hover:underline">
              {detail.spawned_from.ref} {detail.spawned_from.title}
            </Link>
            {detail.spawned_from.project_name ? ` in ${detail.spawned_from.project_name}` : ''}
          </span>
        )}
      </div>
      <ProjectKbLink project={detail} />
      <ProjectMcpInstall project={detail} />
    </header>
  )
}

/** A proposed project is started only from its decision card (the server says 409 otherwise). */
function StatusControl({ detail, onChange }: { detail: ProjectDetail; onChange: (s: string) => void }): JSX.Element {
  if (!detail.may_edit || detail.status === 'proposed') {
    return (
      <span className="inline-flex items-center gap-2">
        <ProjectStatusBadge status={detail.status} />
        {detail.may_edit && detail.proposal && (
          <a href="#project-proposal" className="text-[12px] text-primary hover:underline">
            Decide below
          </a>
        )}
      </span>
    )
  }
  return (
    <select
      aria-label="Project status"
      value={detail.status}
      onChange={(e) => onChange(e.target.value)}
      className="rounded border border-input bg-input px-1.5 py-0.5 text-[12px] text-foreground"
    >
      {PROJECT_STATUSES.filter((s) => s.value !== 'proposed').map((s) => (
        <option key={s.value} value={s.value}>
          {s.label}
        </option>
      ))}
    </select>
  )
}

function Prop({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <>
      <dt className="py-0.5 text-muted-foreground">{label}</dt>
      <dd className="flex min-w-0 items-center gap-2 py-0.5">{children}</dd>
    </>
  )
}

export function DateField({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string
  value: string | null | undefined
  onChange: (d: string | null) => void
  disabled?: boolean
}): JSX.Element {
  if (disabled) return <span className="text-foreground-secondary">{value || <span className="text-foreground-subtle">—</span>}</span>
  return (
    <input
      type="date"
      aria-label={label}
      value={value ?? ''}
      onChange={(e) => onChange(e.target.value || null)}
      className="w-[8.5rem] rounded border border-transparent bg-transparent px-1 text-[12px] text-foreground-secondary hover:border-input focus:border-input"
    />
  )
}

function EditableText({
  value,
  onSave,
  disabled,
  label,
  className,
  multiline = false,
  placeholder,
}: {
  value: string
  onSave: (v: string) => void
  disabled: boolean
  label: string
  className: string
  multiline?: boolean
  placeholder?: string
}): JSX.Element {
  const [draft, setDraft] = useState(value)
  useEffect(() => setDraft(value), [value])
  if (disabled) return multiline ? <p className={className}>{value}</p> : <h1 className={className}>{value}</h1>
  const commit = () => {
    const v = draft.trim()
    if (v !== value.trim() && (v || multiline)) onSave(v)
    else setDraft(value)
  }
  const field = 'w-full rounded border border-transparent bg-transparent px-1 -mx-1 hover:border-input focus:border-input focus:outline-none'
  if (multiline) {
    return (
      <textarea
        value={draft}
        aria-label={label}
        placeholder={placeholder}
        rows={2}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        className={`${field} resize-y placeholder:text-placeholder ${className}`}
      />
    )
  }
  return (
    <h1 className={className}>
      <input
        value={draft}
        aria-label={label}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
          if (e.key === 'Escape') setDraft(value)
        }}
        className={field}
      />
    </h1>
  )
}
