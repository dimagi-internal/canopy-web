import { useEffect, useState, type JSX } from 'react'
import { listMyAddresses, type PersonAddress } from '@/api/people'
import { useAuth } from '@/auth/AuthProvider'
import { MyAddressesEditor } from './MyAddressesEditor'

/**
 * Your contact details (apps/contacts/models.py `PersonAddress`): other ways to
 * reach you — a work email, a phone — beside the login that identifies you.
 * Yours across every workspace; people and agents in a workspace you share can
 * read them, and only you change them.
 */
export function ContactDetailsPanel(): JSX.Element {
  const auth = useAuth()
  const me = auth.status === 'authenticated' ? auth.user.email : 'you'
  const [addresses, setAddresses] = useState<PersonAddress[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    listMyAddresses()
      .then((a) => {
        if (!cancelled) setAddresses(a)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setAddresses([])
          setError(e instanceof Error && e.message ? e.message : 'Failed to load your contact details')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <div id="contact-details" className="rounded-xl border border-border bg-card p-5" data-testid="my-contact-details">
      <h2 className="text-[10px] uppercase tracking-wider font-semibold text-muted-foreground">Contact details</h2>
      <p className="mt-1 text-xs text-muted-foreground">
        Other ways to reach you, such as a work email or a phone. People and agents in any workspace you share can see
        them, and agents use them (for example, to invite your work email to a family calendar event). Only you can
        change them.
      </p>
      {error && <div className="mt-2 text-[12px] text-destructive">{error}</div>}
      {addresses === null ? (
        <div className="mt-3 h-10 animate-pulse rounded-lg bg-muted" />
      ) : (
        <MyAddressesEditor addresses={addresses} who={me} onChange={setAddresses} />
      )}
    </div>
  )
}
