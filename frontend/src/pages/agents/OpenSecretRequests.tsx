import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { listSecretRequests, type SecretRequestOut } from '@/api/vault'
import { SecretSealCard } from '@/components/vault/SecretSealCard'
import { useScopedPath } from '@/lib/scopedLinks'

// What the agent asked for and nobody has answered yet, on its Credentials
// page. A request made outside any of its chats has no card anywhere else; one
// made in a chat is listed too, so an admin can answer it from here.
export function OpenSecretRequests({ slug, onStored }: { slug: string; onStored?: () => void }) {
  const [open, setOpen] = useState<SecretRequestOut[]>([])
  const scoped = useScopedPath()

  useEffect(() => {
    let live = true
    listSecretRequests(slug).then((r) => live && setOpen(r), () => undefined)
    return () => {
      live = false
    }
  }, [slug])

  if (open.length === 0) return null
  return (
    <section aria-labelledby="secret-requests-title" className="mt-6" data-testid="open-secret-requests">
      <h3 id="secret-requests-title" className="mb-1 text-[13px] font-semibold text-foreground">It is asking for</h3>
      {open.map((r) => (
        <div key={r.id}>
          <p className="text-[11px] text-muted-foreground">
            {r.session_id
              ? <>Asked in <Link className="underline" to={scoped(`/chat/${r.session_id}`)}>a chat</Link></>
              : 'Asked outside any of its chats'}
          </p>
          <SecretSealCard slug={slug} name={r.name} requestId={r.id} onStored={onStored} />
        </div>
      ))}
    </section>
  )
}
