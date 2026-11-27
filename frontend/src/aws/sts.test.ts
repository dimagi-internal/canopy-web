// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { amzDate, assumeRole, assumeRoleBody, parseAssumeRole, signedHeaders, StsError, type StsRequest } from './sts'

// botocore 1.x `SigV4Auth(creds, "sts", "us-east-1").add_auth(...)` on this exact request, at
// 2026-11-27T15:48:32Z, with AWS's documentation key: the signatures below are botocore's.
const KEY = { AccessKeyId: 'AKIAIOSFODNN7EXAMPLE', SecretAccessKey: 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY' }
const WHEN = new Date('2026-11-27T15:48:32Z')
const REQ: StsRequest = {
  role_arn: 'arn:aws:iam::858923557655:role/labs-jj-agent-readwrite', role_session_name: 'canopy-ace',
  source_identity: 'canopy-agent-ace', duration_seconds: 3600, region: 'us-east-1',
  tags: [{ Key: 'agent', Value: 'ace' }, { Key: 'caller', Value: 'user-7' }, { Key: 'delivery', Value: 'sealed' }],
}
const BODY = 'Action=AssumeRole&Version=2011-06-15&RoleArn=arn%3Aaws%3Aiam%3A%3A858923557655%3Arole%2Flabs-jj-agent-readwrite'
  + '&RoleSessionName=canopy-ace&DurationSeconds=3600&SourceIdentity=canopy-agent-ace&Tags.member.1.Key=agent'
  + '&Tags.member.1.Value=ace&Tags.member.2.Key=caller&Tags.member.2.Value=user-7&Tags.member.3.Key=delivery'
  + '&Tags.member.3.Value=sealed'

const OK = `<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult>
<Credentials><AccessKeyId>ASIAEXAMPLE</AccessKeyId><SecretAccessKey>s3cr3t</SecretAccessKey>
<SessionToken>tok</SessionToken><Expiration>2026-11-27T16:48:32Z</Expiration></Credentials>
</AssumeRoleResult></AssumeRoleResponse>`
const fail = (code: string, message: string) =>
  `<ErrorResponse><Error><Type>Sender</Type><Code>${code}</Code><Message>${message}</Message></Error></ErrorResponse>`

describe('sts', () => {
  it('encodes the AssumeRole body as botocore does', () => {
    expect(assumeRoleBody(REQ, 3600)).toBe(BODY)
    expect(amzDate(WHEN)).toBe('20261127T154832Z')
  })

  it('signs exactly as botocore does', async () => {
    const h = await signedHeaders(KEY, BODY, WHEN)
    expect(h.authorization).toBe('AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20261127/us-east-1/sts/aws4_request, '
      + 'SignedHeaders=content-type;host;x-amz-date, '
      + 'Signature=b985c3aa5e11f886b88661a9d5e2c71c01981df3f658fcffa960bfe65229c055')
    expect(h['x-amz-date']).toBe('20261127T154832Z')
    expect(h.host).toBeUndefined()
  })

  it('signs a session token in as botocore does', async () => {
    const h = await signedHeaders({ ...KEY, SessionToken: 'FwoGZXIvYXdzEXAMPLETOKEN' }, BODY, WHEN)
    expect(h.authorization).toContain('SignedHeaders=content-type;host;x-amz-date;x-amz-security-token, '
      + 'Signature=4514a3a9b6d616027ad6a4b96e4e498aedede3833a1505e77ab97db1310c0dc5')
  })

  it('reads the session, or the refusal', () => {
    expect(parseAssumeRole(OK)).toEqual({ Version: 1, AccessKeyId: 'ASIAEXAMPLE', SecretAccessKey: 's3cr3t',
                                          SessionToken: 'tok', Expiration: '2026-11-27T16:48:32Z' })
    expect(() => parseAssumeRole(fail('AccessDenied', 'not authorized'))).toThrow(StsError)
  })

  it('posts to STS directly, without cookies, and asks for an hour when the role allows less', async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(new Response(fail('ValidationError', 'The requested DurationSeconds exceeds the MaxSessionDuration')))
      .mockResolvedValueOnce(new Response(OK))
    const s = await assumeRole(KEY, { ...REQ, duration_seconds: 7200 }, fetcher as unknown as typeof fetch, () => WHEN)
    expect(s.AccessKeyId).toBe('ASIAEXAMPLE')
    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('https://sts.amazonaws.com/')
    expect(init.credentials).toBe('omit')
    expect(init.body).toContain('DurationSeconds=7200')
    expect(fetcher.mock.calls[1][1].body).toContain('DurationSeconds=3600')
  })
})
