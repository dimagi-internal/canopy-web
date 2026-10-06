import type { Register } from 'claude-code'

// The in-app half of the canopy desktop runner (runner/claude_desktop/README.md).
//
// The runner daemon outside the app cannot type into a desktop session, and the
// app gives it no API to. This mod is the hand inside: it runs in every desktop
// session, and in one the runner prepared it talks to the daemon through files
// in `<cwd>/.canopy-desktop/`, a directory only the runner creates. Anywhere
// else it finds no directory and does nothing.
//
//   runner -> mod   task.txt          the turn's prompt, submitted once
//                   fu-<n>.txt        a follow-up, submitted once (fu-<n>.done marks it)
//   mod -> runner   alive             a timestamp, rewritten every poll: "this session's process is up"
//                   ev-<t>-<n>-<kind>.json   session.start / submitted / turn.start /
//                                     turn.complete / ask / submit.error
//
// Files, not HTTP: the mod has no credential and should not need one, and a
// file left by a session that died is still there for the runner to read.

let dir = ''
let sid = ''
let seq = 0
let started = false

async function emit($: any, kind: string, extra: unknown) {
  if (!dir) return
  seq += 1
  await $.fs.write(`${dir}/ev-${Date.now()}-${seq}-${kind}.json`,
    JSON.stringify({ t: Date.now(), sid, kind, extra }))
}

async function submitOnce($: any, file: string, marker: string, which: string) {
  if (!(await $.fs.exists(file)) || (await $.fs.exists(marker))) return
  // Mark first: a reload mid-submit must never send the same prompt twice.
  await $.fs.write(marker, String(Date.now()))
  try {
    // asUser: the model reads the prompt bare, as the person's own words,
    // instead of "The canopy-desktop plugin sent a message: …".
    await $.prompt.submit({ text: await $.fs.read(file), asUser: true })
    await emit($, 'submitted', { which })
  } catch (err) {
    await emit($, 'submit.error', { which, err: String(err) })
  }
}

async function poll($: any) {
  if (!dir || !(await $.fs.exists(dir))) return
  await $.fs.write(`${dir}/alive`, String(Date.now()))
  await submitOnce($, `${dir}/task.txt`, `${dir}/task.claimed`, 'task')
  for (let n = 1; n <= 200; n++) {
    const fu = `${dir}/fu-${n}.txt`
    if (!(await $.fs.exists(fu))) break
    await submitOnce($, fu, `${dir}/fu-${n}.done`, `fu-${n}`)
  }
}

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const r = await next(e)
    const candidate = `${e.cwd}/.canopy-desktop`
    if (!(await $.fs.exists(candidate))) return r
    // A headless `claude -p` seed also loads this mod; it must not take the task.
    // The runner writes `seeded` only after the seed has exited.
    if (!(await $.fs.exists(`${candidate}/seeded`))) return r
    dir = candidate
    sid = await $.session.id()
    await emit($, 'session.start', { surface: e.surface, isInteractive: e.isInteractive })
    if (!started) {
      started = true
      $.clock.every(2000, () => poll($))
    }
    await poll($)
    return r
  })

  on('turn.start', async ($, e, next) => {
    await emit($, 'turn.start', {})
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    await emit($, 'turn.complete', {
      turnId: e.turnId, isAborted: e.isAborted, reason: e.reason,
      answer: String(e.answer ?? '').slice(0, 4000),
    })
    return r
  })

  // A permission ask is a session waiting on a human. The app shows its own card;
  // this tells the runner, so canopy-web can show "needs input" too.
  on('tool.check', async ($, e, next) => {
    const r = await next(e)
    // Report only; never let a failed write change the decision.
    if (r.decision === 'ask') await emit($, 'ask', { tool: e.tool, reason: r.reason ?? '' }).catch(() => {})
    return r
  })
}
