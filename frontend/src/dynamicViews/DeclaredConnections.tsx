import { Fragment } from 'react'
import type { DeclaredConnection } from './connectionCalls'
import { ConnectionLine } from '@/lib/ConnectionLine'
import { describeLines } from '@/lib/connectionLines'

const title = (s = 'a connected account') => s.charAt(0).toUpperCase() + s.slice(1)

/**
 * What a screen may do in a connected account — its declared connections — said where a reader of it
 * sees it, beside the declared tasks. A screen with none says nothing.
 */
export function DeclaredConnections({ connections, className = '' }: { connections: readonly DeclaredConnection[] | undefined; className?: string }) {
  if (!connections?.length) return null
  return (
    <div className={className}>
      {connections.map((c, i) => {
        const shared = c.as === 'shared'
        const lines = describeLines(c, shared ? c.agent : 'you')
        if (!lines.length) return null
        return (
          <p key={`${c.provider}/${c.as}/${c.agent ?? ''}/${i}`} className="text-xs text-muted-foreground">
            {shared ? `This screen acts as ${c.agent} in ${title(c.provider)}: ` : `This screen can use your ${title(c.provider)}: `}
            {lines.map((line, n) => (
              <Fragment key={line.code ?? line.words}>
                {n > 0 && ', '}
                <ConnectionLine line={line} />
              </Fragment>
            ))}
          </p>
        )
      })}
    </div>
  )
}
