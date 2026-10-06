import type { Register } from 'claude-code'

// The in-app half of the canopy runner's Claude desktop runtime
// (runner/canopy_runner/canopy_runner/desktop.py).
//
// The runner daemon outside the app cannot type into a desktop session, and the
// app gives it no API to. This mod is the hand inside: it runs in every desktop
// session, and in one the runner prepared it talks to the daemon through files
// in `<cwd>/.canopy-desktop/`, a directory only the runner creates. Anywhere
// else it finds no directory and does nothing.
//
//   runner -> mod   task.txt          the turn's prompt, submitted once
//                   fu-<n>.txt        a follow-up, submitted once (fu-<n>.done marks it)
//                   stop-<n>.txt      stop the running turn (stop-<n>.done marks it)
//   mod -> runner   alive             a timestamp, rewritten every poll: "this session's process is up"
//                   ev-<t>-<n>-<kind>.json   session.start / submitted / turn.start /
//                                     turn.complete / ask / submit.error / stopped
//
// Files, not HTTP: the mod has no credential and should not need one, and a
// file left by a session that died is still there for the runner to read.

let dir = ''
let sid = ''
let seq = 0
let started = false
// The model turn running now, from turn.start, so a stop can name it.
let running = ''

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
    const text = await $.fs.read(file)
    const slash = /^\/([^\s]+)(?:[ \t]+|\n|$)([\s\S]*)$/.exec(text)
    if (slash) {
      // `/hal:turn …` is a command, not a prompt: prompt.submit refuses a leading
      // slash and names this call. Runs as if the person typed it.
      await $.command.run({ command: slash[1], args: slash[2] ?? '' })
    } else {
      // asUser: the model reads the prompt bare, as the person's own words,
      // instead of "The canopy-desktop plugin sent a message: …".
      await $.prompt.submit({ text, asUser: true })
    }
    await emit($, 'submitted', { which })
  } catch (err) {
    await emit($, 'submit.error', { which, err: String(err) })
  }
}

async function stopOnce($: any, file: string, marker: string, which: string) {
  if (!(await $.fs.exists(file)) || (await $.fs.exists(marker))) return
  await $.fs.write(marker, String(Date.now()))
  if (!running) {
    await emit($, 'stopped', { which, outcome: 'idle' })
    return
  }
  try {
    await $.turn.abort({ turnId: running })
    await emit($, 'stopped', { which, outcome: 'interrupted', turnId: running })
  } catch (err) {
    await emit($, 'stopped', { which, outcome: 'failed', err: String(err) })
  }
}

// The channel belongs to the session the runner seeded for it, named in `seeded`.
// Checked on every poll, not once: a directory can be recreated under a session
// that is still running, and that session must not take the new one's prompt.
async function mine($: any): Promise<boolean> {
  if (!dir || !sid || !(await $.fs.exists(`${dir}/seeded`))) return false
  return (await $.fs.read(`${dir}/seeded`)).trim() === sid
}

async function poll($: any) {
  if (!(await mine($))) return
  await $.fs.write(`${dir}/alive`, String(Date.now()))
  for (let n = 1; n <= 200; n++) {
    const stop = `${dir}/stop-${n}.txt`
    if (!(await $.fs.exists(stop))) break
    await stopOnce($, stop, `${dir}/stop-${n}.done`, `stop-${n}`)
  }
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
    if (!(await mine($))) { dir = ''; return r }
    await emit($, 'session.start', { surface: e.surface, isInteractive: e.isInteractive })
    // Submitting is left to the clock, never done in this hook: a command run
    // from inside a hook the session is waiting on is refused.
    if (!started) {
      started = true
      $.clock.every(2000, () => poll($))
    }
    return r
  })

  on('turn.start', async ($, e, next) => {
    running = e.turnId
    await emit($, 'turn.start', { turnId: e.turnId })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    if (running === e.turnId) running = ''
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
