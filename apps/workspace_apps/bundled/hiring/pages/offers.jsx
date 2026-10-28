// Offers: every offer and where it stands, and the whole workflow on one —
// draft → approvers → approved → sent (the candidate gets an email with a private
// link to accept or decline) → accepted → hired, which closes the opening.
// Changing an offer sends it back to draft, so it is approved again.
const STATUS_TONE = { draft: 'default', pending_approval: 'warning', approved: 'success', sent: 'warning', accepted: 'success', declined: 'destructive', withdrawn: 'default', hired: 'success' }
const APPROVAL_TONE = { pending: 'warning', approved: 'success', declined: 'destructive' }
const TERMS = ['opening', 'title', 'start_date', 'salary', 'currency', 'pay_period', 'employment_type', 'details', 'letter']
const EDITABLE = ['draft', 'pending_approval', 'approved', 'declined']
const WITHDRAWABLE = ['draft', 'pending_approval', 'approved', 'sent']

function inputsOf(app, action, fallback) {
  const a = (app.dataModel && app.dataModel._actions) || {}
  return (a[action] && a[action].input) || fallback
}
function blank(v) { return v == null || v === '' }
function salaryText(o) {
  return blank(o.salary) ? '—' : `${o.currency || 'USD'} ${Number(o.salary).toLocaleString('en-US')} / ${o.pay_period || 'year'}`
}
function fileId(v) { return v && typeof v === 'object' ? v.id : v }

function Terms({ app, offer, openings, onSave, onCancel, saveLabel }) {
  const [f, setF] = useState({ currency: 'USD', pay_period: 'year', employment_type: 'Employee', ...offer, letter: null })
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v }))
  return (
    <Form onSubmit={() => onSave(f)}>
      <Stack gap={3}>
        <Grid cols={2}>
          <Input label="Job title offered" required value={f.title} onChange={set('title')} />
          <Select label="Opening it fills" value={String(f.opening || '')} onChange={(v) => set('opening')(v ? Number(v) : null)}
            options={[{ value: '', label: openings.length ? 'Choose an opening…' : 'No open openings on this job' },
              ...openings.map((o) => ({ value: String(o.id), label: o.opening_id }))]} />
          <Input label="Start date" type="date" value={f.start_date} onChange={set('start_date')} />
          <Select label="Engagement" value={f.employment_type} onChange={set('employment_type')} options={app.options('offer', 'employment_type')} />
        </Grid>
        <Grid cols={3}>
          <Input label="Salary" type="number" value={blank(f.salary) ? '' : String(f.salary)} onChange={(v) => set('salary')(v === '' ? null : Number(v))} />
          <Select label="Currency" value={f.currency} onChange={set('currency')} options={app.options('offer', 'currency')} />
          <Select label="Per" value={f.pay_period} onChange={set('pay_period')} options={app.options('offer', 'pay_period')} />
        </Grid>
        <TextArea label="Other terms (bonus, benefits, equipment)" rows={4} value={f.details} onChange={set('details')} />
        <FileInput label={offer && offer.letter ? 'Replace the offer letter (PDF or Word)' : 'Offer letter (PDF or Word)'} collection="offer" field="letter" value={f.letter} onChange={set('letter')} />
        <FieldInputs collection="offer" action={offer && offer.id ? 'update_offer' : 'create_offer'} values={f} onChange={setF} exclude={TERMS} />
        <div className="flex gap-2"><Button type="submit" tone="primary">{saveLabel}</Button><Button onClick={onCancel}>Cancel</Button></div>
      </Stack>
    </Form>
  )
}

