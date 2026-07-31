import { describe, expect, it } from 'vitest'
import {
  DEFAULT_PROJECT,
  consoleLinks,
  setupCommands,
  suggestedServiceAccount,
  topicPath,
  watchLabel,
  watchTone,
} from './inboundSetup'

const PUSH_URL = 'https://labs.connect.dimagi.com/canopy/api/inbound/gmail/dimagi/'

describe('setupCommands', () => {
  const cmds = setupCommands({
    pushUrl: PUSH_URL,
    project: 'connect-labs',
    topic: 'canopy-gmail-push',
    serviceAccount: 'canopy-push@connect-labs.iam.gserviceaccount.com',
  })

  it('grants publisher to the fixed Gmail system account', () => {
    // Not a placeholder: without this, users.watch 403s and says nothing useful.
    expect(cmds).toContain('gmail-api-push@system.gserviceaccount.com')
    expect(cmds).toContain('--role=roles/pubsub.publisher')
  })

  it('uses the push URL as the audience, exactly', () => {
    // The single most common way to get this wrong — and it fails silently.
    expect(cmds).toContain(`--push-auth-token-audience="${PUSH_URL}"`)
    expect(cmds).toContain(`--push-endpoint="${PUSH_URL}"`)
  })

  it('keeps the script prefix that labs serves under', () => {
    expect(cmds).toContain('/canopy/api/inbound/gmail/dimagi/')
  })

  it('derives the service-account create name from the full email', () => {
    expect(cmds).toContain('gcloud iam service-accounts create canopy-push')
  })

  it('falls back to a suggested service account when none is given', () => {
    const out = setupCommands({
      pushUrl: PUSH_URL, project: 'my-proj', topic: 't', serviceAccount: '',
    })
    expect(out).toContain('canopy-push@my-proj.iam.gserviceaccount.com')
  })

  it('lets Pub/Sub mint OIDC tokens as the push identity', () => {
    // Omitting this creates the subscription successfully and then delivers
    // nothing — indistinguishable from a wrong audience.
    expect(cmds).toContain('--role=roles/iam.serviceAccountTokenCreator')
    expect(cmds).toContain('@gcp-sa-pubsub.iam.gserviceaccount.com')
  })
})

describe('DEFAULT_PROJECT', () => {
  it('is a placeholder, never a real project', () => {
    // Gmail requires the topic to live in the OAuth client's own project, which
    // canopy-web cannot know. Defaulting to a real one (`connect-labs`) produced
    // a runnable block whose every users.watch then failed, leaving the mailbox
    // silently on the poll. Obviously-incomplete beats confidently-wrong.
    expect(DEFAULT_PROJECT).toMatch(/^<.*>$/)
  })
})

describe('topicPath', () => {
  it('builds what users.watch needs', () => {
    expect(topicPath('p', 't')).toBe('projects/p/topics/t')
  })

  it('falls back rather than emitting a broken path', () => {
    expect(topicPath('', '')).toBe(`projects/${DEFAULT_PROJECT}/topics/canopy-gmail-push`)
  })
})

describe('suggestedServiceAccount', () => {
  it('follows the conventional name', () => {
    expect(suggestedServiceAccount('abc')).toBe('canopy-push@abc.iam.gserviceaccount.com')
  })
})

describe('consoleLinks', () => {
  it('points every link at the chosen project', () => {
    for (const l of consoleLinks('my-proj')) expect(l.url).toContain('project=my-proj')
  })

  it('url-encodes the project', () => {
    expect(consoleLinks('a b').every((l) => l.url.includes('a%20b'))).toBe(true)
  })
})

describe('watch state copy', () => {
  it('treats "not armed yet" as neutral, not an error', () => {
    // Expected before provisioning — red here would cry wolf on every new tenant.
    expect(watchTone('none')).toBe('muted')
    expect(watchLabel('none', '')).toBe('Not armed yet')
  })

  it('escalates expiring to warning and expired to destructive', () => {
    expect(watchTone('expiring')).toBe('warning')
    expect(watchTone('expired')).toBe('destructive')
  })

  it('says plainly that expired means push is not being delivered', () => {
    expect(watchLabel('expired', '2026-07-30T00:00:00Z')).toContain('not being delivered')
  })

  it('marks a healthy watch as success', () => {
    expect(watchTone('armed')).toBe('success')
  })
})
