import { useEffect, useState } from 'react'

/** The URL hash, kept current (a deep link such as #vault-key opens a section). */
export function useHash(): string {
  const [hash, setHash] = useState(() => window.location.hash)
  useEffect(() => {
    const on = () => setHash(window.location.hash)
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return hash
}
