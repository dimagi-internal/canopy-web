// A candidate's private "pick a time" link. They see the times the recruiter
// offered, grouped by day, choose one, confirm it, and get a calendar invite by
// email. Once a time is chosen (or the interview is cancelled) the link says so.
// Built for a phone: big real buttons, a visible focus ring, no canopy chrome.
const FOCUS = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2'

function parseSlots(text) {
  const out = []
  for (const line of String(text || '').split('\n')) {
    const m = line.trim().match(/^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}:\d{2})/)
    if (m) out.push({ date: m[1], time: m[2].padStart(5, '0') })
  }
  return out
}

function dayLabel(date) {
  const d = new Date(`${date}T12:00:00`)
  if (Number.isNaN(d.getTime())) return date
  return d.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
}

function timeLabel(time) {
  const [h, m] = time.split(':').map(Number)
  if (!Number.isFinite(h)) return time
  return `${((h + 11) % 12) + 1}:${String(m).padStart(2, '0')} ${h < 12 ? 'AM' : 'PM'}`
}

function Shell({ app, children }) {
  const company = (app.settings && app.settings.company_name) || ''
  return (
    <main className="min-h-screen bg-background px-4 py-8 text-foreground sm:py-12">
      <div className="mx-auto max-w-xl">
        {company && <div className="mb-6 text-sm font-semibold tracking-wide text-muted-foreground">{company}</div>}
        {children}
      </div>
    </main>
  )
}

function View({ data, app }) {
  const r = (data && data.record) || {}
  const [picked, setPicked] = useState(null)
  const [state, setState] = useState({ kind: 'idle' })
  const name = (r.application && r.application.name) || ''
  const first = name.split(' ')[0]
  const jobTitle = (r.job && r.job.title) || 'the role'
  const kind = r.kind ? String(r.kind).toLowerCase() : 'conversation'
  const tz = r.timezone || 'your recruiter\'s time zone'
  const today = new Date().toISOString().slice(0, 10)
  const slots = parseSlots(r.slot_options).filter((s) => s.date >= today)
  const days = []
  for (const s of slots) {
    let d = days.find((x) => x.date === s.date)
    if (!d) days.push(d = { date: s.date, times: [] })
    d.times.push(s.time)
  }

  async function confirm() {
    setState({ kind: 'sending' })
    try {
      await app.act('pick_slot', { input: { date: picked.date, time: picked.time } })
      setState({ kind: 'booked', date: picked.date, time: picked.time })
    } catch (e) {
      setState({ kind: 'failed', message: e.message })
    }
  }

  const booked = state.kind === 'booked' ? state : (r.status === 'Scheduled' && r.date ? { date: r.date, time: r.time } : null)
  if (booked) {
    return (
      <Shell app={app}>
        <h1 className="text-2xl font-semibold" tabIndex={-1}>{state.kind === 'booked' ? 'You\'re booked — thank you!' : 'You\'re booked'}</h1>
        <p className="mt-4 text-lg">
          {first ? `${first}, your` : 'Your'} {kind} about {jobTitle} is on <strong>{dayLabel(booked.date)}</strong>
          {booked.time && <> at <strong>{timeLabel(booked.time)}</strong></>} ({tz}){r.duration ? `, for about ${r.duration} minutes` : ''}.
        </p>
        <p className="mt-3 text-muted-foreground">You'll get a calendar invite by email with how to join. If you need to change the time, reply to that email.</p>
      </Shell>
    )
  }
  if (r.status === 'Cancelled') {
    return (
      <Shell app={app}>
        <h1 className="text-2xl font-semibold">This interview was cancelled</h1>
        <p className="mt-4 text-muted-foreground">The {kind} about {jobTitle} is no longer planned. The hiring team will be in touch if there's a new time.</p>
      </Shell>
    )
  }
  if (r.status === 'Done') {
    return (
      <Shell app={app}>
        <h1 className="text-2xl font-semibold">This interview has already taken place</h1>
        <p className="mt-4 text-muted-foreground">Thank you for your time. The hiring team will be in touch.</p>
      </Shell>
    )
  }
  if (r.status !== 'Requested') {
    return (
      <Shell app={app}>
        <h1 className="text-2xl font-semibold">Nothing to choose here</h1>
        <p className="mt-4 text-muted-foreground">This link has no times waiting for you. If you think that's a mistake, reply to the email it came in.</p>
      </Shell>
    )
  }

  return (
    <Shell app={app}>
      <h1 className="text-2xl font-semibold leading-snug">Hi{first ? ` ${first}` : ''}, choose a time for your {kind} about {jobTitle}</h1>
      <p className="mt-3 text-muted-foreground">
        {r.duration ? `About ${r.duration} minutes. ` : ''}All times are in <strong className="text-foreground">{tz}</strong>.
      </p>

      {days.length === 0 ? (
        <p className="mt-8 rounded-lg border border-border bg-muted p-4">There are no upcoming times left to choose from. Please reply to the email this link came in and we'll find another.</p>
      ) : (
        <div className="mt-8 flex flex-col gap-6" role="group" aria-label="Available times">
          {days.map((d) => (
            <section key={d.date} aria-labelledby={`day-${d.date}`}>
              <h2 id={`day-${d.date}`} className="mb-2 text-base font-semibold">{dayLabel(d.date)}</h2>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {d.times.map((t) => {
                  const on = picked && picked.date === d.date && picked.time === t
                  return (
                    <button key={t} type="button" aria-pressed={on} onClick={() => { setPicked({ date: d.date, time: t }); setState({ kind: 'idle' }) }}
                      className={`min-h-12 rounded-lg border px-3 py-3 text-base font-medium ${FOCUS} ${on ? 'border-primary bg-primary text-primary-foreground' : 'border-border bg-card hover:bg-muted'}`}>
                      {timeLabel(t)}
                    </button>
                  )
                })}
              </div>
            </section>
          ))}
        </div>
      )}

      {picked && (
        <div className="sticky bottom-0 mt-8 rounded-lg border border-border bg-card p-4 shadow-sm" aria-live="polite">
          <div className="text-base">You chose <strong>{dayLabel(picked.date)} at {timeLabel(picked.time)}</strong> ({tz}).</div>
          {state.kind === 'failed' && <div className="mt-2 text-sm text-destructive" role="alert">That didn't work: {state.message} Please try another time, or reply to the email.</div>}
          <div className="mt-3 flex flex-wrap gap-2">
            <button type="button" disabled={state.kind === 'sending'} onClick={confirm}
              className={`min-h-12 flex-1 rounded-lg bg-primary px-4 py-3 text-base font-semibold text-primary-foreground ${FOCUS} ${state.kind === 'sending' ? 'opacity-60' : 'hover:opacity-90'}`}>
              {state.kind === 'sending' ? 'Booking…' : 'Confirm this time'}
            </button>
            <button type="button" onClick={() => setPicked(null)} className={`min-h-12 rounded-lg border border-border px-4 py-3 text-base ${FOCUS}`}>Change</button>
          </div>
        </div>
      )}
    </Shell>
  )
}
