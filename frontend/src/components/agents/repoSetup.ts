// Getting an agent's repo onto a laptop runner, as the commands to run there.
//
// A laptop runs an agent turn in the emdash project named after the agent's slug,
// and reports the names of the emdash projects it has; one without that project
// can never take the agent's work. Canopy never fetches it for you — a clone onto
// someone's machine is theirs to start — so it hands over the commands instead.

/** A shell to write the command for: a macOS/Linux shell, or Windows PowerShell,
 *  where `~` reaches git unexpanded and `$HOME` is the home folder. */
export type Shell = 'posix' | 'powershell'

/** The folder the runner's own checkout convention uses (runner/canopy_runner/README.md). */
export function repoFolder(slug: string, shell: Shell = 'posix'): string {
  return shell === 'powershell' ? `$HOME\\emdash-projects\\${slug}` : `~/emdash-projects/${slug}`
}

/** The clone, or null when the agent has no repo to clone. */
export function cloneCommand(slug: string, repoUrl: string, shell: Shell = 'posix'): string | null {
  const url = repoUrl.trim()
  return url ? `git clone ${url} ${repoFolder(slug, shell)}` : null
}
