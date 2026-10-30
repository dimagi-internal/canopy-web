import { useState } from 'react'

import type { AgentAccessRowOut } from '@/api/agents'
import { PeopleTable, type PersonRow } from '@/components/people/PeopleTable'
import { roleWord, sourceWords } from './accessWords'

// "WHAT EACH PERSON GETS NOW": the one question only the full roster answers. The server
// composes each person's result with the same rules a turn is decided by; this draws it —
// their role, and in words where it comes from. Read-only: change it on the list above.
export function AgentAccessPeople({ members }: { members: readonly AgentAccessRowOut[] }) {
  const [open, setOpen] = useState(false)
  const count = members.length
  const people = `${count} ${count === 1 ? 'person' : 'people'}`

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="text-[12px] text-muted-foreground hover:text-foreground"
        data-testid="agent-access-show-all"
      >
        {open ? '▾ Hide' : '▸ Show'} what each person gets now ({people})
      </button>
      {open && (
        <PeopleTable
          roleHeader="Role here"
          extraColumns={['Why']}
          actions={false}
          rows={members.map(
            (r): PersonRow => ({
              key: r.user_id,
              testId: 'agent-access-row',
              name: r.name,
              email: r.email,
              detail: <span className="capitalize">workspace {r.workspace_role}</span>,
              role: r.role ?? 'none',
              roleLabel: roleWord(r.role),
              options: [],
              editable: false,
              extra: [
                <span
                  key="why"
                  className={`text-[12px] ${r.role ? 'text-foreground-secondary' : 'text-warning'}`}
                  title={r.manual_only ? 'Every turn needs approval before anything goes out' : undefined}
                >
                  {sourceWords(r)}
                  {r.manual_only ? ' · needs approval' : ''}
                </span>,
              ],
            }),
          )}
        />
      )}
    </div>
  )
}
