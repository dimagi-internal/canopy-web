// Facts the pages share, kept in one place so a link or a date changes once.

export const GITHUB_URL = 'https://github.com/dimagi-internal/canopy-web'
export const LICENSE_URL = `${GITHUB_URL}/blob/main/LICENSE`
// The door into the app: signed in, straight to your workbench; signed out, the
// login middleware sends you through Google sign-in first, then here.
export const SIGN_IN_URL = '/app'
// The request-access form posts here. Empty = this host (canopy-web serves the site
// today); set PUBLIC_CANOPY_API at build time once the site lives elsewhere.
export const API_BASE: string = import.meta.env.PUBLIC_CANOPY_API ?? ''
