import { Navigate, useLocation } from 'react-router-dom'

// An old agent address that redirects and KEEPS its query, so an old
// `/items?batch=…` link still opens its sitting on Tasks. `add` merges extra
// params on top, e.g. the retired Inbox is Tasks filtered to `waiting=me`.
export function KeepQueryRedirect({ to, add }: { to: string; add?: Record<string, string> }) {
  const { search } = useLocation()
  const query = new URLSearchParams(search)
  for (const [k, v] of Object.entries(add ?? {})) query.set(k, v)
  const qs = query.toString()
  return <Navigate to={qs ? `${to}?${qs}` : to} replace />
}
