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
//                   start-<n>.json    start ANOTHER session with the app's start_session
//                                     tool (canopy-web#1238); the result goes to start-<n>.done
//                   seeded = "start"  this folder waits for a session start_session is
//                                     opening: the first one to start here claims it
//   mod -> runner   alive             a timestamp, rewritten every poll: "this session's process is up"
//                   caps              what this mod can do for the runner (`start`)
//                   ev-<t>-<n>-<kind>.json   session.start / submitted / turn.start /
//                                     turn.complete / ask / answered / submit.error / stopped
//
// Files, not HTTP: the mod has no credential and should not need one, and a
// file left by a session that died is still there for the runner to read.

let dir = ''
let sid = ''
let seq = 0
let started = false
// The model turn running now, from turn.start, so a stop can name it.
let running = ''
// Claimed by start_session: the session's first turn is the start's own brief,
// and the task waits until it completes.
let fresh = false
// Permission cards drawn and not yet answered, by tool. PermissionRequest names
// no call id, so a call settling clears a card for its own tool only.
const asking = new Map<string, number>()

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

// The runner cannot start a session in the app without a claude:// link, which
// brings the app to the front. A live session can: the app's own start_session
// tool opens a sibling in the background. `$.mcp.call` is the mod's own call, so
// it needs no rule or classifier: agents' turns gain no new power, only the
// runner's request through this channel does.
async function startOnce($: any, file: string, marker: string) {
  if (!(await $.fs.exists(file)) || (await $.fs.exists(marker))) return
  await $.fs.write(marker, JSON.stringify({ pending: true }))
  try {
    const req = JSON.parse(await $.fs.read(file))
    const r = await $.mcp.call('ccd_session', 'start_session', {
      initiation: 'user_asked', context: 'fresh', use_worktree: false,
      cwd: req.cwd, title: req.title, prompt: req.prompt, background: req.background,
    })
    const text = (r.content ?? []).map((b: any) => b.text ?? '').join('\n')
    await $.fs.write(marker, JSON.stringify({ isError: !!r.isError, text }))
  } catch (err) {
    await $.fs.write(marker, JSON.stringify({ isError: true, text: String(err) }))
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
  for (let n = 1; n <= 200; n++) {
    const start = `${dir}/start-${n}.json`
    if (!(await $.fs.exists(start))) break
    await startOnce($, start, `${dir}/start-${n}.done`)
  }
  if (!fresh) await submitOnce($, `${dir}/task.txt`, `${dir}/task.claimed`, 'task')
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
    // The `claude-cli` runtime's own headless turns run here too (headless.py). The
    // channel is for the app's process only: claimed here, `alive` would read as
    // "the app holds this session" and the runner would hand its turn to itself.
    if (await $.env.get('CANOPY_HEADLESS')) return r
    dir = candidate
    sid = await $.session.id()
    // A folder the runner handed to start_session: the session it opened is the
    // only one that starts here, so the first to arrive is it.
    if ((await $.fs.read(`${dir}/seeded`)).trim() === 'start') {
      await $.fs.write(`${dir}/seeded`, sid)
      fresh = true
    }
    if (!(await mine($))) { dir = ''; return r }
    await $.fs.write(`${dir}/caps`, 'start')
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
    fresh = false
    await emit($, 'turn.complete', {
      turnId: e.turnId, isAborted: e.isAborted, reason: e.reason,
      answer: String(e.answer ?? '').slice(0, 4000),
    })
    return r
  })

  // Says, in the app, that this session is canopy's: a person who opened it from
  // canopy-web (or found it in the sidebar) can see that messages sent from
  // canopy-web land here. Drawn only in a session whose channel this mod claimed.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (!dir) return next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box>
        <Text bold>Canopy</Text>
        <Text dimColor> · connected to canopy-web. Messages sent there arrive in this session.</Text>
      </Box>
    )
  })

  // A permission card is a session waiting on a human. The app shows its own;
  // this tells the runner, so canopy-web can show "needs input" too.
  //
  // Not from `tool.check`: its `ask` is the verdict before the mode's decider
  // settles it, and a rule, a hook or the classifier often allows the call with
  // no card ever drawn (found live on jj: a `python3 -c` the allow-list passed
  // still read as "needs input"). PermissionRequest is raised only where a card
  // is about to be shown, and a settings hook that answers it leaves none.
  on('classic.PermissionRequest', async ($, e, next) => {
    const r = await next(e)
    if (!r.decision) {
      asking.set(e.tool_name, (asking.get(e.tool_name) ?? 0) + 1)
      // Report only; never let a failed write change the decision.
      await emit($, 'ask', { tool: e.tool_name }).catch(() => {})
    }
    return r
  })

  // The card was answered, either way, once the call it held up settles.
  on('tool.call', async ($, e, next) => {
    const r = await next(e)
    const n = asking.get(e.tool) ?? 0
    if (n > 0) {
      if (n === 1) asking.delete(e.tool); else asking.set(e.tool, n - 1)
      await emit($, 'answered', { tool: e.tool }).catch(() => {})
    }
    return r
  })
}
