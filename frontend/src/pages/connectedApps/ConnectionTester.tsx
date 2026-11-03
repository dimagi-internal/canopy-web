import { useState, type JSX } from 'react'
import { Button } from 'canopy-ui/ui'
import { testConnectedApp, type ConnectedApp, type ConnectionCheck, type ConnectionTest } from '@/api/connectedApps'
import { toneClasses } from 'canopy-ui/lib'
import { Td, Th, Tr } from '@/components/DataTable'

export const STATUS_STYLE: Record<ConnectionCheck['status'], string> = {
  pass: toneClasses('success'),
  fail: toneClasses('destructive'),
  skip: toneClasses('neutral'),
}

/** One row per check: status, what, why. Shared by the settings checks and the
 *  live probe, which report in the same shape. */
function CheckRows({ checks }: { checks: readonly ConnectionCheck[] }): JSX.Element {
  return (
    <table className="w-full border-collapse text-xs">
      <thead>
        <Tr rule="bottom" head="plain">
          <Th density="compact-gutter" weight="normal" className="w-14">Result</Th>
          <Th density="compact-gutter" weight="normal">Check</Th>
          <Th weight="normal">Detail</Th>
        </Tr>
      </thead>
      <tbody>
        {checks.map((c, i) => (
          <Tr rule="bottom-last" top key={`${c.name}-${i}`}>
            <Td density="compact-gutter">
              <span className={`inline-block rounded border px-1.5 text-[10px] uppercase ${STATUS_STYLE[c.status]}`}>
                {c.status}
              </span>
            </Td>
            <Td density="compact-gutter" tone="foreground" title={c.name}>
              {c.label}
            </Td>
            <Td tone="secondary" mono className="break-all text-[11px]">{c.detail}</Td>
          </Tr>
        ))}
      </tbody>
    </table>
  )
}

/** What the live probe concluded, in one sentence. */
export function liveProbeSummary(result: Pick<ConnectionTest, 'live_probe' | 'live_probe_ok'>): string {
  const steps = result.live_probe ?? []
  if (result.live_probe_ok === true) {
    return 'Live probe passed: a real grant was issued, redeemed and used.'
  }
  const failed = steps.find((s) => s.status === 'fail')
  if (result.live_probe_ok === false && failed) return `Live probe failed: ${failed.label}.`
  const skipped = steps.find((s) => s.status === 'skip')
  return `Live probe not run: ${skipped?.detail || 'no verdict'}.`
}

/** The checks a connection test ran — the settings, then the live probe. */
export function ConnectionTestTable({ result }: { result: ConnectionTest }): JSX.Element {
  const failed = result.checks.filter((c) => c.status === 'fail').length
  const probe = result.live_probe ?? []
  return (
    <div className="space-y-3">
      <div className="space-y-1">
        <p className={`text-xs ${failed === 0 ? 'text-success' : 'text-destructive'}`}>
          {failed === 0
            ? 'Every check passed.'
            : `${failed} of ${result.checks.length} checks failed.`}
        </p>
        <CheckRows checks={result.checks} />
      </div>
      {probe.length > 0 && (
        <div className="space-y-1">
          <h3 className="text-xs font-medium text-foreground">Live probe</h3>
          <p
            className={`text-xs ${
              result.live_probe_ok === true
                ? 'text-success'
                : result.live_probe_ok === false
                  ? 'text-destructive'
                  : 'text-muted-foreground'
            }`}
          >
            {liveProbeSummary(result)}
          </p>
          <CheckRows checks={probe} />
        </div>
      )}
    </div>
  )
}

/** "Test connection": runs canopy's conformance checks against this site's
 *  settings, from canopy's server, and shows the table. */
export function ConnectionTester({ slug, app }: { slug: string; app: ConnectedApp }): JSX.Element {
  const [result, setResult] = useState<ConnectionTest | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onTest() {
    setRunning(true)
    setError(null)
    try {
      setResult(await testConnectedApp(slug, app.id))
    } catch (e) {
      setResult(null)
      setError(e instanceof Error ? e.message : 'Could not test the connection.')
    } finally {
      setRunning(false)
    }
  }

  return (
    <div className="space-y-2 border-t border-border pt-2">
      <div className="flex items-center gap-3">
        <Button size="sm" variant="outline" disabled={running} onClick={() => void onTest()}>
          {running ? 'Testing…' : 'Test connection'}
        </Button>
        <span className="text-xs text-muted-foreground">
          Reads its keys and discovery documents, asks whether it accepts canopy, then runs the live
          probe — from canopy&apos;s server.
        </span>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {result && <ConnectionTestTable result={result} />}
    </div>
  )
}
