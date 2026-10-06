// Facts the pages share, kept in one place so a link changes once.

export const GITHUB_URL = 'https://github.com/dimagi-internal/canopy-web'
export const LICENSE_URL = `${GITHUB_URL}/blob/main/LICENSE`
// The door into the app: signed in, straight to your workbench; signed out, the
// login middleware sends you through Google sign-in first, then here.
export const SIGN_IN_URL = '/app'
// The request-access form posts here. Empty = this host (canopy-web serves the site
// today); set PUBLIC_CANOPY_API at build time once the site lives elsewhere.
export const API_BASE: string = import.meta.env.PUBLIC_CANOPY_API ?? ''

// The Dimagi legal pages every product site's footer links to.
export const PRIVACY_POLICY_URL = 'https://dimagi.com/terms-privacy/'
export const TERMS_URL = 'https://dimagi.com/terms-of-service/'

export type NavKey = 'home' | 'how-it-works'
export const NAV_LINKS: { key: NavKey; label: string; href: string }[] = [
  { key: 'home', label: 'Canopy', href: '/' },
  { key: 'how-it-works', label: 'How it works', href: '/how-it-works' },
]
