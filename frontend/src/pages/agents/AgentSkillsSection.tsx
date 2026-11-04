import { useEffect, useState } from 'react'
import { useOutletContext } from 'react-router-dom'
import { listAgentSkills, type AgentSkillOut } from '@/api/agents'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'
import { SkillCard } from '@/components/agents/cards'
import { WorkbenchSubHeader, WorkbenchSkeleton } from 'canopy-ui'

export function AgentSkillsSection() {
  const { agent } = useOutletContext<AgentOutletContext>()
  const [skills, setSkills] = useState<AgentSkillOut[] | null>(null)

  useEffect(() => {
    let cancelled = false
    setSkills(null)
    listAgentSkills(agent.slug)
      .then((list) => !cancelled && setSkills(list))
      .catch(() => !cancelled && setSkills([]))
    return () => {
      cancelled = true
    }
  }, [agent.slug])

  const internal = skills?.filter((sk) => sk.internal).length ?? 0

  return (
    <div className="max-w-4xl px-6 py-8">
      <WorkbenchSubHeader title="Skills" count={skills?.length} />
      {skills && internal > 0 && (
        <p className="text-[12px] text-muted-foreground mb-3">
          {skills.length - internal} you can start with <code>/</code> in chat. {internal} internal: the agent
          loads {internal === 1 ? 'it' : 'them'} itself.
        </p>
      )}
      {skills === null ? (
        <WorkbenchSkeleton />
      ) : skills.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No skills yet.</p>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          {skills.map((sk) => (
            <SkillCard key={sk.id} skill={sk} />
          ))}
        </div>
      )}
    </div>
  )
}
