/**
 * `/ext/signed-in/` — where the sign-in tab the browser extension's panel opened lands
 * (`ExtPanelPage`, `signInUrl`). The session now exists in this browser; the panel finds it
 * the next time it has focus, so this page only says to go back.
 */
import { CanopyWordmark } from '@/brand/CanopyWordmark'

export function ExtSignedInPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6 text-foreground">
      <div className="w-full max-w-md rounded-xl border border-border bg-card p-8 text-center">
        <h1 className="mb-2 text-2xl font-semibold"><CanopyWordmark /></h1>
        <p className="text-sm text-muted-foreground">
          You&apos;re signed in. Return to the Canopy side panel — it picks up your sign-in when you click back into it.
          You can close this tab.
        </p>
      </div>
    </div>
  )
}
