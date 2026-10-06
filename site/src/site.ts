// Facts the pages share, kept in one place so a link or a date changes once.

export const GITHUB_URL = 'https://github.com/dimagi-internal/canopy-web'
export const LICENSE_URL = `${GITHUB_URL}/blob/main/LICENSE`
// Signing in lands a member on their workbench. Same door the app's own sign-in uses.
export const SIGN_IN_URL = '/accounts/google/login/?next=%2F'
// The closed-beta form posts here. Empty = this host (canopy-web serves the site
// today); set PUBLIC_CANOPY_API at build time once the site lives elsewhere.
export const API_BASE: string = import.meta.env.PUBLIC_CANOPY_API ?? ''
