// The pieces of the GitHub panel: one installation's row, the scope advice, the connect link and the card and note frames. The panel and its state are `GitHubPanel`.
import {
  gitHubConnectPath,
  type GitHubInstallationOut,
} from '@/api/github'

/**
 * What one installation actually reaches.
 *
 * Naming the repositories rather than only the account is the point. Reporting
 * "access granted on dimagi-internal" for an installation scoped to a single
 * repo reads like the whole organisation — it OVERSTATES the grant to the
 * person reading it, which is the opposite of useful when the advice above was
 * "scope it narrowly". If canopy tells people to pick carefully, it has to show
 * what they picked.
 *
 * An "all repositories" grant is called out rather than listed: enumerating it
 * could be thousands of rows, and it is the one grant the advice exists to
 * steer people away from, so it should read as a choice worth revisiting.
 */
export function InstallationRow({ install }: { install: GitHubInstallationOut }) {
  // `repositories` is optional on the wire (it has a server-side default), and
  // `grants_all_repositories` is a service-layer property rather than a
  // serialized field — so the "all" case is derived from the one thing GitHub
  // actually tells us.
  const repos = install.repositories ?? []
  const grantsAll = install.repository_selection === 'all'
  const more = install.repository_count - repos.length
  return (
    <div className="rounded border border-border bg-muted/40 p-2.5">
      <p className="text-[13px]">
        <span className="font-medium text-foreground">{install.account_login}</span>
        <span className="text-muted-foreground">
          {install.is_org ? ' (organisation)' : ' (your account)'}
        </span>
      </p>
      {grantsAll ? (
        <p className="mt-1 text-[12px] text-warning">
          All repositories — canopy can reach every repository this account can see. Narrow
          it if you did not mean that.
        </p>
      ) : repos.length > 0 ? (
        <ul className="mt-1 space-y-0.5">
          {repos.map((full) => (
            <li key={full} className="text-[12px] text-foreground-secondary">
              {full}
            </li>
          ))}
          {more > 0 && (
            <li className="text-[12px] text-muted-foreground">
              and {more} more
            </li>
          )}
        </ul>
      ) : (
        <p className="mt-1 text-[12px] text-muted-foreground">
          No repositories listed. Add the ones you want agents working in.
        </p>
      )}
    </div>
  )
}

/**
 * The scope guidance, shown on the buttons that lead to the INSTALL flow —
 * which is the only screen where the choice it describes actually appears.
 * Putting it on the authorize button (the first version) described a picker the
 * user was never shown.
 */
export function ScopeAdvice() {
  return (
    <p className="text-[13px] text-muted-foreground">
      GitHub will ask which repositories to grant. Choose{' '}
      <span className="font-medium text-foreground">Only select repositories</span> and pick
      just the ones you want agents working in — canopy cannot create or delete
      repositories, so it only ever reaches what you list here.
    </p>
  )
}

/**
 * A real anchor, not a Button with onClick: this is a full-page navigation out
 * of the SPA (GitHub has to show its own consent screen), and a link keeps
 * middle-click and "copy link" working.
 */
export function ConnectLink({ label }: { label: string }) {
  return (
    <a
      href={gitHubConnectPath()}
      className="inline-flex items-center rounded bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground hover:bg-primary/90"
    >
      {label}
    </a>
  )
}

export function Card({ children }: { children: React.ReactNode }) {
  return <div className="rounded-xl border border-border bg-card p-5 space-y-4">{children}</div>
}

export function Note({
  tone,
  children,
  onDismiss,
}: {
  tone: 'success' | 'info' | 'error'
  children: React.ReactNode
  onDismiss: () => void
}) {
  const toneClass =
    tone === 'success'
      ? 'border-success/30 bg-success/10 text-success'
      : tone === 'error'
        ? 'border-destructive/30 bg-destructive/10 text-destructive'
        : 'border-info/30 bg-info/10 text-info'
  return (
    <div className={`flex items-start justify-between gap-3 rounded border p-3 ${toneClass}`}>
      <p className="text-[13px]">{children}</p>
      <button
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss"
        className="shrink-0 text-[13px] opacity-70 hover:opacity-100"
      >
        ×
      </button>
    </div>
  )
}
