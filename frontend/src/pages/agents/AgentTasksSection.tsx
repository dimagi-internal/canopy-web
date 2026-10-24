// An agent's Tasks tab: the workspace task list pinned to this agent — the
// same table, board and calendar as /w/:ws/tasks, so a task looks and behaves
// the same here as everywhere else. "Waiting on me" is a tab, which is where
// `?waiting=me` (and the old Inbox address) lands; any change re-reads the
// rail's waiting badge.
import { useMemo, type JSX } from 'react'
import { useOutletContext, useParams } from 'react-router-dom'
import { WorkbenchSubHeader } from 'canopy-ui'

import { TasksView } from '@/components/work/TasksView'
import type { TabId } from '@/components/work/viewState'
import type { AgentOutletContext } from '@/pages/AgentWorkspacePage'

const AGENT_TABS: readonly TabId[] = ['all', 'waiting']

export function AgentTasksSection(): JSX.Element {
  const { agent, refreshWaiting } = useOutletContext<AgentOutletContext>()
  const { workspace: routeWorkspace = '' } = useParams()
  const workspace = agent.workspace || routeWorkspace
  const fixed = useMemo(() => ({ agent: agent.slug }), [agent.slug])
  return (
    <div className="px-6 py-8">
      <WorkbenchSubHeader title="Tasks" />
      <TasksView
        key={`${workspace}|${agent.slug}`}
        workspace={workspace}
        fixed={fixed}
        tabs={AGENT_TABS}
        onChanged={refreshWaiting}
      />
    </div>
  )
}
