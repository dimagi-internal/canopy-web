import { Navigate, useLocation } from 'react-router-dom'

// Credentials is a section of the agent's Overview now. Old links and
// bookmarks keep working, and keep their query — the Google sign-in used to
// return here with `?google=ok`, and the outcome banner reads that.
export function CredentialsRedirect() {
  const { search } = useLocation()
  return <Navigate to={`../overview${search}#credentials`} replace />
}
