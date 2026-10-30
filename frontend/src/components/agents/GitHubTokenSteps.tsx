import type { JSX } from 'react'

/** How to make the fine-grained token an agent acts on GitHub with — shared by
 *  the create page (where the same token also pushes the scaffold) and the
 *  agent's External accounts → GitHub. `tokenUrl` is GitHub's form, filled in by the
 *  server (`delegations.token_form_url`); GitHub cannot pre-pick repositories,
 *  so steps 2–3 say what to choose. */
export function GitHubTokenSteps({ tokenUrl, repo }: { tokenUrl: string; repo: string }): JSX.Element {
  return (
    <ol className="mb-2 list-decimal space-y-0.5 pl-4 text-[11px] text-muted-foreground">
      <li>
        <a href={tokenUrl} target="_blank" rel="noreferrer" className="text-primary">
          Create the token on GitHub
        </a>{' '}
        — the form opens filled in.
      </li>
      <li>
        Check <strong>Resource owner</strong> reads{' '}
        <span className="font-mono">{repo ? repo.split('/')[0] : 'the repo’s org'}</span>.
      </li>
      <li>
        Under <strong>Only select repositories</strong>, pick{' '}
        {repo ? <span className="font-mono">{repo}</span> : 'this agent’s repo'}, plus any other repo it ships to
        or installs a plugin from.
      </li>
      <li>Generate it, and paste it here.</li>
    </ol>
  )
}
