import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { Button } from 'canopy-ui/ui'
import { useAuth } from '@/auth/AuthProvider'
import { loginHref } from '@/auth/loginHref'
import { getCsrfToken } from '@/api/base'
import { acceptInvite, previewInvite, WorkspaceApiError, type InvitePreviewOut } from '@/api/workspaces'

type ViewState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'mismatch' }
  | { kind: 'preview'; preview: InvitePreviewOut }

const TERMINAL_MESSAGE: Record<string, string> = {
  expired: 'This invite link has expired. Ask the workspace owner to send you a new one.',
  revoked: 'This invite has been revoked.',
  accepted: 'This invite has already been accepted.',
}

function Centered({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div className="flex min-h-[60vh] items-center justify-center px-6">
      <div className="max-w-md w-full rounded-xl border border-border bg-card p-8 text-center">
        {children}
      </div>
    </div>
  )
}

export function InviteAcceptPage(): JSX.Element | null {
  const { token } = useParams()
  const auth = useAuth()
  const navigate = useNavigate()
  const [state, setState] = useState<ViewState>({ kind: 'loading' })
  const [accepting, setAccepting] = useState(false)

  useEffect(() => {
    if (!token) return
    let cancelled = false
    setState({ kind: 'loading' })
    previewInvite(token)
      .then((preview) => {
        if (!cancelled) setState({ kind: 'preview', preview })
      })
      .catch((e: unknown) => {
        if (cancelled) return
        setState({ kind: 'error', message: e instanceof Error ? e.message : 'This invite could not be found.' })
      })
    return () => {
      cancelled = true
    }
  }, [token])

  async function handleAccept() {
    if (!token) return
    setAccepting(true)
    try {
      const ws = await acceptInvite(token)
      navigate(`/w/${ws.slug}`, { replace: true })
    } catch (e) {
      if (e instanceof WorkspaceApiError && e.status === 403) {
        setState({ kind: 'mismatch' })
      } else {
        setState({
          kind: 'error',
          message: e instanceof Error ? e.message : 'Failed to accept the invite.',
        })
      }
    } finally {
      setAccepting(false)
    }
  }

  if (!token) return null

  if (state.kind === 'loading') {
    return (
      <Centered>
        <p className="text-sm text-muted-foreground">Loading invite…</p>
      </Centered>
    )
  }

  if (state.kind === 'error') {
    return (
      <Centered>
        <h1 className="mb-2 text-lg font-semibold text-foreground">Invite not available</h1>
        <p className="text-sm text-muted-foreground">{state.message}</p>
      </Centered>
    )
  }

  if (state.kind === 'mismatch') {
    const signedInAs = auth.status === 'authenticated' ? auth.user.email : 'a different account'
    const csrfToken = getCsrfToken()
    return (
      <Centered>
        <h1 className="mb-2 text-lg font-semibold text-foreground">Different address</h1>
        <p className="text-sm text-muted-foreground">
          This invite is bound to a different email address than the one you&apos;re signed in
          as ({signedInAs}). Sign out and sign back in with the invited address to accept it.
        </p>
        <form method="post" action="/accounts/logout/" className="mt-4 inline-block">
          <input type="hidden" name="csrfmiddlewaretoken" value={csrfToken} />
          <Button type="submit">Sign out</Button>
        </form>
      </Centered>
    )
  }

  const { preview } = state

  if (preview.status !== 'pending') {
    return (
      <Centered>
        <h1 className="mb-2 text-lg font-semibold text-foreground">Invite not available</h1>
        <p className="text-sm text-muted-foreground">
          {TERMINAL_MESSAGE[preview.status] ?? 'This invite is no longer valid.'}
        </p>
      </Centered>
    )
  }

  const currentPath = `/invite/${token}`

  if (auth.status !== 'authenticated') {
    return (
      <Centered>
        <h1 className="mb-2 text-lg font-semibold text-foreground">
          You&apos;ve been invited to {preview.workspace_display_name}
        </h1>
        <p className="mb-6 text-sm text-muted-foreground">
          as <span className="capitalize">{preview.role}</span>
        </p>
        <a
          href={loginHref(currentPath)}
          className="inline-block rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
        >
          Sign in with Google to accept
        </a>
      </Centered>
    )
  }

  return (
    <Centered>
      <h1 className="mb-2 text-lg font-semibold text-foreground">
        You&apos;ve been invited to {preview.workspace_display_name}
      </h1>
      <p className="mb-6 text-sm text-muted-foreground">
        as <span className="capitalize">{preview.role}</span>
      </p>
      <Button onClick={() => void handleAccept()} disabled={accepting}>
        {accepting ? 'Accepting…' : 'Accept invite'}
      </Button>
    </Centered>
  )
}
