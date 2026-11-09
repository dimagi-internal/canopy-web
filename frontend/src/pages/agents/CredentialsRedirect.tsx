import { Navigate, useLocation } from 'react-router-dom'

// Credentials is a section of the agent's Settings page, with the rest of the
// agent's configuration. Old links and bookmarks keep working, and keep
// their query — the Google sign-in returns with `?google=ok`, and the outcome
// banner reads that.
export function CredentialsRedirect() {
  const { search } = useLocation()
  return <Navigate to={`../settings${search}#credentials`} replace />
}
