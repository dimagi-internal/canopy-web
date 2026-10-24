// An agent's alias for a project (`eva`/`P5`) → the project's one page at its
// address (`url`, `/w/<ws>/projects/<handle>`). Used by the agent-board route
// and the older `/w/:ws/projects/:agent/:ref` address; small and eager so the
// router can use it without loading the agent workspace.
import { useEffect, useState, type JSX } from 'react'
import { Navigate } from 'react-router-dom'

import { AgentApiError, getProject } from '@/api/agents'

export function ProjectRefRedirect({ slug, projectRef }: { slug: string; projectRef: string }): JSX.Element {
  const key = `${slug}/${projectRef}`
  const [target, setTarget] = useState<{ key: string; to: string | null; error: string | null } | null>(null)

  useEffect(() => {
    let cancelled = false
    getProject(slug, projectRef)
      .then((p) => !cancelled && setTarget({ key, to: p.url, error: null }))
      .catch((err: unknown) => {
        if (cancelled) return
        const error =
          err instanceof AgentApiError && err.status === 404
            ? `No project ${projectRef} on ${slug}.`
            : err instanceof Error
              ? err.message
              : 'Could not open the project'
        setTarget({ key, to: null, error })
      })
    return () => {
      cancelled = true
    }
  }, [slug, projectRef, key])

  const fresh = target?.key === key ? target : null
  if (fresh?.to) return <Navigate to={fresh.to} replace />
  if (fresh?.error) return <p className="px-6 py-8 text-[13px] text-destructive">{fresh.error}</p>
  return <p className="px-6 py-8 text-[13px] text-muted-foreground">Opening {projectRef}…</p>
}
