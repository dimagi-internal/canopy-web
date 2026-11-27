/**
 * AWS STS from the browser: `AssumeRole`, signed here (Signature Version 4, WebCrypto) with a key
 * this browser just opened from the vault. The key and the session it makes never pass through
 * canopy-web; STS answers the browser directly (it allows any origin).
 *
 * The CLI signs the same request the same way (canopy `orchestrator/aws_sealed.py`); both are
 * checked against botocore's own signature for a fixed request (`sts.test.ts`).
 */

export interface AwsKey {
  AccessKeyId: string
  SecretAccessKey: string
  SessionToken?: string
}

export interface AwsSession {
  Version: 1
  AccessKeyId: string
  SecretAccessKey: string
  SessionToken: string
  Expiration: string
}

/** What canopy-web says to ask (`aws_sealed.sts_request`): the session stamped with the agent. */
export interface StsRequest {
  role_arn: string
  role_session_name: string
  source_identity: string
  tags: { Key: string; Value: string }[]
  duration_seconds: number
  region: string
}

export const STS_HOST = 'sts.amazonaws.com'
const CONTENT_TYPE = 'application/x-www-form-urlencoded; charset=utf-8'
const enc = new TextEncoder()

const hex = (b: ArrayBuffer) => [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, '0')).join('')

async function sha256Hex(text: string): Promise<string> {
  return hex(await crypto.subtle.digest('SHA-256', enc.encode(text)))
}

async function hmac(key: ArrayBuffer | Uint8Array, text: string): Promise<ArrayBuffer> {
  const k = await crypto.subtle.importKey('raw', key as BufferSource, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  return crypto.subtle.sign('HMAC', k, enc.encode(text))
}

/** RFC 3986 form encoding, the one AWS's query protocol documents. */
export function formEncode(params: [string, string][]): string {
  const e = (s: string) => encodeURIComponent(s).replace(/[!'()*]/g, (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`)
  return params.map(([k, v]) => `${e(k)}=${e(v)}`).join('&')
}

export function assumeRoleBody(req: StsRequest, seconds: number): string {
  const params: [string, string][] = [
    ['Action', 'AssumeRole'], ['Version', '2011-06-15'], ['RoleArn', req.role_arn],
    ['RoleSessionName', req.role_session_name], ['DurationSeconds', String(seconds)],
    ['SourceIdentity', req.source_identity],
  ]
  req.tags.forEach((t, i) => {
    params.push([`Tags.member.${i + 1}.Key`, t.Key], [`Tags.member.${i + 1}.Value`, t.Value])
  })
  return formEncode(params)
}

/** `YYYYMMDDTHHMMSSZ` for `when`. */
export function amzDate(when: Date): string {
  return when.toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '')
}

/** The headers for a POST of `body` to STS, signed with `key` (SigV4, service `sts`). */
export async function signedHeaders(key: AwsKey, body: string, when: Date, region = 'us-east-1',
                                    host = STS_HOST): Promise<Record<string, string>> {
  const date = amzDate(when)
  const day = date.slice(0, 8)
  const headers: Record<string, string> = { 'content-type': CONTENT_TYPE, host, 'x-amz-date': date }
  if (key.SessionToken) headers['x-amz-security-token'] = key.SessionToken
  const names = Object.keys(headers).sort()
  const canonical = ['POST', '/', '', ...names.map((n) => `${n}:${headers[n]}`), '', names.join(';'),
    await sha256Hex(body)].join('\n')
  const scope = `${day}/${region}/sts/aws4_request`
  const toSign = ['AWS4-HMAC-SHA256', date, scope, await sha256Hex(canonical)].join('\n')
  let k = await hmac(enc.encode(`AWS4${key.SecretAccessKey}`), day)
  for (const part of [region, 'sts', 'aws4_request']) k = await hmac(k, part)
  const signature = hex(await hmac(k, toSign))
  const out: Record<string, string> = { ...headers,
    authorization: `AWS4-HMAC-SHA256 Credential=${key.AccessKeyId}/${scope}, SignedHeaders=${names.join(';')}, Signature=${signature}` }
  delete out.host  // the browser sets it, and may not be told to
  return out
}

export class StsError extends Error {
  code: string
  constructor(code: string, message: string) {
    super(`AWS refused: ${code}${message ? ` — ${message}` : ''}`)
    this.code = code
  }
}

function text(doc: Document, tag: string): string {
  return doc.getElementsByTagName(tag)[0]?.textContent?.trim() ?? ''
}

/** Parse STS's XML answer: the session, or StsError. */
export function parseAssumeRole(xml: string): AwsSession {
  const doc = new DOMParser().parseFromString(xml, 'application/xml')
  const code = text(doc, 'Code')
  if (code) throw new StsError(code, text(doc, 'Message'))
  const s = { Version: 1 as const, AccessKeyId: text(doc, 'AccessKeyId'), SecretAccessKey: text(doc, 'SecretAccessKey'),
              SessionToken: text(doc, 'SessionToken'), Expiration: text(doc, 'Expiration') }
  if (!s.AccessKeyId || !s.SecretAccessKey || !s.SessionToken || !s.Expiration) {
    throw new StsError('BadResponse', 'STS answered without a whole session')
  }
  return s
}

/**
 * `AssumeRole` as the agent, with `key`. Asks for `req.duration_seconds`; when the role allows
 * less (its MaxSessionDuration), asks again for an hour, the longest every role allows.
 */
export async function assumeRole(key: AwsKey, req: StsRequest, fetcher: typeof fetch = fetch.bind(globalThis),
                                 now: () => Date = () => new Date()): Promise<AwsSession> {
  const ask = async (seconds: number) => {
    const body = assumeRoleBody(req, seconds)
    const res = await fetcher(`https://${STS_HOST}/`, {
      method: 'POST', headers: await signedHeaders(key, body, now(), req.region), body, credentials: 'omit',
    })
    return parseAssumeRole(await res.text())
  }
  try {
    return await ask(req.duration_seconds)
  } catch (e) {
    if (e instanceof StsError && e.code === 'ValidationError' && /DurationSeconds/i.test(e.message)
        && req.duration_seconds > 3600) {
      return ask(3600)
    }
    throw e
  }
}
