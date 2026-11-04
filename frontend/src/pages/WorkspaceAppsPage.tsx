import { useParams, useSearchParams } from 'react-router-dom'
import { Tabs, TabsList, TabsTrigger } from 'canopy-ui/ui'
import { PersonalAppsTab } from '@/components/apps/PersonalAppsTab'
import { WorkspaceAppsTab } from '@/components/apps/WorkspaceAppsTab'

/**
 * Every app, on one page: the workspace's, and the ones that are yours. Each tab draws the same
 * lists; only what they hold and what you can do to them differ. The tab is in the URL, so the menu
 * bar and a link can open either one.
 */
export default function WorkspaceAppsPage() {
  const { workspace = '' } = useParams()
  const [search, setSearch] = useSearchParams()
  const tab = search.get('tab') === 'personal' ? 'personal' : 'workspace'

  return (
    <div className="mx-auto max-w-5xl p-6">
      <h1 className="mb-3 text-2xl font-semibold">Apps</h1>
      <Tabs value={tab} onValueChange={(next) => setSearch(next === 'personal' ? { tab: 'personal' } : {})}>
        <TabsList>
          <TabsTrigger value="workspace">Workspace apps</TabsTrigger>
          <TabsTrigger value="personal">Personal apps</TabsTrigger>
        </TabsList>
      </Tabs>
      {tab === 'personal' ? <PersonalAppsTab /> : <WorkspaceAppsTab workspace={workspace} />}
    </div>
  )
}
