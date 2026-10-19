import { useEffect, useState, type JSX } from 'react'
import { getRunnerDownloads, RunnerPairingError, type RunnerDownloadOut } from '@/api/runnerPairings'
import { currentNext, loginHref } from '@/auth/loginHref'

type State =
  | { kind: 'loading' }
  | { kind: 'signed-out' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; sha: string; downloads: readonly RunnerDownloadOut[] }

function megabytes(size: number): string {
  return size > 0 ? `${(size / (1024 * 1024)).toFixed(0)} MB` : ''
}

function buildNote(d: RunnerDownloadOut): string {
  if (d.signed && (d.notarized || !d.platform.startsWith('macos'))) return 'Signed'
  if (d.platform.startsWith('windows')) return 'Unsigned build: Windows SmartScreen will warn — choose More info → Run anyway'
  return 'Unsigned build: macOS will block it — System Settings → Privacy & Security → Open Anyway'
}

/**
 * Download Canopy Runner — the installers of the runner version this Canopy
 * runs (GET /api/harness/runner-releases/downloads), then open it and approve
 * the pairing in the browser. Signed out: a sign-in link, since the downloads
 * are for signed-in people. `developerCommand` is the install-runner.sh line
 * for someone working from a canopy-web checkout, kept collapsed.
 */
export function RunnerDownloads({ authed, developerCommand }: { authed: boolean; developerCommand: string }): JSX.Element {
  const [state, setState] = useState<State>(authed ? { kind: 'loading' } : { kind: 'signed-out' })

  useEffect(() => {
    if (!authed) {
      setState({ kind: 'signed-out' })
      return
    }
    let cancelled = false
    setState({ kind: 'loading' })
    getRunnerDownloads()
      .then((r) => {
        if (!cancelled) setState({ kind: 'ready', sha: r.sha, downloads: r.downloads })
      })
      .catch((e: unknown) => {
        if (cancelled) return
        if (e instanceof RunnerPairingError && e.status === 401) setState({ kind: 'signed-out' })
        else if (e instanceof RunnerPairingError && e.status === 503)
          setState({ kind: 'error', message: 'Downloads are not set up on this Canopy.' })
        else setState({ kind: 'error', message: e instanceof Error ? e.message : 'Could not load the downloads' })
      })
    return () => {
      cancelled = true
    }
  }, [authed])

  const base = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')

  return (
    <div className="flex flex-col gap-2 text-[13px]" data-testid="runner-downloads">
      <ol className="m-0 list-decimal space-y-0.5 pl-5 text-foreground-secondary">
        <li>Download Canopy Runner for your computer.</li>
        <li>Open it. It installs for your account only, with no administrator password.</li>
        <li>It opens this site in your browser: check the code matches, pick a workspace, and approve.</li>
      </ol>

      {state.kind === 'loading' && <div className="h-16 animate-pulse rounded-md bg-muted" />}
      {state.kind === 'signed-out' && (
        <p className="m-0">
          <a href={loginHref(currentNext())} className="font-medium text-primary hover:underline">
            Sign in to download
          </a>
        </p>
      )}
      {state.kind === 'error' && <p className="m-0 text-destructive">{state.message}</p>}
      {state.kind === 'ready' && state.downloads.length === 0 && (
        <p className="m-0 text-muted-foreground">
          No installer has been built yet for the runner version this Canopy runs. Builds appear a few minutes after
          a change ships.
        </p>
      )}
      {state.kind === 'ready' && state.downloads.length > 0 && (
        <table className="w-full text-left text-[13px]" aria-label="Canopy Runner downloads">
          <thead className="text-[12px] text-muted-foreground">
            <tr>
              <th className="py-1 pr-3 font-medium">Computer</th>
              <th className="py-1 pr-3 font-medium">Size</th>
              <th className="py-1 pr-3 font-medium">Build</th>
              <th className="py-1 font-medium">
                <span className="sr-only">Download</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {state.downloads.map((d) => (
              <tr key={d.platform} className="border-t border-border" data-testid="runner-download">
                <td className="py-1 pr-3 text-foreground">{d.label}</td>
                <td className="py-1 pr-3 whitespace-nowrap text-muted-foreground">{megabytes(d.size)}</td>
                <td className={d.signed ? 'py-1 pr-3 text-muted-foreground' : 'py-1 pr-3 text-warning'}>{buildNote(d)}</td>
                <td className="py-1 text-right">
                  <a
                    href={`${base}${d.href}`}
                    className="inline-flex min-h-8 items-center rounded-md bg-primary px-3 text-[12px] font-medium text-primary-foreground hover:bg-primary/90"
                  >
                    Download
                  </a>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <details className="text-[12px] text-muted-foreground">
        <summary className="cursor-pointer select-none hover:text-foreground">Developers</summary>
        <p className="mt-1">
          From a canopy-web checkout, <code className="font-mono">runner/canopy_runner/scripts/install-runner.sh</code>{' '}
          still installs a runner with <code className="font-mono">uv</code> and your canopy token:
        </p>
        <code className="mt-1 block break-all rounded-md border border-input bg-input p-2 font-mono text-foreground">
          {developerCommand}
        </code>
        <p className="mt-1">
          Both installs run the same runner and pairing: opening the app on a developer account takes it over, and
          re-running <code className="font-mono">install-runner.sh</code> switches back.{' '}
          <code className="font-mono">touch ~/.canopy/hold-updates</code> keeps a box on the build you installed.
          To try the app without touching your runner, use a second macOS account.
        </p>
      </details>
    </div>
  )
}
