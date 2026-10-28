// Approvals: what is waiting on the signed-in person — jobs to open and offers to
// make — with what they are agreeing to, and Approve / Decline with a note. A job
// opens (and an offer becomes sendable) when its last approver approves; one
// decline sends it back to draft. Recruiters also see everything still waiting,
// by approver, so they know whom to chase.
const TONE = { pending: 'warning', approved: 'success', declined: 'destructive' }

function blank(v) { return v == null || v === '' }
function money(n) { return Number(n).toLocaleString('en-US') }
function jobPay(j, app) {
  if (!j || (blank(j.pay_min) && blank(j.pay_max))) return 'No pay range set'
  const range = !blank(j.pay_min) && !blank(j.pay_max) ? `${money(j.pay_min)}–${money(j.pay_max)}` : money(blank(j.pay_min) ? j.pay_max : j.pay_min)
  return `${j.pay_currency || 'USD'} ${range} per ${app.optionLabel('job', 'pay_period', j.pay_period || 'YEAR')}`
}
function since(iso) {
  if (!iso) return ''
  const d = Math.round((Date.now() - new Date(iso).getTime()) / 86400000)
  return d <= 0 ? 'today' : d === 1 ? '1 day' : `${d} days`
}

function Pending({ app, a, job, offer, offerHidden, candidate, onDone }) {
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  async function decide(action) {
    setBusy(true)
    setErr(null)
    try {
      await app.act(action, { record: a.id, input: note ? { note } : {} })
      onDone()
    } catch (e) {
      setErr(e.message)
      setBusy(false)
    }
  }
  const facts = a.kind === 'Offer'
    ? (offer
      ? [['Candidate', candidate ? candidate.name : `Candidate #${offer.application}`], ['Title offered', offer.title],
        ['Salary', blank(offer.salary) ? 'Not set' : `${offer.currency || 'USD'} ${money(offer.salary)} per ${offer.pay_period || 'year'}`],
        ['Engagement', offer.employment_type], ['Start date', offer.start_date], ['Other terms', offer.details],
        ['Job pay range', jobPay(job, app)]]
      : [['Job', job ? job.title : `Job #${a.job}`], ['What you are approving', a.summary], ['Offer', `#${a.offer || '?'}`],
        ['Terms', offerHidden ? 'Offer terms are readable by recruiters only — ask the recruiter for the letter.' : a.offer ? 'Loading…' : 'This approval names no offer.']])
    : [['Team', job && [job.department, job.office].filter(Boolean).join(' · ')], ['Location', job && [job.location, job.workplace, job.employment_type].filter(Boolean).join(' · ')],
      ['Pay range', jobPay(job, app)], ['Openings', job && job.openings], ['Requisition ID', job && job.requisition_id],
      ['Hiring manager', job && job.hiring_manager], ['Recruiter', job && job.recruiter]]
  return (
    <Card title={<span className="flex flex-wrap items-center gap-2"><Badge tone={a.kind === 'Offer' ? 'warning' : 'default'}>{a.kind}</Badge>{job ? job.title : `Job #${a.job}`}</span>}
      actions={<span className="text-xs text-muted-foreground">waiting {since(a.created_at)}</span>}>
      <Stack gap={3}>
        <dl className="grid gap-x-4 gap-y-1 text-sm" style={{ gridTemplateColumns: 'max-content 1fr' }}>
          {facts.filter(([, v]) => !blank(v)).map(([k, v]) => (
            <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd className="whitespace-pre-wrap">{String(v)}</dd></div>
          ))}
        </dl>
        {job && a.kind === 'Job' && (
          <button type="button" className="self-start text-sm text-primary hover:underline" onClick={() => app.go('job-setup', { id: job.id })}>Read the full job</button>
        )}
        <TextArea label="Note (optional — the recruiter sees it)" rows={2} value={note} onChange={setNote} />
        {err && <div className="text-sm text-destructive" role="alert">{err}</div>}
        <div className="flex gap-2">
          <Button tone="primary" disabled={busy} onClick={() => decide('approve')}>Approve</Button>
          <Button tone="destructive" disabled={busy} confirm={`Decline this ${a.kind === 'Offer' ? 'offer' : 'job'}? It goes back to draft for the recruiter.`} onClick={() => decide('decline_approval')}>Decline…</Button>
        </div>
      </Stack>
    </Card>
  )
}

