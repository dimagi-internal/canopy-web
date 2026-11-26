import { Fragment } from 'react'
import type { DeclaredConnection } from './connectionCalls'
import { ConnectionLine } from '@/lib/ConnectionLine'
import { describeLines } from '@/lib/connectionLines'

const title = (s = 'a connected account') => s.charAt(0).toUpperCase() + s.slice(1)

/** "your Salesforce", "your Salesforce and your Google", "Salesforce as eva". */
function accountsWords(connections: readonly DeclaredConnection[]): string {
  const seen = new Set<string>()
  const words: string[] = []
  for (const c of connections) {
    const w = c.as === 'shared' ? `${title(c.provider)} as ${c.agent}` : `your ${title(c.provider)}`
    if (!seen.has(w)) { seen.add(w); words.push(w) }
  }
  if (words.length <= 1) return words[0] ?? ''
  return `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`
}

/**
 * What a screen may do in a connected account — its declared connections — said where a reader of it
 * sees it, beside the declared tasks. A screen with none says nothing.
 *
 * One line by default ("This screen uses your Salesforce. What it can do"): the full list of what it
 * may do is one click away, not a paragraph pushing the screen itself below the fold.
 */
export function DeclaredConnections({ connections, className = '' }: { connections: readonly DeclaredConnection[] | undefined; className?: string }) {
  if (!connections?.length) return null
  const described = connections
    .map((c) => ({ c, shared: c.as === 'shared', lines: describeLines(c, c.as === 'shared' ? c.agent : 'you') }))
    .filter((d) => d.lines.length)
  if (!described.length) return null
  return (
    <details className={`text-xs text-muted-foreground ${className}`}>
      <summary className="cursor-pointer select-none">
        This screen uses {accountsWords(described.map((d) => d.c))}. <span className="underline">What it can do</span>
      </summary>
      {described.map(({ c, shared, lines }, i) => (
        <p key={`${c.provider}/${c.as}/${c.agent ?? ''}/${i}`} className="mt-1">
          {shared ? `This screen acts as ${c.agent} in ${title(c.provider)}: ` : `This screen can use your ${title(c.provider)}: `}
          {lines.map((line, n) => (
            <Fragment key={line.code ?? line.words}>
              {n > 0 && ', '}
              <ConnectionLine line={line} />
            </Fragment>
          ))}
        </p>
      ))}
    </details>
  )
}
