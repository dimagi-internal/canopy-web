import { Navigate, useLocation } from 'react-router-dom'

// `items` is Work now. The query travels: `?batch=` is one sitting (a fleet
// audit) and those links get handed to people.
export function WorkRedirect() {
  const { search } = useLocation()
  return <Navigate to={`../work${search}`} replace />
}
