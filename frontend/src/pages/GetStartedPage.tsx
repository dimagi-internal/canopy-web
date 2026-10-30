import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '@/auth/AuthProvider'
import { CopyCommand } from '@/components/agents/CopyCommand'
import { RunnerDownloads } from '@/components/runners/RunnerDownloads'
import { listAgents } from '@/api/agents'
import { scopedPath } from '@/lib/scopedLinks'
import { pairingCommand } from '@/pages/topology/topologyMap'
import { useActiveWorkspace } from '@/workspace/WorkspaceProvider'

/**
 * /get-started — how a person starts using canopy agents: install canopy,
 * install an agent, set the agent up. Agents run in the person's OWN Claude
 * Code, set up from the agent's Canopy vault, not remotely through
 * canopy-web's MCP connector.
 *
 * Public (an exact-match allowlist entry in apps/common/middleware.py, beside
 * /about): step 1 is useful before someone has an account. There is ONE agent
 * catalog per person, `canopy-agents`, which `/canopy:setup` registers. It lists
 * PLUGINS (code), not instances: an agent installs as
 * `<plugin.name>@canopy-agents` and sets up with `/<plugin.name>:setup`, where
 * `plugin` comes from GET /api/agents/ (named exactly as the catalog names it).
 * Signed in, steps 2 and 3 list the agents in all of the reader's workspaces
 * that have a plugin, one line per plugin (two instances of one plugin install
 * once). Signed out, or when nothing loads, the page shows the ACE example.
 *
 * Below the steps: making an agent of your own (a link to the active workspace's
 * create page, when one is known) and, at #runner, pairing a laptop runner — the
 * runner step of an agent's setup checklist links here. A runner is a download
 * (Canopy Runner for macOS or Windows) approved in the browser; the
 * install-runner.sh path for a canopy-web checkout is a collapsed note.
 */

/** Where a cloud runner is stood up from: it needs AWS rights canopy-web does not hold. */
const CLOUD_RUNNER_README =
  'https://github.com/dimagi-internal/canopy-web/blob/main/runner/ec2/README.md#lifecycle--commands-only'

const CATALOG = 'canopy-agents'
const EXAMPLE_AGENT = 'ace'

interface ListedPlugin {
  /** The plugin's name in the canopy-agents catalog. */
  plugin: string
  name: string
  description: string
}

function InlineCode({ children }: { children: ReactNode }) {
  return <code className="rounded bg-muted px-1 py-0.5 font-mono text-[12px] text-foreground">{children}</code>
}

function Notes({ children }: { children: ReactNode }) {
  return (
    <ul className="mt-3 list-disc space-y-1 pl-5 text-[13px] leading-relaxed text-muted-foreground">{children}</ul>
  )
}

function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <li className="relative grid grid-cols-[2rem_minmax(0,1fr)] gap-4 pb-8 last:pb-0">
      <span
        aria-hidden="true"
        className="flex h-8 w-8 items-center justify-center rounded-full bg-primary text-sm font-semibold text-primary-foreground"
      >
        {n}
      </span>
      <section aria-labelledby={`step-${n}`} className="min-w-0">
        <h2 id={`step-${n}`} className="pt-1 text-base font-semibold text-foreground">
          <span className="sr-only">Step {n}: </span>
          {title}
        </h2>
        {children}
      </section>
    </li>
  )
}

function Panel({ title, id, children }: { title: string; id?: string; children: ReactNode }) {
  return (
    <div id={id} className="scroll-mt-4 rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      <div className="mt-1.5 text-[13px] leading-relaxed text-foreground-secondary">{children}</div>
    </div>
  )
}

