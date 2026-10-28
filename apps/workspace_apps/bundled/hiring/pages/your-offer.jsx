// A candidate's private offer link: the offer laid out plainly, and Accept or
// Decline, each with an optional note to the hiring team. Declining asks for a
// confirmation first. Once answered — or if the offer was withdrawn — the link
// shows where it stands instead of the buttons. Built for a phone.
const FOCUS = 'focus:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2'
const BIG = `min-h-12 rounded-lg px-4 py-3 text-base font-semibold ${FOCUS}`

function money(n, currency) {
  if (n == null || n === '') return ''
  try {
    return Number(n).toLocaleString(undefined, { style: 'currency', currency: currency || 'USD', maximumFractionDigits: 0 })
  } catch (e) {
    return `${currency || ''} ${Number(n).toLocaleString()}`.trim()
  }
}

function longDate(d) {
  if (!d) return ''
  const x = new Date(`${d}T12:00:00`)
  return Number.isNaN(x.getTime()) ? d : x.toLocaleDateString(undefined, { year: 'numeric', month: 'long', day: 'numeric' })
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

const STATE_TEXT = {
  accepted: ['You accepted this offer', 'Welcome aboard! The hiring team will be in touch about next steps.'],
  hired: ['You accepted this offer', 'Welcome aboard! The hiring team will be in touch about next steps.'],
  declined: ['You declined this offer', 'Thank you for letting us know, and for the time you spent with us.'],
  withdrawn: ['This offer is no longer available', 'It was withdrawn. If you have questions, reply to the email this link came in.'],
}

function View({ data, app }) {
  const o = (data && data.record) || {}
  const [mode, setMode] = useState(null) // null | 'accept' | 'decline'
  const [note, setNote] = useState('')
  const [state, setState] = useState({ kind: 'idle' })
  const name = (o.application && o.application.name) || ''
  const first = name.split(' ')[0]
  const company = (app.settings && app.settings.company_name) || 'us'
  const job = o.job || {}

  async function answer(action) {
    setState({ kind: 'sending' })
    try {
      await app.act(action, { input: note.trim() ? { candidate_note: note.trim() } : {} })
      setState({ kind: 'done', status: action === 'accept_offer' ? 'accepted' : 'declined' })
    } catch (e) {
      setState({ kind: 'failed', message: e.message })
    }
  }

  const status = state.kind === 'done' ? state.status : o.status
  const terms = [
    ['Role', o.title || job.title],
    ['Location', job.location],
    ['Start date', longDate(o.start_date)],
    ['Pay', o.salary != null && o.salary !== '' ? `${money(o.salary, o.currency)} per ${o.pay_period || 'year'}` : ''],
    ['Engagement', o.employment_type],
  ].filter(([, v]) => v)

  const summary = (
    <section className="mt-6 rounded-lg border border-border bg-card p-4 sm:p-6" aria-label="Offer details">
      <dl className="grid gap-x-6 gap-y-3 text-base sm:grid-cols-[max-content_1fr]">
        {terms.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-sm text-muted-foreground sm:text-base">{k}</dt>
            <dd className="font-medium">{v}</dd>
          </div>
        ))}
      </dl>
      {o.details && (
        <div className="mt-4 border-t border-border pt-4">
          <h2 className="mb-1 text-sm font-semibold">Other terms</h2>
          <p className="whitespace-pre-wrap text-base">{o.details}</p>
        </div>
      )}
    </section>
  )

  if (status !== 'sent') {
    const [title, body] = STATE_TEXT[status] || ['This offer is not open for an answer', 'If you think that is a mistake, reply to the email this link came in.']
    return (
      <Shell app={app}>
        <h1 className="text-2xl font-semibold">{state.kind === 'done' ? (status === 'accepted' ? `Thank you${first ? `, ${first}` : ''} — you've accepted!` : `Thank you${first ? `, ${first}` : ''}`) : title}</h1>
        <p className="mt-3 text-muted-foreground">{state.kind === 'done' && status === 'accepted'
          ? `We're delighted. The hiring team has been told and will be in touch about next steps.`
          : state.kind === 'done' ? 'We\'ve let the hiring team know. We appreciate your time and wish you the very best.' : body}</p>
        {terms.length > 0 && status !== 'withdrawn' && summary}
      </Shell>
    )
  }

  return (
    <Shell app={app}>
      <h1 className="text-2xl font-semibold leading-snug">{first ? `${first}, we're` : 'We\'re'} delighted to offer you the role of {o.title || job.title || 'this role'}</h1>
      <p className="mt-3 text-muted-foreground">Here is the offer from {company}. Read it through, then accept or decline below.</p>
      {summary}

      {mode === null && (
        <div className="mt-6 flex flex-col gap-2 sm:flex-row">
          <button type="button" className={`${BIG} flex-1 bg-primary text-primary-foreground hover:opacity-90`} onClick={() => setMode('accept')}>Accept the offer</button>
          <button type="button" className={`${BIG} flex-1 border border-border bg-card hover:bg-muted`} onClick={() => setMode('decline')}>Decline</button>
        </div>
      )}

      {mode !== null && (
        <section className="mt-6 rounded-lg border border-border bg-card p-4" aria-live="polite">
          <h2 className="text-lg font-semibold">{mode === 'accept' ? 'Accept this offer' : 'Decline this offer?'}</h2>
          {mode === 'decline' && <p className="mt-1 text-sm text-muted-foreground">Once you decline, this link can't be used to accept. If you'd like to talk anything over first, reply to the email instead.</p>}
          <label className="mt-3 block text-sm">
            <span className="mb-1 block font-medium">A note to the hiring team (optional)</span>
            <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} maxLength={2000}
              className={`w-full rounded-md border border-border bg-background px-3 py-2 text-base ${FOCUS}`} />
          </label>
          {state.kind === 'failed' && <div className="mt-2 text-sm text-destructive" role="alert">That didn't go through: {state.message}</div>}
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <button type="button" disabled={state.kind === 'sending'}
              className={`${BIG} flex-1 ${mode === 'accept' ? 'bg-primary text-primary-foreground' : 'border border-destructive/40 bg-destructive/10 text-destructive'} ${state.kind === 'sending' ? 'opacity-60' : 'hover:opacity-90'}`}
              onClick={() => answer(mode === 'accept' ? 'accept_offer' : 'decline_offer')}>
              {state.kind === 'sending' ? 'Sending…' : mode === 'accept' ? 'Yes, I accept' : 'Yes, decline the offer'}
            </button>
            <button type="button" className={`${BIG} border border-border bg-card font-normal hover:bg-muted`} onClick={() => { setMode(null); setState({ kind: 'idle' }) }}>Go back</button>
          </div>
        </section>
      )}
    </Shell>
  )
}
