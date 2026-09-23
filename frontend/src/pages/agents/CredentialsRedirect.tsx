import { Navigate, useLocation } from 'react-router-dom'

// Credentials is a section of the agent's Settings page now (it was its own
// rail entry, then an Overview section, and moved with the rest of the agent's
// configuration on 2026-09-23). Old links and bookmarks keep working, and keep
// their query — the Google sign-in returns with `?google=ok`, and the outcome
// banner reads that.
export function CredentialsRedirect() {
  const { search } = useLocation()
  return <Navigate to={`../settings${search}#credentials`} replace />
}
