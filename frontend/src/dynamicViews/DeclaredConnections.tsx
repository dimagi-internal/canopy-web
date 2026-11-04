import type { DeclaredConnection } from './connectionCalls'
import { describeLines } from '@/lib/connectionLines'

const title = (s = 'a connected account') => s.charAt(0).toUpperCase() + s.slice(1)

/**
 * What a view may do in a connected account — its declared connections — said where a reader of the
 * view sees it, beside the declared tasks. A view with none says nothing.
 */
export function DeclaredConnections({ connections, className = '' }: { connections: readonly DeclaredConnection[] | undefined; className?: string }) {
  if (!connections?.length) return null
  return (
    <div className={className}>
      {connections.map((c, i) => (
        <details key={`${c.provider}/${c.as}/${c.agent ?? ''}/${i}`} className="text-xs text-muted-foreground">
          <summary className="cursor-pointer">
            This page can use {title(c.provider)} as {c.as === 'shared' ? c.agent : 'you'}
          </summary>
          <ul className="mt-1 list-disc pl-4">
            {describeLines([...(c.reads ?? []), ...(c.writes ?? [])]).map((t) => <li key={t} className="text-foreground">{t}</li>)}
          </ul>
        </details>
      ))}
    </div>
  )
}
