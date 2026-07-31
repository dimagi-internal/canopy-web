// The provisioning commands, generated from a workspace's OWN values.
//
// Pure and exported so it is testable without a renderer (the pattern
// `isInvitePending` and `RunnerAssignments` already follow), and because the
// commands are the part most likely to be got wrong by hand: the audience must
// match the push URL exactly, and the URL carries a `/canopy` script prefix on
// labs that is easy to drop.

export type SetupInputs = {
  pushUrl: string
  project: string
  topic: string
  serviceAccount: string
}

export const DEFAULT_PROJECT = 'connect-labs'
export const DEFAULT_TOPIC = 'canopy-gmail-push'

/** `canopy-push@<project>.iam.gserviceaccount.com` — the conventional name. */
export function suggestedServiceAccount(project: string): string {
  return `canopy-push@${(project || DEFAULT_PROJECT).trim()}.iam.gserviceaccount.com`
}

/** `projects/<project>/topics/<topic>` — the value `users.watch` needs. */
export function topicPath(project: string, topic: string): string {
  return `projects/${(project || DEFAULT_PROJECT).trim()}/topics/${(topic || DEFAULT_TOPIC).trim()}`
}

/**
 * The gcloud commands to provision one workspace's push path.
 *
 * `gmail-api-push@system.gserviceaccount.com` is a FIXED Google-owned account,
 * not a placeholder — without the publisher grant, `users.watch` returns 403 and
 * the error does not say why.
 */
export function setupCommands({ pushUrl, project, topic, serviceAccount }: SetupInputs): string {
  const p = (project || DEFAULT_PROJECT).trim()
  const t = (topic || DEFAULT_TOPIC).trim()
  const sa = (serviceAccount || suggestedServiceAccount(p)).trim()
  const saName = sa.split('@')[0] || 'canopy-push'
  return [
    `gcloud config set project ${p}`,
    ``,
    `# 1. The topic`,
    `gcloud pubsub topics create ${t}`,
    ``,
    `# 2. Let Gmail publish to it (fixed Google-owned account)`,
    `gcloud pubsub topics add-iam-policy-binding ${t} \\`,
    `  --member=serviceAccount:gmail-api-push@system.gserviceaccount.com \\`,
    `  --role=roles/pubsub.publisher`,
    ``,
    `# 3. The identity Google calls us as (no key needed)`,
    `gcloud iam service-accounts create ${saName} \\`,
    `  --display-name="canopy-web inbound push"`,
    ``,
    `# 4. The push subscription — audience MUST equal the endpoint below`,
    `gcloud pubsub subscriptions create ${t}-sub \\`,
    `  --topic=${t} \\`,
    `  --push-endpoint="${pushUrl}" \\`,
    `  --push-auth-service-account="${sa}" \\`,
    `  --push-auth-token-audience="${pushUrl}" \\`,
    `  --ack-deadline=30`,
  ].join('\n')
}

/** Console links, so nobody hunts for the right project's page. */
export function consoleLinks(project: string): { label: string; url: string }[] {
  const p = encodeURIComponent((project || DEFAULT_PROJECT).trim())
  return [
    { label: 'Enable the Pub/Sub API', url: `https://console.cloud.google.com/apis/library/pubsub.googleapis.com?project=${p}` },
    { label: 'Topics', url: `https://console.cloud.google.com/cloudpubsub/topic/list?project=${p}` },
    { label: 'Subscriptions', url: `https://console.cloud.google.com/cloudpubsub/subscription/list?project=${p}` },
    { label: 'Service accounts', url: `https://console.cloud.google.com/iam-admin/serviceaccounts?project=${p}` },
  ]
}

/** Human copy for a mailbox's watch state. `none` is not an error — it means
 *  nothing has armed it yet, which is the expected state before provisioning. */
export function watchLabel(state: string, expiresAt: string): string {
  switch (state) {
    case 'armed':
      return `Armed until ${new Date(expiresAt).toLocaleString()}`
    case 'expiring':
      return `Expires ${new Date(expiresAt).toLocaleString()} — re-arm is due`
    case 'expired':
      return `EXPIRED ${new Date(expiresAt).toLocaleString()} — push is not being delivered`
    default:
      return 'Not armed yet'
  }
}

export function watchTone(state: string): 'success' | 'warning' | 'destructive' | 'muted' {
  if (state === 'armed') return 'success'
  if (state === 'expiring') return 'warning'
  if (state === 'expired') return 'destructive'
  return 'muted'
}
