import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { useAuth } from '@/auth/AuthProvider'
import { CopyCommand } from '@/components/agents/CopyCommand'
import { listAgents } from '@/api/agents'

/**
 * /get-started — how a person starts using canopy agents: install canopy,
 * install an agent, set the agent up. Agents run in the person's OWN Claude
 * Code with their own logins, not remotely through canopy-web's MCP connector.
 *
 * Public (an exact-match allowlist entry in apps/common/middleware.py, beside
 * /about): step 1 is useful before someone has an account. There is ONE agent
 * catalog per person, `canopy-agents`, which `/canopy:setup` registers; every
 * agent installs as `<slug>@canopy-agents`. Signed in, steps 2 and 3 list the
 * agents in all of the reader's workspaces (the existing GET /api/agents/,
 * which, read off a non-tenant URL, spans every workspace the reader can see).
 * Signed out, or when nothing loads, the page shows the ACE example.
 */

const CATALOG = 'canopy-agents'
const EXAMPLE_AGENT = 'ace'

interface ListedAgent {
  slug: string
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
  const authed = auth.status === 'authenticated'
  const [agents, setAgents] = useState<ListedAgent[] | null>(null)

  useEffect(() => {
    if (!authed) return
    let cancelled = false
    listAgents({ limit: 200 })
      .then((page) => {
        if (cancelled) return
        const seen = new Set<string>()
        setAgents(
          page.items
            .filter((a) => !seen.has(a.slug) && seen.add(a.slug))
            .map((a) => ({ slug: a.slug, name: a.name || a.slug, description: a.description })),
        )
      })
      .catch(() => {
        // The example below stands in; the page is still complete without the list.
      })
    return () => {
      cancelled = true
    }
  }, [authed])

  const listed = authed && agents && agents.length > 0 ? agents : null
  const setupSlugs = listed ? listed.map((a) => a.slug) : [EXAMPLE_AGENT]

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
          <InlineCode>dimagi</InlineCode> or <InlineCode>connect</InlineCode>), and{' '}
          <strong className="text-foreground">read access to each agent&apos;s GitHub repo</strong> (ACE&apos;s
          is public; the others are private and need access granted on GitHub). If you don&apos;t have an
          account, ask a workspace admin to invite you. To set up an agent you also need the{' '}
          <strong className="text-foreground">1Password CLI</strong>, signed in, with access to that
          agent&apos;s vault. You don&apos;t need a runner.
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
                <li key={a.slug}>
                  <p className="text-[13px] font-medium text-foreground">
                    {a.name}
                    {a.description ? (
                      <span className="font-normal text-muted-foreground"> — {a.description}</span>
                    ) : null}
                  </p>
                  <CopyCommand command={`/plugin install ${a.slug}@${CATALOG}`} />
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
            agent&apos;s 1Password vault, then checks everything works.
          </p>
          <div className="mt-3 space-y-2">
            {setupSlugs.map((p) => (
              <CopyCommand key={p} command={`/${p}:setup`} />
            ))}
          </div>
          <Notes>
            <li>
              You need the 1Password CLI (<InlineCode>op</InlineCode>) signed in to{' '}
              <InlineCode>dimagi.1password.com</InlineCode>, with access to the agent&apos;s vault (ACE&apos;s is{' '}
              <InlineCode>Agent-Ace</InlineCode>). If you don&apos;t have access, ask the agent&apos;s owner.
            </li>
            <li>Setup tells you exactly what&apos;s missing, and you can run it again any time.</li>
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
