// /w/:workspace/agents/:slug/projects/:ref — an agent's alias for a project
// (`P5`, or its handle). A project has ONE page, at its address in the
// workspace (`/w/<ws>/projects/<handle>`); this only looks the alias up and
// goes there, so old links and the agent's own board keep working.
import type { JSX } from 'react'
import { useParams } from 'react-router-dom'

import { ProjectRefRedirect } from '@/pages/work/ProjectRefRedirect'

export function AgentProjectPage(): JSX.Element {
  const { slug = '', ref = '' } = useParams()
  return <ProjectRefRedirect slug={slug} projectRef={ref} />
}
