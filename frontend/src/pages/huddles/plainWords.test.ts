import { describe, expect, it } from 'vitest'
import {
  ANSWER_WORDS, answerWords, boardLinkText, confidenceWords, deJargon, effortWords, ENGINE_TERMS, firstSentence,
  gistOf, heldReasonWords, holdSentence, huddleExplainer, nextStep, peopleWords, resolutionWords, sizePlain, stepHeading,
  stepName, taskStatusPlain, trimPriority, VERDICT_WORDS,
} from './plainWords'

const clean = (s: string) => ENGINE_TERMS.every((re) => !re.test(s))

describe('every engine term has a plain word', () => {
  it('steps, not rounds', () => {
    expect([1, 2, 3, 4].map((r) => stepName('work', r))).toEqual([
      "What everyone's working on", 'Ideas', "Agreement", 'Settling changes',
    ])
    expect(stepHeading('work', 2)).toBe('Step 2 · Ideas')
    expect(stepHeading('health', 2)).toBe('Step 2')
  })

  it('answers', () => {
    expect(answerWords('co-sign')).toBe("I'm in")
    expect(answerWords('amend')).toBe('In, with changes')
    expect(answerWords('decline')).toBe('Not in')
    expect(answerWords('pending')).toBe('No answer yet')
    expect(answerWords('amend-accepted')).toBe('Changes agreed')
    expect(answerWords('amend-rejected')).toBe('Changes not agreed')
    expect(resolutionWords('accept')).toBe('Changes agreed')
    expect(resolutionWords('reject')).toBe('Changes not agreed')
    expect(Object.values(ANSWER_WORDS).every(clean)).toBe(true)
  })

  it('outcomes and task status', () => {
    expect(VERDICT_WORDS.filed).toBe('Sent to you to decide')
    expect(VERDICT_WORDS.held).toBe('Parked')
    expect(taskStatusPlain('suggested')).toBe('waiting for your yes/no')
    expect(taskStatusPlain('in_progress')).toBe('in progress')
    expect(taskStatusPlain('done')).toBe('done')
    expect(taskStatusPlain('declined')).toBe('you said no')
    expect(boardLinkText('eva')).toBe("on Eva's board")
  })

  it('size, confidence and who', () => {
    expect(effortWords('S')).toBe('small job')
    expect(effortWords('m')).toBe('medium job')
    expect(effortWords('L')).toBe('large job')
    expect(confidenceWords(0.65)).toBe("65% sure it's worth it")
    expect(sizePlain('S', 0.6)).toBe("small job · 60% sure it's worth it")
    expect(peopleWords('ace', ['eva'])).toBe('led by Ace, together with Eva')
    expect(peopleWords('hal', [])).toBe('Hal, on its own')
    expect(huddleExplainer('ada')).toBe('Ada checked in with the agents to work out what they could push forward for you, alone or together.')
  })

  it('translates engine words in free text, keeping case', () => {
    expect(deJargon('I co-sign with two changes. Co-sign only the narrowed form.')).toBe('I sign on with two changes. Sign on only the narrowed form.')
    expect(deJargon('Amendment: I am not co-signing; it was filed, then held (attempt 2, roundtable).'))
      .toBe('Change: I am not signing on; it was logged, then parked (try 2, idea round).')
    expect(clean(deJargon('cosigned amends amended attempts Held FILED'))).toBe(true)
  })
})

describe("why an idea was parked", () => {
  const ctx = { lead: 'eva', leader: 'ada' }
  it("rewrites the engine's held reasons as sentences", () => {
    expect(heldReasonWords('amend unresolved (echo)', ctx))
      .toBe("Echo said yes with changes, and Eva (who suggested it) hasn't confirmed them yet.")
    expect(heldReasonWords('amend unresolved (eva, ace)', { lead: 'echo' }))
      .toBe("Eva and Ace said yes with changes, and Echo (who suggested it) hasn't confirmed them yet.")
    expect(heldReasonWords('hal has not co-signed', ctx)).toBe("Hal hasn't said whether they're in.")
    expect(heldReasonWords('echo declined', ctx)).toBe("Echo said they're not in.")
    expect(heldReasonWords('amend rejected by lead', { ...ctx, who: ['hal'] })).toBe("Eva didn't agree to Hal's changes.")
    expect(heldReasonWords('amend rejected by lead', ctx)).toBe("Eva didn't agree to the changes asked for.")
  })

  it('passes other reasons through, de-jargoned', () => {
    const s = heldReasonWords('held at filing: no stated priority', ctx)
    expect(s).toBe('Parked at filing: no stated priority.')
  })

  it('says what would un-park it', () => {
    expect(holdSentence({ kind: 'amend', who: ['echo'] }, ctx).clear).toBe("Eva agreeing to Echo's changes would send it to you.")
    expect(holdSentence({ kind: 'declined', who: ['echo'] }, ctx).clear).toMatch(/later huddle/)
    expect(holdSentence({ kind: 'pending', who: ['hal'] }, { ...ctx, open: true }).why).toBe("Waiting for Hal to say whether they're in.")
    // A lead someone else named "leads it" rather than "suggested it".
    expect(holdSentence({ kind: 'amend', who: ['eva'] }, { lead: 'hal', proposedBy: 'eva' }).why)
      .toBe("Eva said yes with changes, and Hal (who leads it) hasn't confirmed them yet.")
    for (const kind of ['declined', 'amend-rejected', 'amend', 'pending', 'gate'] as const) {
      const { why, clear } = holdSentence({ kind, who: ['hal'] }, ctx)
      expect(clean(why) && clean(clear)).toBe(true)
    }
  })
})