function NewOffer({ app, candidates, jobs, onCreated, onCancel }) {
  const [appId, setAppId] = useState(candidates[0] ? String(candidates[0].id) : '')
  const [openings, setOpenings] = useState([])
  const [err, setErr] = useState(null)
  const cand = candidates.find((c) => String(c.id) === appId)
  const job = cand ? jobs.find((j) => j.id === cand.job) : null
  useEffect(() => {
    if (!cand || !cand.job) { setOpenings([]); return }
    app.query('opening', { where: { job: cand.job, status: 'open' }, limit: 100 }).then(setOpenings, () => setOpenings([]))
  }, [appId])
  if (candidates.length === 0) return <Card title="Make an offer"><Empty>No active candidate is at the Offer stage. Move one there from the pipeline first.</Empty><Button onClick={onCancel}>Back</Button></Card>
  async function save(f) {
    setErr(null)
    const allowed = inputsOf(app, 'create_offer', ['application', 'job', ...TERMS])
    const input = { application: cand.id, job: cand.job || null }
    for (const k of allowed) if (k in f && !blank(f[k]) && !(k in input)) input[k] = k === 'letter' ? fileId(f[k]) : f[k]
    try { const o = await app.act('create_offer', { input }); onCreated(o.id) } catch (e) { setErr(e.message) }
  }
  const base = job ? { title: job.title, currency: job.pay_currency || 'USD', pay_period: (job.pay_period || 'YEAR').toLowerCase(), opening: null } : {}
  return (
    <Card title="Make an offer">
      <Stack gap={3}>
        <Select label="Candidate (at the Offer stage)" value={appId} onChange={setAppId}
          options={candidates.map((c) => { const j = jobs.find((x) => x.id === c.job); return { value: String(c.id), label: `${c.name}${j ? ` — ${j.title}` : ''}` } })} />
        {job && !blank(job.pay_min) && <div className="text-xs text-muted-foreground">The job's posted range: {job.pay_currency} {Number(job.pay_min).toLocaleString('en-US')}–{Number(job.pay_max || job.pay_min).toLocaleString('en-US')} per {(job.pay_period || 'YEAR').toLowerCase()}.</div>}
        <Terms key={appId} app={app} offer={base} openings={openings} onSave={save} onCancel={onCancel} saveLabel="Save as draft" />
        {err && <div className="text-sm text-destructive" role="alert">{err}</div>}
      </Stack>
    </Card>
  )
}

function OfferPanel({ app, offer, candidate, job, onChanged, onClose }) {
  const [approvals, setApprovals] = useState([])
  const [openings, setOpenings] = useState([])
  const [editing, setEditing] = useState(false)
  const [approver, setApprover] = useState('')
  const [answer, setAnswer] = useState(null)
  const [signed, setSigned] = useState(null)
  const [link, setLink] = useState(null)
  const [err, setErr] = useState(null)
  const loadApprovals = () => app.query('approval', { where: { offer: offer.id, kind: 'Offer' }, limit: 100 }).then(setApprovals, () => setApprovals([]))
  useEffect(() => {
    loadApprovals()
    if (offer.job) app.query('opening', { where: { job: offer.job }, limit: 100 }).then(setOpenings, () => setOpenings([]))
  }, [offer.id, offer.updated_at])

  async function run(fn) {
    setErr(null)
    try { await fn(); await loadApprovals(); onChanged(); return true } catch (e) { setErr(e.message); return false }
  }
  const pending = approvals.filter((a) => a.status === 'pending')
  const opening = openings.find((o) => o.id === offer.opening)
  const s = offer.status
  async function getLink() {
    setErr(null)
    try {
      const l = await app.link('offer', offer.id)
      setLink(l)
      try { await navigator.clipboard.writeText(l.url) } catch (e) { /* shown below to copy by hand */ }
    } catch (e) { setErr(e.message) }
  }

  return (
    <Card title={<span className="flex flex-wrap items-center gap-2">{offer.title} <Badge tone={STATUS_TONE[s]}>{app.optionLabel('offer', 'status', s)}</Badge></span>}
      actions={<Button size="sm" onClick={onClose}>Close</Button>}>
      <Stack gap={4}>
        <dl className="grid gap-x-4 gap-y-1 text-sm" style={{ gridTemplateColumns: 'max-content 1fr' }}>
          {[['Candidate', candidate ? <button type="button" className="hover:underline" onClick={() => app.go('candidate', { id: candidate.id })}>{candidate.name}</button> : `#${offer.application}`],
            ['Job', job ? job.title : ''], ['Opening', opening ? opening.opening_id : (offer.opening ? `#${offer.opening}` : 'None chosen')],
            ['Salary', salaryText(offer)], ['Engagement', offer.employment_type], ['Start date', offer.start_date], ['Other terms', offer.details],
            ['Sent', offer.sent_on], ['Answered', (offer.responded_at || '').slice(0, 16).replace('T', ' ')], ["Candidate's note", offer.candidate_note]]
            .filter(([, v]) => !blank(v)).map(([k, v]) => <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd className="whitespace-pre-wrap">{v}</dd></div>)}
        </dl>
        <FieldValues collection="offer" record={offer} />
        <div className="flex flex-wrap gap-2">
          {offer.letter && <Button size="sm" onClick={() => app.openFile(offer.letter)}>Open offer letter</Button>}
          {offer.signed_letter && <Button size="sm" onClick={() => app.openFile(offer.signed_letter)}>Open signed letter</Button>}
        </div>

        {err && <div className="text-sm text-destructive" role="alert">{err}</div>}
        <div className="flex flex-wrap items-center gap-2">
          {s === 'draft' && <Button tone="primary" disabled={approvals.length === 0} onClick={() => run(async () => {
            for (const a of approvals.filter((x) => x.status !== 'pending')) await app.act('reset_approval', { record: a.id })
            await app.act('request_offer_approval', { record: offer.id })
          })}>Ask for approval ({approvals.length})</Button>}
          {s === 'draft' && approvals.length === 0 && <Button confirm="Approve this offer yourself, without approvers? Use this only where your process needs no sign-off."
            onClick={() => run(() => app.act('approve_offer_directly', { record: offer.id }))}>Approve without approvers</Button>}
          {s === 'approved' && <Button tone="primary" confirm={`Send the offer to ${candidate ? candidate.name : 'the candidate'}? They get an email with a private link to read it and accept or decline.`}
            onClick={() => run(() => app.act('send_offer', { record: offer.id }))}>Send the offer</Button>}
          {s === 'sent' && <Button onClick={getLink}>Copy the candidate's link</Button>}
          {s === 'sent' && answer === null && <Button onClick={() => setAnswer({ status: 'accepted', candidate_note: '' })}>Record an answer from email…</Button>}
          {EDITABLE.includes(s) && !editing && <Button onClick={() => setEditing(true)}>Change the offer</Button>}
          {WITHDRAWABLE.includes(s) && <Button tone="destructive" confirm="Withdraw this offer? The candidate's link stops working for accepting it." onClick={() => run(() => app.act('withdraw_offer', { record: offer.id }))}>Withdraw</Button>}
        </div>
        {s === 'draft' && approvals.length === 0 && <div className="text-xs text-muted-foreground">Add approvers below and ask for approval — the offer can be sent once every approver has approved — or approve it yourself if your process needs no sign-off.</div>}
        {s === 'pending_approval' && <div className="text-sm text-muted-foreground">Waiting on {pending.length} of {approvals.length} approver{approvals.length === 1 ? '' : 's'}. It becomes sendable when the last one approves.</div>}
        {s === 'approved' && <div className="text-xs text-muted-foreground">Sending emails the candidate a private link (valid 30 days) to read the offer and accept or decline it.</div>}
        {link && <div className="rounded-md border border-border bg-muted p-2 text-xs">
          <div className="mb-1">The candidate's link (works for {link.days} days) — copied if your browser allowed it:</div>
          <code className="break-all">{link.url}</code></div>}

        {editing && (
          <div className="rounded-md border border-border p-3">
            <div className="mb-2 text-sm text-warning">Saving puts the offer back to draft: changing an approved offer needs approval again.</div>
            <Terms app={app} offer={offer} openings={openings.filter((o) => o.status === 'open' || o.id === offer.opening)} saveLabel="Save changes"
              onCancel={() => setEditing(false)} onSave={async (f) => {
                const input = {}
                for (const k of inputsOf(app, 'update_offer', TERMS)) {
                  if (!(k in f)) continue
                  if (k === 'letter') { if (f.letter) input.letter = fileId(f.letter) } else input[k] = f[k] === '' ? null : f[k]
                }
                if (await run(() => app.act('update_offer', { record: offer.id, input }))) setEditing(false)
              }} />
          </div>
        )}

        {answer && (
          <Form onSubmit={async () => {
            const input = { status: answer.status, candidate_note: answer.candidate_note || null }
            if (signed) input.signed_letter = signed.id
            if (await run(() => app.act('record_offer_answer', { record: offer.id, input }))) { setAnswer(null); setSigned(null) }
          }}>
            <Stack gap={2}>
              <div className="text-sm font-medium">The candidate answered by email or phone</div>
              <Select label="Answer" value={answer.status} onChange={(status) => setAnswer({ ...answer, status })}
                options={[{ value: 'accepted', label: 'Accepted' }, { value: 'declined', label: 'Declined' }]} />
              <TextArea label="What they said" rows={2} value={answer.candidate_note} onChange={(candidate_note) => setAnswer({ ...answer, candidate_note })} />
              {answer.status === 'accepted' && <FileInput label="Signed letter (optional, PDF)" collection="offer" field="signed_letter" value={signed} onChange={setSigned} />}
              <div className="flex gap-2"><Button type="submit" tone="primary">Record answer</Button><Button onClick={() => setAnswer(null)}>Cancel</Button></div>
            </Stack>
          </Form>
        )}

        {s === 'accepted' && (
          <Form onSubmit={() => run(() => app.act('mark_hired', { record: offer.id, input: signed ? { signed_letter: signed.id } : {} }))}>
            <Stack gap={2}>
              <div className="text-sm">Accepted. Marking hired moves {candidate ? candidate.name : 'the candidate'} to Hired and closes {opening ? `opening ${opening.opening_id}` : 'the opening'}.</div>
              {!offer.signed_letter && <FileInput label="Signed letter (optional, PDF)" collection="offer" field="signed_letter" value={signed} onChange={setSigned} />}
              <div><Button type="submit" tone="primary">Mark hired</Button></div>
            </Stack>
          </Form>
        )}

        <div>
          <div className="mb-1 text-sm font-medium">Approvers</div>
          <Table rows={approvals} empty="No approvers yet." columns={[
            { key: 'approver', label: 'Approver' },
            { key: 'status', label: 'Status', render: (a) => <Badge tone={APPROVAL_TONE[a.status]}>{app.optionLabel('approval', 'status', a.status)}</Badge> },
            { key: 'decided_at', label: 'Decided', render: (a) => (a.decided_at || '').slice(0, 16).replace('T', ' ') },
            { key: 'note', label: 'Note' },
            { key: 'act', label: '', render: (a) => a.status === 'pending'
              ? <Button size="sm" onClick={() => run(() => app.act('remove_approver', { record: a.id }))}>Remove</Button>
              : <Button size="sm" onClick={() => run(() => app.act('reset_approval', { record: a.id }))}>Ask again</Button> },
          ]} />
          {['draft', 'pending_approval'].includes(s) && (
            <Form onSubmit={async () => {
              if (await run(() => app.act('add_approver', { input: { kind: 'Offer', job: offer.job || (candidate && candidate.job), offer: offer.id, approver, summary: `Offer to ${(candidate && candidate.name) || 'the candidate'}: ${offer.title}, ${offer.currency || ''} ${offer.salary ?? '—'} per ${offer.pay_period || 'year'}${offer.start_date ? `, starting ${offer.start_date}` : ''} (${offer.employment_type || 'Employee'}).` } }))) setApprover('')
            }}>
              <div className="mt-2 flex flex-wrap items-end gap-2">
                <Input label="Add an approver (email)" type="email" required value={approver} onChange={setApprover} />
                <Button type="submit">Add approver</Button>
              </div>
            </Form>
          )}
        </div>
      </Stack>
    </Card>
  )
}

function View({ data, app }) {
  const offers = Array.isArray(data.offers) ? data.offers : []
  const candidates = Array.isArray(data.applications) ? data.applications : []
  const jobs = Array.isArray(data.jobs) ? data.jobs : []
  const [selected, setSelected] = useState(app.params.id ? Number(app.params.id) : null)
  const [creating, setCreating] = useState(false)
  const [filter, setFilter] = useState('')
  const [extra, setExtra] = useState({})

  const people = { ...Object.fromEntries(candidates.map((c) => [c.id, c])), ...extra }
  const missing = [...new Set(offers.map((o) => o.application).filter((id) => id && !people[id]))]
  useEffect(() => {
    if (missing.length === 0) return
    app.query('application', { where: { id: missing.slice(0, 50) }, limit: 50 })
      .then((rows) => setExtra((x) => ({ ...x, ...Object.fromEntries(rows.map((r) => [r.id, r])) })), () => {})
  }, [missing.join(',')])

  const jobTitle = (id) => (jobs.find((j) => j.id === id) || {}).title || ''
  const count = (st) => offers.filter((o) => st.includes(o.status)).length
  const accepted = count(['accepted', 'hired'])
  const declined = count(['declined'])
  const rate = accepted + declined ? Math.round((accepted / (accepted + declined)) * 100) : null
  const atOffer = candidates.filter((c) => c.stage === 'Offer' && c.status === 'active')
  const offered = new Set(offers.filter((o) => !['withdrawn', 'declined'].includes(o.status)).map((o) => o.application))
  const current = offers.find((o) => o.id === selected)
  const shown = offers.filter((o) => !filter || o.status === filter)

  if (creating) {
    return <NewOffer app={app} jobs={jobs} candidates={[...atOffer.filter((c) => !offered.has(c.id)), ...atOffer.filter((c) => offered.has(c.id))]}
      onCancel={() => setCreating(false)} onCreated={(id) => { setCreating(false); setSelected(id); app.refresh() }} />
  }
  return (
    <Stack gap={4}>
      <Grid cols={5}>
        <Stat label="Being prepared" value={count(['draft', 'pending_approval', 'approved'])} hint={`${count(['pending_approval'])} awaiting approval`} />
        <Stat label="Out with candidates" value={count(['sent'])} tone="warning" />
        <Stat label="Accepted" value={accepted} tone="success" hint={`${count(['hired'])} marked hired`} />
        <Stat label="Declined" value={declined} />
        <Stat label="Acceptance rate" value={rate == null ? '—' : `${rate}%`} hint="accepted ÷ (accepted + declined)" />
      </Grid>
      {current && <OfferPanel key={current.id} app={app} offer={current} candidate={people[current.application]} job={jobs.find((j) => j.id === current.job)}
        onChanged={() => app.refresh()} onClose={() => setSelected(null)} />}
      {selected && !current && offers.length > 0 && <Empty>That offer isn't in the list (it may have been removed).</Empty>}
      <Card title={`Offers (${shown.length})`} actions={<>
        <Select value={filter} onChange={setFilter} options={[{ value: '', label: 'All statuses' }, ...app.options('offer', 'status')]} />
        <Button tone="primary" onClick={() => setCreating(true)}>Make an offer</Button>
      </>}>
        <Table rows={shown} empty={atOffer.length ? `No offers yet — ${atOffer.length} candidate${atOffer.length === 1 ? ' is' : 's are'} at the Offer stage.` : 'No offers yet.'} columns={[
          { key: 'application', label: 'Candidate', render: (o) => (
            <button type="button" className="font-medium hover:underline" onClick={() => setSelected(o.id)}>{people[o.application] ? people[o.application].name : `#${o.application}`}</button>
          ) },
          { key: 'title', label: 'Title' },
          { key: 'job', label: 'Job', render: (o) => jobTitle(o.job) },
          { key: 'salary', label: 'Salary', align: 'right', render: salaryText },
          { key: 'start_date', label: 'Start' },
          { key: 'status', label: 'Status', render: (o) => <Badge tone={STATUS_TONE[o.status]}>{app.optionLabel('offer', 'status', o.status)}</Badge> },
          { key: 'updated_at', label: 'Updated', render: (o) => (o.updated_at || '').slice(0, 10) },
        ]} />
      </Card>
      {atOffer.filter((c) => !offered.has(c.id)).length > 0 && (
        <div className="text-sm text-muted-foreground">Waiting for an offer: {atOffer.filter((c) => !offered.has(c.id)).map((c) => c.name).join(', ')}.</div>
      )}
    </Stack>
  )
}