export function GetStartedPage(): JSX.Element {
  const auth = useAuth()
  const authed = auth.status === 'authenticated'
  const workspace = useActiveWorkspace()
  // React Router does not scroll to a fragment, so `/get-started#runner` (linked from an
  // agent's setup checklist) would open at the top. The agent list loading below can
  // shift the section, so it scrolls again once that settles.
  const { hash } = useLocation()
  const [agents, setAgents] = useState<ListedPlugin[] | null>(null)

  useEffect(() => {
    if (!authed) return
    let cancelled = false
    listAgents({ limit: 200 })
      .then((page) => {
        if (cancelled) return
        const seen = new Set<string>()
        const listed: ListedPlugin[] = []
        for (const a of page.items) {
          const plugin = a.plugin?.name
          // No plugin = no github.com repo: it is in no catalog, so nothing to install.
          if (!plugin || seen.has(plugin)) continue
          seen.add(plugin)
          listed.push({ plugin, name: a.name || plugin, description: a.description })
        }
        setAgents(listed)
      })
      .catch(() => {
        // The example below stands in; the page is still complete without the list.
      })
    return () => {
      cancelled = true
    }
  }, [authed])

  useEffect(() => {
    if (!hash) return
    document.getElementById(decodeURIComponent(hash.slice(1)))?.scrollIntoView?.({ block: 'start' })
  }, [hash, agents])

  const listed = authed && agents && agents.length > 0 ? agents : null
  const setupPlugins = listed ? listed.map((a) => a.plugin) : [EXAMPLE_AGENT]
  const runnerCommand = pairingCommand(workspace ?? '<workspace>', `${window.location.origin}${import.meta.env.BASE_URL}`)

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-primary">Getting started</p>
      <h1 className="mt-1 text-2xl font-semibold text-foreground">Use a canopy agent in your own Claude Code</h1>
      <p className="mt-3 text-[15px] leading-relaxed text-foreground-secondary">
        Agents like ACE are Claude Code plugins. They run on your machine, in your own Claude Code, set up
        from the agent&apos;s Canopy vault. Three steps, about ten minutes.
      </p>
      <ol aria-label="The three steps" className="mt-4 flex flex-wrap gap-2">
        {['install canopy', 'install an agent', 'set up the agent'].map((label, i) => (
          <li key={label} className="rounded-full bg-primary/10 px-3 py-1 font-mono text-[12px] text-primary">
            <span className="font-semibold">{i + 1}</span> {label}
          </li>
        ))}
      </ol>

      <div className="mt-8">
        <Panel title="Before you start">
          You need <strong className="text-foreground">Claude Code</strong> and a{' '}
          <strong className="text-foreground">canopy account that belongs to a workspace</strong> (at Dimagi,{' '}
          <InlineCode>dimagi</InlineCode> or <InlineCode>connect</InlineCode>), and{' '}
          <strong className="text-foreground">read access to each agent&apos;s GitHub repo</strong> (ACE&apos;s
          is public; the others are private and need access granted on GitHub). If you don&apos;t have an
          account, ask a workspace admin to invite you. You don&apos;t need a runner.
        </Panel>
      </div>

      <ol className="mt-8">
        <Step n={1} title="Install canopy">
          <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">
            In Claude Code, add canopy&apos;s plugin catalog, install the canopy plugin, then run its setup.
          </p>
          <div className="mt-3 space-y-2">
            <CopyCommand command="/plugin marketplace add dimagi-internal/canopy" />
            <CopyCommand command="/plugin install canopy@canopy" />
            <CopyCommand command="/reload-plugins" />
            <CopyCommand command="/canopy:setup" />
          </div>
          <Notes>
            <li>
              Setup opens a browser tab. Click <strong className="text-foreground">Authorize</strong> to sign in
              with your canopy account.
            </li>
            <li>
              Setup then adds your agent catalog, <InlineCode>{CATALOG}</InlineCode>, which lists every agent in
              the workspaces you belong to, and prints the install line for each.
            </li>
            <li>Setup doesn&apos;t install any agents. You choose them in step 2.</li>
          </Notes>
        </Step>

        <Step n={2} title="Install an agent">
          <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">
            {listed
              ? 'Install the agents you want from your catalog. These are the agents in your workspaces; every one installs the same way:'
              : 'Install the agents you want from your catalog. Every agent installs the same way:'}
          </p>
          {listed ? (
            <ul className="mt-3 space-y-3" aria-label="Agents you can install">
              {listed.map((a) => (
                <li key={a.plugin}>
                  <p className="text-[13px] font-medium text-foreground">
                    {a.name}
                    {a.description ? (
                      <span className="font-normal text-muted-foreground"> — {a.description}</span>
                    ) : null}
                  </p>
                  <CopyCommand command={`/plugin install ${a.plugin}@${CATALOG}`} />
                </li>
              ))}
            </ul>
          ) : (
            <div className="mt-3">
              <CopyCommand command={`/plugin install ${EXAMPLE_AGENT}@${CATALOG}`} />
            </div>
          )}
          <div className="mt-2">
            <CopyCommand command="/reload-plugins" />
          </div>
          <Notes>
            <li>
              Or browse them in <InlineCode>/plugin</InlineCode> → Marketplaces → <InlineCode>{CATALOG}</InlineCode>.
            </li>
            <li>
              The plugin downloads from the agent&apos;s GitHub repo with your own GitHub access. If the install
              fails with a GitHub error, ask the agent&apos;s owner for repo access.
            </li>
            <li>Agents update themselves whenever their team ships a change.</li>
          </Notes>
        </Step>

        <Step n={3} title="Set up the agent">
          <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">
            Each agent has its own setup command. It loads the agent&apos;s settings and logins from the
            agent&apos;s Canopy vault, then checks everything works.
          </p>
          <div className="mt-3 space-y-2">
            {setupPlugins.map((p) => (
              <CopyCommand key={p} command={`/${p}:setup`} />
            ))}
          </div>
          <Notes>
            <li>
              You need access to the agent&apos;s Canopy vault. If you don&apos;t have it, ask the agent&apos;s owner.
            </li>
            <li>Setup tells you exactly what&apos;s missing, and you can run it again any time.</li>
          </Notes>
        </Step>
      </ol>

      <div className="mt-8 grid gap-4 sm:grid-cols-2">
        <Panel title="Make an agent of your own">
          A new agent starts from an empty GitHub repo you make; canopy fills it with the agent scaffold and
          then walks you through the rest of its setup.{' '}
          {authed && workspace ? (
            <Link to={scopedPath(workspace, '/agents/new')} className="font-medium text-primary hover:underline">
              Create an agent
            </Link>
          ) : (
            'Sign in and open a workspace to create one.'
          )}
        </Panel>
        <Panel id="runner" title="Pair a runner">
          A runner is the computer that runs an agent&apos;s turns when someone asks it for something through
          canopy (a task, an email, Slack). You need one to run an agent for others, not to use one yourself.
          <h4 className="mt-3 text-[13px] font-semibold text-foreground">Download Canopy Runner</h4>
          <div className="mt-1">
            <RunnerDownloads authed={authed} developerCommand={runnerCommand} />
          </div>
          <Notes>
            <li>A runner takes an agent&apos;s turns only if you are one of that agent&apos;s admins.</li>
            <li>
              A cloud box is stood up from the{' '}
              <a href={CLOUD_RUNNER_README} target="_blank" rel="noreferrer" className="text-primary hover:underline">
                cloud runner guide
              </a>{' '}
              and needs AWS access.
            </li>
          </Notes>
        </Panel>
        <Panel title="Then just ask">
          Talk to Claude as usual and name the agent: &ldquo;Use ACE to explain how a Connect opportunity is
          set up.&rdquo; The agent&apos;s skills also appear as slash commands, such as{' '}
          <InlineCode>/ace:explain-ace</InlineCode>.
        </Panel>
        <Panel title="The canopy-web connector is separate">
          Canopy also offers a connector for using canopy-web itself from Claude (your board, sessions,
          settings). It isn&apos;t how you use an agent. To use an agent, install it as above.
        </Panel>
      </div>
    </div>
  )
}