function View({ data, app, scope }) {
  const all = Array.isArray(data.approvals) ? data.approvals : []
  const jobs = Array.isArray(data.jobs) ? data.jobs : []
  const me = ((scope && scope.viewer && scope.viewer.email) || '').toLowerCase()
  const recruiter = app.roles.includes('recruiter')
  const jobById = Object.fromEntries(jobs.map((j) => [j.id, j]))
  const mine = all.filter((a) => (a.approver || '').toLowerCase() === me)
  const myPending = mine.filter((a) => a.status === 'pending')
  const myDecided = mine.filter((a) => a.status !== 'pending').sort((x, y) => String(y.decided_at || '').localeCompare(String(x.decided_at || '')))
  const offerIds = [...new Set(all.filter((a) => a.kind === 'Offer' && a.offer).map((a) => a.offer))]
  const [offers, setOffers] = useState({})
  const [offerHidden, setOfferHidden] = useState(false)
  const [people, setPeople] = useState({})

  useEffect(() => {
    if (offerIds.length === 0) return
    app.query('offer', { where: { id: offerIds.slice(0, 50) }, limit: 50 }).then((rows) => {
      setOffers(Object.fromEntries(rows.map((o) => [o.id, o])))
      const appIds = [...new Set(rows.map((o) => o.application).filter(Boolean))]
      if (appIds.length) {
        app.query('application', { where: { id: appIds }, limit: 50 })
          .then((r) => setPeople(Object.fromEntries(r.map((p) => [p.id, p]))), () => {})
      }
    }, () => setOfferHidden(true))
  }, [offerIds.join(',')])

  const waiting = all.filter((a) => a.status === 'pending')
  const byApprover = {}
  for (const a of waiting) (byApprover[a.approver] = byApprover[a.approver] || []).push(a)
  const chase = Object.entries(byApprover).map(([approver, list]) => ({
    approver, count: list.length, oldest: list.reduce((m, a) => (!m || a.created_at < m ? a.created_at : m), ''), list,
  })).sort((x, y) => y.count - x.count)
  const what = (a) => {
    const j = jobById[a.job]
    const o = offers[a.offer]
    return a.kind === 'Offer' ? `Offer${o ? ` · ${o.title}${people[o.application] ? ` for ${people[o.application].name}` : ''}` : ''} (${j ? j.title : `job #${a.job}`})` : (j ? j.title : `Job #${a.job}`)
  }

  return (
    <Stack gap={4}>
      <Grid cols={recruiter ? 3 : 2}>
        <Stat label="Waiting on you" value={myPending.length} tone={myPending.length ? 'warning' : 'default'} />
        <Stat label="You decided" value={myDecided.length} />
        {recruiter && <Stat label="Waiting across the team" value={waiting.length} hint={`${chase.length} approver${chase.length === 1 ? '' : 's'}`} />}
      </Grid>
      {data.approvals && data.approvals.error && <Empty>{data.approvals.error}</Empty>}
      {myPending.length === 0 ? (
        <Card title="Waiting on you"><Empty>Nothing needs your approval right now.</Empty></Card>
      ) : (
        <Stack gap={3}>
          <h2 className="text-sm font-semibold">Waiting on you ({myPending.length})</h2>
          {myPending.map((a) => {
            const o = offers[a.offer]
            return <Pending key={a.id} app={app} a={a} job={jobById[a.job]} offer={o} offerHidden={offerHidden}
              candidate={o ? people[o.application] : null} onDone={() => app.refresh()} />
          })}
        </Stack>
      )}
      <Card title={`Your decisions (${myDecided.length})`}>
        <Table rows={myDecided} empty="You haven't decided any approvals yet." columns={[
          { key: 'kind', label: 'For', render: (a) => <Badge>{a.kind}</Badge> },
          { key: 'job', label: 'What', render: what },
          { key: 'status', label: 'Decision', render: (a) => <Badge tone={TONE[a.status]}>{app.optionLabel('approval', 'status', a.status)}</Badge> },
          { key: 'decided_at', label: 'When', render: (a) => (a.decided_at || '').slice(0, 16).replace('T', ' ') },
          { key: 'note', label: 'Your note' },
        ]} />
      </Card>
      {recruiter && (
        <Card title="Everything waiting, by approver">
          <Table rows={chase} empty="No approvals are waiting." columns={[
            { key: 'approver', label: 'Approver' },
            { key: 'count', label: 'Waiting', align: 'right' },
            { key: 'oldest', label: 'Oldest', render: (r) => since(r.oldest) },
            { key: 'list', label: 'On', render: (r) => (
              <div className="flex flex-col gap-0.5">
                {r.list.map((a) => (
                  <button key={a.id} type="button" className="text-left hover:underline"
                    onClick={() => (a.kind === 'Offer' ? app.go('offers', { id: a.offer }) : app.go('job-setup', { id: a.job }))}>
                    {what(a)}
                  </button>
                ))}
              </div>
            ) },
          ]} />
        </Card>
      )}
    </Stack>
  )
}