describe("a task's next step", () => {
  const agents = ['ace', 'echo', 'eva', 'hal']
  it('reads BLOCKED / Waiting on / [MANUAL as stuck, lightly de-jargoned', () => {
    const t = nextStep({ status: 'in_progress', next_action: "BLOCKED on creds: ~/.chrome-sales/.sf-creds.json + .gws-sa-key.json must be provisioned on cloud-ec2-2 (Hal's 1Password SA can't see them). Deps already fixed." }, agents)
    expect(t).toMatchObject({ stuck: true, onYou: false })
    expect(t.text).toBe('The Salesforce credentials and the Google Drive key must be installed on the cloud runner. Dependencies already fixed.')
    expect(nextStep({ status: 'in_progress', next_action: 'waiting on Beth to fix the amount' }).stuck).toBe(true)
    expect(nextStep({ status: 'in_progress', next_action: '[MANUAL] Jonathan runs the deploy' })).toMatchObject({ stuck: true, onYou: true, text: 'You runs the deploy' })
  })

  it('knows a task stuck on the reader, and one waiting on a teammate', () => {
    const t = nextStep({ status: 'in_progress', next_action: 'Alarms NOT verifiable from cloud runner (no CloudWatch). Needs a laptop runner / Jonathan: read-only check.' }, agents)
    expect(t).toMatchObject({ stuck: true, onYou: true })
    expect(t.text).toBe("Alarms can't be checked from the cloud runner. Needs a laptop runner or you: read-only check.")
    const w = nextStep({ status: 'in_progress', next_action: 'Run the T21 funnel + one real SF query on your first turn after Hal reports a fix' }, agents)
    expect(w).toMatchObject({ stuck: true, waitingOn: 'hal' })
    expect(w.text).toMatch(/^Waiting on Hal first — /)
  })

  it('a moving task is not stuck; a declined or done one has no next step', () => {
    expect(nextStep({ status: 'in_progress', next_action: 'Draft the slide by 10/8' })).toMatchObject({ stuck: false, text: 'Draft the slide by 10/8' })
    expect(nextStep({ status: 'declined', next_action: 'BLOCKED on x' }).text).toBe('')
    expect(nextStep({ status: 'in_progress', next_action: '' }).text).toBe('')
  })
})

describe('gists', () => {
  it('trims a priority to a phrase', () => {
    expect(trimPriority('Connect-labs reliability under load (overload exhausting DB slots, 5xx with no alarm): from Jonathan flagging the #1060 residual (T15)'))
      .toBe('Connect-labs reliability under load')
    expect(trimPriority('Ship one REAL (non-synthetic) story (board T4; suggested T2) — from the Echo board.')).toBe('Ship one REAL story')
  })

  it('shortens a report line, dropping ref-only lead-ins', () => {
    expect(gistOf("T44 / P-9: podcast-promo step replaced (eva PR #364, merged); calendar note (#365)")).toBe('podcast-promo step replaced')
    expect(gistOf('P-13 overclaim check (§D) added to agent-turn-review, hal PR #235 merged (board T47)')).toBe('overclaim check added to agent-turn-review')
  })

  it('takes the first sentence, or two when the first is short', () => {
    expect(firstSentence('I co-sign with two changes. (1) Source rule: public only. (2) Due 10/8.')).toBe('I sign on with two changes. Source rule: public only.')
    expect(firstSentence('Confidence 0.45, M effort, no confirmed partner access. As written I would park it.')).toBe('Confidence 0.45, M effort, no confirmed partner access.')
    expect(firstSentence('A long enough opening sentence that says the whole thing in one go, really. And more.')).toBe('A long enough opening sentence that says the whole thing in one go, really.')
  })
})
