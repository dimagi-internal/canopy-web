import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { useAuth } from '@/auth/AuthProvider'
import { useWorkspace } from '@/workspace/WorkspaceProvider'
import { CopyCommand } from '@/components/agents/CopyCommand'
import { catalogName, loadCatalogs, type WorkspaceCatalog } from './getStarted'

/**
 * /get-started — how a person starts using canopy agents: install canopy,
 * install an agent, set the agent up. Agents run in the person's OWN Claude
 * Code with their own logins, not remotely through canopy-web's MCP connector.
 *
 * Public (an exact-match allowlist entry in apps/common/middleware.py, beside
 * /about): step 1 is useful before someone has an account. Signed in, steps 2
 * and 3 list the agents each of the person's workspaces actually offers, read
 * from that workspace's plugin marketplace (/w/<ws>/marketplace.json,
 * apps/plugins) — the catalog Claude Code installs from. Catalogs are per
 * workspace (`canopy-<workspace>`, no child workspaces), so an agent's install
 * line names ITS workspace, not whichever one the reader has open. Signed out,
 * or when nothing loads, the page shows the ACE example (ACE lives in connect).
 */

const EXAMPLE_AGENT = 'ace'
const EXAMPLE_CATALOG = catalogName('connect')

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

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      <div className="mt-1.5 text-[13px] leading-relaxed text-foreground-secondary">{children}</div>
    </div>
  )
}

export function GetStartedPage(): JSX.Element {
  const auth = useAuth()
  const { workspaces } = useWorkspace()
  const authed = auth.status === 'authenticated'
  const slugs = authed ? workspaces.map((w) => w.slug) : []
  const slugKey = slugs.join(',')
  // Keyed on the workspace list it was loaded for, so a stale result is never
  // shown for a different list.
  const [loaded, setLoaded] = useState<{ key: string; catalogs: WorkspaceCatalog[] } | null>(null)
  const catalogs = loaded && loaded.key === slugKey ? loaded.catalogs : null

  useEffect(() => {
    if (!slugKey) return
    let cancelled = false
    void loadCatalogs(slugKey.split(',')).then((found) => {
      if (!cancelled) setLoaded({ key: slugKey, catalogs: found })
    })
    return () => {
      cancelled = true
    }
  }, [slugKey])

  const listed = catalogs && catalogs.length > 0 ? catalogs : null
  const setupPlugins = listed
    ? [...new Set(listed.flatMap((c) => c.agents.map((a) => a.plugin)))]
    : [EXAMPLE_AGENT]

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <p className="text-[11px] font-semibold uppercase tracking-wider text-primary">Getting started</p>
      <h1 className="mt-1 text-2xl font-semibold text-foreground">Use a canopy agent in your own Claude Code</h1>
      <p className="mt-3 text-[15px] leading-relaxed text-foreground-secondary">
        Agents like ACE are Claude Code plugins. You install them into your own Claude Code and they run
        there, with your own logins. Three steps, about ten minutes.
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
          <InlineCode>dimagi</InlineCode> or <InlineCode>connect</InlineCode>). If you don&apos;t have one, ask a
          workspace admin to invite you. You don&apos;t need a GitHub account, 1Password, or a runner.
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
              Setup then adds a private agent catalog for each workspace you belong to, named{' '}
              <InlineCode>canopy-&lt;workspace&gt;</InlineCode>, and lists the agents you can install.
            </li>
            <li>Setup doesn&apos;t install any agents. You choose them in step 2.</li>
          </Notes>
        </Step>

        <Step n={2} title="Install an agent">
          {listed ? (
            <>
              <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">
                Install the agents you want. Each one installs from its own workspace&apos;s catalog; these
                are the agents your workspaces offer right now.
              </p>
              {listed.map((c) => (
                <section key={c.workspace} className="mt-4" aria-label={`Agents in ${c.catalog}`}>
                  <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">
                    {c.catalog}
                  </h3>
                  <ul className="mt-2 space-y-3">
                    {c.agents.map((a) => (
                      <li key={a.plugin}>
                        <p className="text-[13px] font-medium text-foreground">
                          {a.plugin}
                          {a.description ? (
                            <span className="font-normal text-muted-foreground"> — {a.description}</span>
                          ) : null}
                        </p>
                        <CopyCommand command={`/plugin install ${a.plugin}@${c.catalog}`} />
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </>
          ) : (
            <>
              <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">
                Install the agents you want from your workspace&apos;s catalog. The example installs ACE,
                which lives in the <InlineCode>connect</InlineCode> workspace.
              </p>
              <div className="mt-3">
                <CopyCommand command={`/plugin install ${EXAMPLE_AGENT}@${EXAMPLE_CATALOG}`} />
              </div>
            </>
          )}
          <div className="mt-2">
            <CopyCommand command="/reload-plugins" />
          </div>
          <Notes>
            <li>
              Or browse the agents in <InlineCode>/plugin</InlineCode> → Marketplaces →{' '}
              <InlineCode>canopy-&lt;workspace&gt;</InlineCode>.
            </li>
            <li>Agents update themselves whenever their team ships a change.</li>
          </Notes>
        </Step>

        <Step n={3} title="Set up the agent">
          <p className="mt-1 text-[13px] leading-relaxed text-foreground-secondary">
            Each agent has its own setup command. It checks what the agent can already do for you and tells
            you which of your own logins unlock the rest.
          </p>
          <div className="mt-3 space-y-2">
            {setupPlugins.map((p) => (
              <CopyCommand key={p} command={`/${p}:setup`} />
            ))}
          </div>
          <Notes>
            <li>Reading and explaining work immediately, with no logins.</li>
            <li>
              Doing things in a system (CommCare HQ, Connect, Connect Labs, OCS, Google Drive) uses{' '}
              <em>your</em> account there. Setup gives you one command per system to connect it.
            </li>
            <li>Run it again any time. It only reports what&apos;s still missing.</li>
          </Notes>
        </Step>
      </ol>

      <div className="mt-8 grid gap-4 sm:grid-cols-2">
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
