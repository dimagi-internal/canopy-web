// One candidate (?id): who they are, where they stand, and everything the team
// has done with them — notes, interviews, scorecards, offers and a timeline.
// Recruiters act from the header; fields the workspace added show in Details;
// the workspace's own panels go in the `sidebar` slot. Self-ID answers are never
// shown here (they belong to the compliance role, as totals only).
function arr(x) { return Array.isArray(x) ? x : [] }
function can(app, name, role) {
  const a = ((app.dataModel && app.dataModel._actions) || {})[name]
  if (a && a.enabled === false) return false
  return app.roles.includes(a ? a.role : role)
}
function when(iso) { return iso ? String(iso).slice(0, 16).replace('T', ' ') : '' }
function tagList(tags) { return String(tags || '').split(',').map((t) => t.trim()).filter(Boolean) }
function parseRatings(text) {
  return String(text || '').split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const i = l.lastIndexOf(':')
    return i > 0 ? { attr: l.slice(0, i).trim(), rating: l.slice(i + 1).trim() } : { attr: l, rating: '' }
  })
}
const TONE = { 'Strong no': 'destructive', No: 'warning', Mixed: 'default', Yes: 'success', 'Strong yes': 'success' }
const STATUS_TONE = { active: 'default', rejected: 'destructive', hired: 'success' }
const BUILT_IN = ['job', 'name', 'email', 'phone', 'location', 'country', 'region', 'linkedin', 'resume', 'resume_url',
  'cover_letter', 'source', 'source_tag', 'referrer', 'credited_to', 'kind', 'pool', 'stage', 'status', 'stage_entered_at',
  'applied_on', 'rejected_on', 'hired_on', 'rejection_reason', 'rejection_note', 'tags', 'do_not_email', 'legal_hold',
  'privacy_notice', 'consent_pool', 'eeo_gender', 'eeo_race', 'eeo_veteran', 'eeo_disability']
// When the rejection email goes: held-back letters are cancelled if the candidate is un-rejected first.
const DELAYS = [{ value: '0', label: 'Send now' }, { value: '24', label: 'Send tomorrow (in 24 hours)' }, { value: '72', label: 'Send in 3 days' }]
const NEW_IV = { mode: 'schedule', step: '', kind: '', date: '', time: '', duration: '45', timezone: 'America/New_York',
  interviewers: '', meeting_link: '', slots: [{ date: '', time: '' }] }

function View({ app, scope }) {
  const id = Number(app.params.id)
  const me = String((scope && scope.viewer && scope.viewer.email) || '').toLowerCase()
  const recruiter = app.roles.includes('recruiter')
  const [a, setA] = useState(undefined)
  const [job, setJob] = useState(null)
  const [jobs, setJobs] = useState([])
  const [others, setOthers] = useState([])
  const [notes, setNotes] = useState([])
  const [interviews, setInterviews] = useState([])
  const [cards, setCards] = useState([])
  const [steps, setSteps] = useState([])
  const [offers, setOffers] = useState([])
  const [templates, setTemplates] = useState([])
  const [changes, setChanges] = useState([])
  const [log, setLog] = useState([])
  const [panel, setPanel] = useState(app.params.reject === '1' ? 'reject' : null)
  const [form, setForm] = useState({})
  const [iv, setIv] = useState(null)
  const [noteText, setNoteText] = useState('')
  const [notePrivate, setNotePrivate] = useState(false)
  const [linkFor, setLinkFor] = useState(null)
  const [notice, setNotice] = useState(null)
  const [err, setErr] = useState(null)

  async function load() {
    const rec = arr(await app.query('application', { where: { id } }))[0] || null
    setA(rec)
    if (!rec) return
    const quiet = (p, set) => p.then((r) => set(arr(r))).catch(() => set([]))
    if (rec.job) app.query('job', { where: { id: rec.job } }).then((r) => setJob(arr(r)[0] || null)).catch(() => setJob(null))
    if (rec.job) quiet(app.query('interview_step', { where: { job: rec.job }, order: 'position' }), setSteps)
    if (rec.email) quiet(app.query('application', { where: { email: rec.email }, limit: 20 }).then((r) => arr(r).filter((x) => x.id !== id)), setOthers)
    quiet(app.query('note', { where: { application: id }, limit: 200 }), setNotes)
    quiet(app.query('interview', { where: { application: id }, order: 'date', limit: 100 }), setInterviews)
    quiet(app.query('scorecard', { where: { application: id }, limit: 100 }), setCards)
    quiet(app.query('stage_change', { where: { application: id }, limit: 200 }), setChanges)
    quiet(app.history(id), setLog)
    if (recruiter) {
      quiet(app.query('job', { limit: 2000, order: 'title' }), setJobs)
      quiet(app.query('offer', { where: { application: id }, limit: 50 }), setOffers)
      quiet(app.query('email_template', { limit: 200 }), setTemplates)
    }
  }
  useEffect(() => { load().catch((e) => { setErr(e.message); setA(null) }) }, [id])

  async function run(fn, done) {
    setErr(null)
    try {
      const out = await fn()
      const sent = arr(out && out._effects)
      setNotice(done ? done + (sent.length ? ` · ${sent.map((s) => `email to ${s.to}: ${s.status}`).join('; ')}` : '') : null)
      setPanel(null)
      await load()
      return out || true
    } catch (e) { setErr(e.message); return null }
  }
  const act = (action, opts, done) => run(() => app.act(action, { record: id, ...opts }), done)
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))
  function open(name, initial) { setPanel(name); setForm(initial || {}); setErr(null) }
  function fromTemplate(tid) {
    const t = templates.find((x) => String(x.id) === String(tid))
    setForm((f) => ({ ...f, template: tid, subject: t ? t.subject : f.subject, body: t ? t.body : f.body }))
  }

  if (a === undefined) return <Loading />
  if (a === null) return <Empty>{err || 'No such candidate.'}</Empty>

  const stageOpts = app.options('application', 'stage').filter((o) => o.value !== 'Rejected' && o.value !== 'Hired')
  const rejectTemplates = templates.filter((t) => t.kind === 'Rejection')
  const jobTitle = Object.fromEntries([...jobs, ...(job ? [job] : [])].map((j) => [j.id, j.title]))
  const ivById = Object.fromEntries(interviews.map((x) => [x.id, x]))
  const stepById = Object.fromEntries(steps.map((s) => [s.id, s]))
  const active = a.status === 'active'
  const activeOthers = others.filter((o) => o.status === 'active' && o.kind !== 'prospect')
  const actionLabel = (k) => (((app.dataModel && app.dataModel._actions) || {})[k] || {}).label || String(k || '').replace(/_/g, ' ')

  const timeline = [
    ...changes.map((c) => ({ at: c.at || c.created_at, who: c.by, what: `Moved to ${app.optionLabel('application', 'stage', c.to_stage)}` })),
    ...log.map((e) => {
      const ch = Object.entries(e.changes || {}).filter(([k]) => !['stage', 'stage_entered_at', 'status'].includes(k) && !k.startsWith('eeo_'))
      const detail = e.op === 'update' ? ch.map(([k, v]) => `${app.fieldLabel('application', k)}: ${Array.isArray(v) ? `${v[0] ?? '—'} → ${v[1] ?? '—'}` : v}`).join('; ') : ''
      const mail = arr(e.effects).map((x) => `email to ${x.to} (${x.status})`).join('; ')
      if (e.op === 'update' && !detail && !mail && e.action !== 'email_candidate') return null
      return { at: e.at, who: e.actor, what: actionLabel(e.action), detail: [detail, mail].filter(Boolean).join(' · ') }
    }).filter(Boolean),
  ].sort((x, y) => String(y.at || '').localeCompare(String(x.at || '')))

  function pickStep(sid) {
    const s = stepById[Number(sid)]
    setIv((v) => ({ ...v, step: sid, ...(s ? { duration: String(s.duration || 45), interviewers: s.interviewers || v.interviewers } : {}) }))
  }
  async function saveInterview() {
    const s = stepById[Number(iv.step)]
    const base = { application: id, job: a.job || null, step: iv.step ? Number(iv.step) : null, kind: iv.kind || (app.options('interview', 'kind')[0] || {}).value,
      duration: Number(iv.duration) || 45, timezone: iv.timezone || null, interviewers: iv.interviewers || null, meeting_link: iv.meeting_link || null }
    const ok = iv.mode === 'schedule'
      ? await run(() => app.act('schedule_interview', { input: { ...base, date: iv.date, time: iv.time || null } }), `${s ? s.name : base.kind} scheduled`)
      : await run(() => app.act('request_availability', { input: { ...base,
        slot_options: iv.slots.filter((x) => x.date && x.time).map((x) => `${x.date} ${x.time}`).join('\n') } }), 'Times sent to the candidate')
    if (ok) setIv(null)
  }
  async function copyLink(x) {
    try {
      const out = await app.link('schedule', x.id)
      setLinkFor({ id: x.id, url: out.url, days: out.days })
      try { await navigator.clipboard.writeText(out.url) } catch (e) { /* the link stays on screen to copy by hand */ }
    } catch (e) { setErr(e.message) }
  }
  async function addNote() {
    if (!noteText.trim()) return
    const ok = notePrivate && recruiter
      ? await run(() => app.act('add_private_note', { input: { application: id, private_body: noteText.trim() } }), 'Private note added')
      : await run(() => app.act('add_note', { input: { application: id, body: noteText.trim() } }), 'Note added')
    if (ok) { setNoteText(''); setNotePrivate(false) }
  }

  const ratingTally = {}
  for (const c of cards) ratingTally[c.rating] = (ratingTally[c.rating] || 0) + 1

  return (
    <Stack gap={4}>
      <button type="button" className="text-left text-sm text-muted-foreground hover:text-foreground" onClick={() => app.go('pipeline', a.job ? { job: a.job } : {})}>← Pipeline{job ? ` · ${job.title}` : ''}</button>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">{a.name}</h2>
          <div className="text-sm text-muted-foreground">{[a.email, a.phone, a.location, a.country].filter(Boolean).join(' · ')}</div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm">
            {job && <button type="button" className="font-medium hover:underline" onClick={() => app.go('job-setup', { id: job.id })}>{job.title}</button>}
            <Badge>{app.optionLabel('application', 'stage', a.stage)}</Badge>
            <Badge tone={STATUS_TONE[a.status]}>{app.optionLabel('application', 'status', a.status)}</Badge>
            {a.kind === 'prospect' && <Badge>Prospect</Badge>}
            {a.do_not_email && <Badge tone="warning">Do not email</Badge>}
            {a.legal_hold && <Badge tone="warning">Legal hold</Badge>}
            {tagList(a.tags).map((t) => <Badge key={t}>{t}</Badge>)}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            Source: {app.optionLabel('application', 'source', a.source) || '—'}{a.source_tag ? ` (${a.source_tag})` : ''}
            {a.referrer ? ` · referred by ${a.referrer}` : ''}{a.credited_to && a.credited_to !== a.referrer ? ` · credited to ${a.credited_to}` : ''}
            {a.applied_on ? ` · applied ${a.applied_on}` : ''}{a.status === 'rejected' && a.rejection_reason ? ` · rejected: ${a.rejection_reason}` : ''}
          </div>
          <div className="mt-2 flex flex-wrap gap-2 text-sm">
            {a.resume && <Button size="sm" onClick={() => app.openFile(a.resume)}>Open résumé</Button>}
            {a.resume_url && <a className="text-primary hover:underline" href={a.resume_url}>Résumé link</a>}
            {a.linkedin && <a className="text-primary hover:underline" href={a.linkedin}>LinkedIn</a>}
          </div>
        </div>
        {recruiter && (
          <div className="flex max-w-md flex-wrap justify-end gap-2">
            {active && can(app, 'move_stage', 'recruiter') && <Button onClick={() => open('move', { stage: a.stage })}>Move stage</Button>}
            {active && can(app, 'transfer', 'recruiter') && <Button onClick={() => open('transfer', { job: '' })}>Transfer</Button>}
            {active && (can(app, 'reject', 'recruiter') || can(app, 'reject_and_email', 'recruiter')) && <Button tone="destructive" onClick={() => open('reject', { email: true, delay: '0', alsoOthers: false, subject: app.settings.rejection_subject || '', body: app.settings.rejection_body || '' })}>Reject…</Button>}
            {a.status === 'rejected' && can(app, 'unreject', 'recruiter') && <Button onClick={() => open('unreject', { stage: (stageOpts[0] || {}).value || 'Applied' })}>Un-reject</Button>}
            {can(app, 'email_candidate', 'recruiter') && <Button onClick={() => open('email', { subject: '', body: '' })}>Email</Button>}
            {can(app, 'update_candidate', 'recruiter') && <Button onClick={() => open('tags', { tags: a.tags || '' })}>Tags</Button>}
            {can(app, 'update_candidate', 'recruiter') && <Button onClick={() => act('update_candidate', { input: { do_not_email: !a.do_not_email } }, a.do_not_email ? 'Emails allowed again' : 'Marked do-not-email')}>{a.do_not_email ? 'Allow email' : 'Do not email'}</Button>}
            {can(app, 'set_hold', 'recruiter') && <Button onClick={() => act('set_hold', { input: { legal_hold: !a.legal_hold } }, a.legal_hold ? 'Legal hold lifted' : 'Legal hold set')}>{a.legal_hold ? 'Lift legal hold' : 'Legal hold'}</Button>}
          </div>
        )}
      </div>
      {notice && <div className="rounded-md border border-border bg-muted px-3 py-2 text-sm">{notice}</div>}
      {err && <div className="text-sm text-destructive">{err}</div>}

      {panel === 'move' && (
        <Card title="Move to another stage">
          <div className="flex flex-wrap items-end gap-2">
            <Select label="Stage" value={form.stage} onChange={set('stage')} options={stageOpts} />
            <Button tone="primary" disabled={form.stage === a.stage} onClick={() => act('move_stage', { input: { stage: form.stage } }, `Moved to ${app.optionLabel('application', 'stage', form.stage)}`)}>Move</Button>
            <Button onClick={() => setPanel(null)}>Cancel</Button>
          </div>
        </Card>
      )}
      {panel === 'transfer' && (
        <Card title="Move to another job">
          <div className="flex flex-wrap items-end gap-2">
            <Select label="Job" value={form.job} onChange={set('job')}
              options={[{ value: '', label: 'Choose a job…' }, ...jobs.filter((j) => j.id !== a.job && !j.is_template && j.status !== 'closed').map((j) => ({ value: String(j.id), label: j.title }))]} />
            <Button tone="primary" disabled={!form.job} onClick={() => act('transfer', { input: { job: Number(form.job) } }, 'Moved to the new job, back at the first stage')}>Transfer</Button>
            <Button onClick={() => setPanel(null)}>Cancel</Button>
          </div>
        </Card>
      )}
      {panel === 'reject' && (
        <Card title={`Reject ${a.name}`}>
          <Stack gap={3}>
            <Grid cols={2}>
              <Select label="Reason" value={form.reason || ''} onChange={set('reason')} options={[{ value: '', label: 'Choose a reason…' }, ...app.options('application', 'rejection_reason')]} />
              <TextArea label="Note (team only)" rows={2} value={form.note} onChange={set('note')} />
            </Grid>
            {can(app, 'reject_and_email', 'recruiter') && (
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.email} onChange={(e) => set('email')(e.target.checked)} /> Send a rejection email{a.do_not_email ? ' (this candidate is do-not-email: none will go)' : ''}</label>
            )}
            {form.email && can(app, 'reject_and_email', 'recruiter') && (
              <Stack gap={2}>
                <Select label="Template" value={form.template || ''} onChange={(v) => fromTemplate(v)}
                  options={[{ value: '', label: 'Standard rejection email' }, ...(rejectTemplates.length ? rejectTemplates : templates).map((t) => ({ value: String(t.id), label: t.name }))]} />
                <Input label="Subject" value={form.subject} onChange={set('subject')} />
                <TextArea label="Email" rows={7} value={form.body} onChange={set('body')} />
                <Select label="When" value={form.delay || '0'} onChange={set('delay')} options={DELAYS} />
              </Stack>
            )}
            {activeOthers.length > 0 && (
              <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={!!form.alsoOthers} onChange={(e) => set('alsoOthers')(e.target.checked)} /> Also reject from their {activeOthers.length} other active application{activeOthers.length === 1 ? '' : 's'} ({activeOthers.map((o) => jobTitle[o.job] || 'Prospect').join(', ')})</label>
            )}
            <div className="flex flex-wrap gap-2">
              <Button tone="destructive" onClick={() => {
                const input = { rejection_reason: form.reason || null, rejection_note: form.note || null }
                const emailing = form.email && can(app, 'reject_and_email', 'recruiter')
                const params = { subject: form.subject || undefined, body: form.body || undefined,
                  delay_hours: Number(form.delay) > 0 ? Number(form.delay) : undefined }
                const action = emailing ? 'reject_and_email' : 'reject'
                const ids = [id, ...(form.alsoOthers ? activeOthers.map((o) => o.id) : [])]
                if (ids.length > 1) {
                  return run(() => app.bulk(action, { records: ids, input, ...(emailing ? { params } : {}) }).then((out) => {
                    const failed = arr(out && out.failed)
                    if (failed.length) throw new Error(`${failed.length} could not be rejected: ${failed.map((f) => f.error).join('; ')}`)
                    return out
                  }), `Rejected from ${ids.length} applications`)
                }
                return emailing ? act(action, { input, params }, 'Rejected') : act(action, { input }, 'Rejected, no email sent')
              }}>{form.email ? 'Reject and send email' : 'Reject'}</Button>
              <Button onClick={() => setPanel(null)}>Cancel</Button>
            </div>
          </Stack>
        </Card>
      )}
      {panel === 'unreject' && (
        <Card title="Un-reject">
          <div className="flex flex-wrap items-end gap-2">
            <Select label="Back to stage" value={form.stage} onChange={set('stage')} options={stageOpts} />
            <Button tone="primary" onClick={() => act('unreject', { input: { stage: form.stage } }, 'Active again')}>Un-reject</Button>
            <Button onClick={() => setPanel(null)}>Cancel</Button>
          </div>
        </Card>
      )}
      {panel === 'email' && (
        <Card title={`Email ${a.name}`}>
          <Form onSubmit={() => act('email_candidate', { params: { subject: form.subject, body: form.body } }, 'Email sent')}>
            <Stack gap={2}>
              {a.do_not_email && <div className="text-sm text-warning">This candidate is marked do-not-email, so nothing will be sent.</div>}
              <Select label="Start from a template" value={form.template || ''} onChange={(v) => fromTemplate(v)}
                options={[{ value: '', label: 'Blank' }, ...templates.map((t) => ({ value: String(t.id), label: `${t.name} (${t.kind})` }))]} />
              <Input label="Subject" required value={form.subject} onChange={set('subject')} />
              <TextArea label="Email" required rows={8} value={form.body} onChange={set('body')} />
              <div className="text-xs text-muted-foreground">{'{{name}}'} and {'{{job.title}}'} are filled in when it is sent.</div>
              <div className="flex gap-2"><Button type="submit" tone="primary">Send</Button><Button onClick={() => setPanel(null)}>Cancel</Button></div>
            </Stack>
          </Form>
        </Card>
      )}
      {panel === 'tags' && (
        <Card title="Tags">
          <div className="flex flex-wrap items-end gap-2">
            <Input label="Comma-separated" value={form.tags} onChange={set('tags')} />
            <Button tone="primary" onClick={() => act('update_candidate', { input: { tags: form.tags || null } }, 'Tags saved')}>Save</Button>
            <Button onClick={() => setPanel(null)}>Cancel</Button>
          </div>
        </Card>
      )}

      <div className="grid gap-4" style={{ gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)' }}>
        <Stack gap={4}>
          <Card title="Application">
            <div className="whitespace-pre-wrap text-sm">{a.cover_letter || <span className="text-muted-foreground">No cover letter.</span>}</div>
            {a.status === 'rejected' && a.rejection_note && <div className="mt-2 text-xs text-muted-foreground">Rejection note: {a.rejection_note}</div>}
          </Card>

          <Card title={`Interviews (${interviews.length})`} actions={recruiter && can(app, 'schedule_interview', 'recruiter') && !iv &&
            <Button size="sm" onClick={() => setIv({ ...NEW_IV, kind: (app.options('interview', 'kind')[0] || {}).value || '' })}>Schedule</Button>}>
            <Stack gap={2}>
              {interviews.length === 0 && !iv && <Empty>None yet.</Empty>}
              {interviews.map((x) => (
                <div key={x.id} className="border-b border-border pb-2 text-sm last:border-0">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div>
                      <span className="font-medium">{(stepById[x.step] || {}).name || app.optionLabel('interview', 'kind', x.kind)}</span>
                      {' · '}{x.date ? `${x.date}${x.time ? ` ${x.time}` : ''}${x.timezone ? ` (${x.timezone})` : ''}` : 'no time yet'}
                      {x.duration ? ` · ${x.duration} min` : ''}
                      <div className="text-xs text-muted-foreground">{x.interviewers || 'No interviewers yet'}{x.meeting_link && <> · <a className="text-primary" href={x.meeting_link}>meeting link</a></>}</div>
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      <Badge tone={x.status === 'Done' ? 'success' : x.status === 'Cancelled' ? 'destructive' : x.status === 'Requested' ? 'warning' : 'default'}>{x.status === 'Requested' ? 'Waiting for a pick' : x.status}</Badge>
                      <Button size="sm" onClick={() => app.go('kit', { id: x.id })}>Kit</Button>
                      {x.status === 'Requested' && recruiter && <Button size="sm" onClick={() => copyLink(x)}>Copy pick-a-time link</Button>}
                      {x.status === 'Scheduled' && can(app, 'complete_interview', 'interviewer') && <Button size="sm" onClick={() => run(() => app.act('complete_interview', { record: x.id }), 'Marked done')}>Done</Button>}
                      {['Requested', 'Scheduled'].includes(x.status) && can(app, 'cancel_interview', 'recruiter') && <Button size="sm" tone="destructive" onClick={() => run(() => app.act('cancel_interview', { record: x.id }), 'Interview cancelled')}>Cancel</Button>}
                    </div>
                  </div>
                  {x.status === 'Requested' && x.slot_options && <div className="mt-1 text-xs text-muted-foreground">Offered: {x.slot_options.split('\n').filter(Boolean).join(' · ')}</div>}
                  {linkFor && linkFor.id === x.id && (
                    <div className="mt-1"><Input value={linkFor.url} onChange={() => {}} /><div className="text-xs text-muted-foreground">Copied if your browser allowed it. Works for {linkFor.days} days.</div></div>
                  )}
                </div>
              ))}
              {iv && (
                <Form onSubmit={saveInterview}>
                  <Stack gap={3}>
                    <div className="flex gap-2">
                      <Button tone={iv.mode === 'schedule' ? 'primary' : 'default'} size="sm" onClick={() => setIv({ ...iv, mode: 'schedule' })}>Pick the time</Button>
                      {can(app, 'request_availability', 'recruiter') && <Button tone={iv.mode === 'request' ? 'primary' : 'default'} size="sm" onClick={() => setIv({ ...iv, mode: 'request' })}>Let the candidate choose</Button>}
                    </div>
                    <Grid cols={2}>
                      {steps.length > 0 && <Select label="From the interview plan" value={iv.step} onChange={pickStep}
                        options={[{ value: '', label: 'Not from the plan' }, ...steps.map((s) => ({ value: String(s.id), label: `${s.name} (${s.duration || 45} min)` }))]} />}
                      <Select label="Kind" value={iv.kind} onChange={(kind) => setIv({ ...iv, kind })} options={app.options('interview', 'kind')} />
                      {iv.mode === 'schedule' && <Input label="Date" type="date" required value={iv.date} onChange={(date) => setIv({ ...iv, date })} />}
                      {iv.mode === 'schedule' && <Input label="Time (HH:MM)" required value={iv.time} onChange={(time) => setIv({ ...iv, time })} placeholder="14:00" />}
                      <Input label="Minutes" type="number" value={iv.duration} onChange={(duration) => setIv({ ...iv, duration })} />
                      <Input label="Time zone" value={iv.timezone} onChange={(timezone) => setIv({ ...iv, timezone })} />
                      <Input label="Interviewers (emails, comma-separated)" value={iv.interviewers} onChange={(interviewers) => setIv({ ...iv, interviewers })} />
                      <Input label="Meeting link" type="url" value={iv.meeting_link} onChange={(meeting_link) => setIv({ ...iv, meeting_link })} />
                    </Grid>
                    {iv.mode === 'request' && (
                      <Stack gap={2}>
                        <div className="text-sm font-medium">Times the candidate may choose (up to 5)</div>
                        {iv.slots.map((s, i) => (
                          <div key={i} className="flex flex-wrap items-end gap-2">
                            <Input label={`Option ${i + 1}: date`} type="date" value={s.date} onChange={(date) => setIv({ ...iv, slots: iv.slots.map((x, j) => (j === i ? { ...x, date } : x)) })} />
                            <Input label={`Option ${i + 1}: time`} value={s.time} placeholder="10:00" onChange={(time) => setIv({ ...iv, slots: iv.slots.map((x, j) => (j === i ? { ...x, time } : x)) })} />
                            {iv.slots.length > 1 && <Button size="sm" onClick={() => setIv({ ...iv, slots: iv.slots.filter((_, j) => j !== i) })}>Remove</Button>}
                          </div>
                        ))}
                        {iv.slots.length < 5 && <div><Button size="sm" onClick={() => setIv({ ...iv, slots: [...iv.slots, { date: '', time: '' }] })}>Add a time</Button></div>}
                      </Stack>
                    )}
                    <div className="flex gap-2">
                      <Button type="submit" tone="primary">{iv.mode === 'schedule' ? 'Schedule and send invites' : 'Send times to the candidate'}</Button>
                      <Button onClick={() => setIv(null)}>Cancel</Button>
                    </div>
                  </Stack>
                </Form>
              )}
            </Stack>
          </Card>

          <Card title={`Notes (${notes.filter((n) => n.body || n.private_body).length})`}>
            <Stack gap={3}>
              {notes.filter((n) => n.body || n.private_body).sort((x, y) => String(y.created_at).localeCompare(String(x.created_at))).map((n) => (
                <div key={n.id} className="border-b border-border pb-2 text-sm last:border-0">
                  <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
                    <span>{n.author || 'Someone'} · {when(n.created_at)}{n.private_body ? ' · private' : ''}</span>
                    {(recruiter || String(n.author || '').toLowerCase() === me) && can(app, 'delete_note', 'interviewer') &&
                      <button type="button" className="hover:text-destructive" onClick={() => run(() => app.act('delete_note', { record: n.id }), 'Note deleted')}>Delete</button>}
                  </div>
                  <div className="mt-1 whitespace-pre-wrap">{n.body || n.private_body || ''}</div>
                </div>
              ))}
              {can(app, 'add_note', 'interviewer') && (
                <Stack gap={2}>
                  <TextArea rows={3} value={noteText} onChange={setNoteText} placeholder="Add a note for the hiring team" />
                  <div className="flex items-center gap-3">
                    <Button size="sm" disabled={!noteText.trim()} onClick={addNote}>Add note</Button>
                    {recruiter && can(app, 'add_private_note', 'recruiter') && <label className="flex items-center gap-1 text-xs text-muted-foreground"><input type="checkbox" checked={notePrivate} onChange={(e) => setNotePrivate(e.target.checked)} /> Private (recruiters only)</label>}
                  </div>
                </Stack>
              )}
            </Stack>
          </Card>

          <Card title="Activity">
            <Table rows={timeline} empty="Nothing yet." columns={[
              { key: 'at', label: 'When', render: (e) => when(e.at) },
              { key: 'who', label: 'Who' },
              { key: 'what', label: 'What', render: (e) => <span>{e.what}{e.detail ? <span className="text-muted-foreground"> — {e.detail}</span> : null}</span> },
            ]} />
          </Card>
        </Stack>

        <Stack gap={4}>
          <Card title={`Scorecards (${cards.length})`}>
            <Stack gap={3}>
              {cards.length > 0 && (
                <div className="flex flex-wrap gap-1">{Object.entries(ratingTally).map(([r, n]) => <Badge key={r} tone={TONE[r]}>{r} × {n}</Badge>)}</div>
              )}
              {cards.length === 0 && <Empty>No scorecards yet.</Empty>}
              {cards.map((c) => {
                const x = ivById[c.interview]
                return (
                  <div key={c.id} className="border-b border-border pb-2 text-sm last:border-0">
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium">{c.interviewer}</span><Badge tone={TONE[c.rating]}>{c.rating}</Badge>
                    </div>
                    {x && <button type="button" className="text-xs text-primary hover:underline" onClick={() => app.go('kit', { id: x.id })}>{(stepById[x.step] || {}).name || x.kind}{x.date ? ` · ${x.date}` : ''} — open kit</button>}
                    {parseRatings(c.attribute_ratings).length > 0 && (
                      <div className="mt-1 grid gap-x-2 text-xs" style={{ gridTemplateColumns: '1fr max-content' }}>
                        {parseRatings(c.attribute_ratings).map((r, i) => (
                          <div key={i} className="contents"><span className="text-muted-foreground">{r.attr}</span><span className={r.rating.includes('no') || r.rating === 'No' ? 'text-destructive' : r.rating.includes('es') ? 'text-success' : ''}>{r.rating}</span></div>
                        ))}
                      </div>
                    )}
                    {c.key_takeaways && <div className="mt-1 whitespace-pre-wrap">{c.key_takeaways}</div>}
                    {c.private_notes && <div className="mt-1 whitespace-pre-wrap text-xs text-muted-foreground">Private: {c.private_notes}</div>}
                  </div>
                )
              })}
            </Stack>
          </Card>

          {recruiter && (
            <Card title={`Offers (${offers.length})`} actions={<Button size="sm" onClick={() => app.go('offers', { application: id })}>Offers page</Button>}>
              {offers.length === 0 ? <Empty>No offer yet.</Empty> : (
                <Stack gap={2}>
                  {offers.map((o) => (
                    <div key={o.id} className="flex items-center justify-between gap-2 text-sm">
                      <span>{o.title}{o.salary ? ` · ${o.currency || ''} ${Number(o.salary).toLocaleString()}/${o.pay_period || 'year'}` : ''}{o.start_date ? ` · starts ${o.start_date}` : ''}</span>
                      <Badge tone={['accepted', 'hired'].includes(o.status) ? 'success' : ['declined', 'withdrawn'].includes(o.status) ? 'destructive' : 'default'}>{app.optionLabel('offer', 'status', o.status)}</Badge>
                    </div>
                  ))}
                </Stack>
              )}
            </Card>
          )}

          {others.length > 0 && (
            <Card title="Other applications (same email)">
              <Stack gap={1}>
                {others.map((o) => (
                  <button key={o.id} type="button" className="flex items-center justify-between gap-2 text-left text-sm hover:underline" onClick={() => app.go('candidate', { id: o.id })}>
                    <span className="truncate">{jobTitle[o.job] || (o.kind === 'prospect' ? 'Prospect' : `Job #${o.job || '—'}`)}</span>
                    <Badge tone={STATUS_TONE[o.status]}>{app.optionLabel('application', 'stage', o.stage)}</Badge>
                  </button>
                ))}
              </Stack>
            </Card>
          )}

          <Card title="Details">
            <dl className="grid gap-x-4 gap-y-1 text-sm" style={{ gridTemplateColumns: 'max-content 1fr' }}>
              {[['Privacy region', a.region], ['Applied on', a.applied_on], ['In stage since', when(a.stage_entered_at)],
                ['Privacy notice seen', a.privacy_notice], ['Stays in talent pool', a.consent_pool ? 'Yes' : null],
                ['Rejected on', a.rejected_on], ['Hired on', a.hired_on]].filter(([, v]) => v).map(([k, v]) => (
                <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd>{v}</dd></div>
              ))}
            </dl>
            <FieldValues collection="application" record={a} exclude={BUILT_IN} />
          </Card>

          <Slot name="sidebar" record={a} job={job} reload={load} app={app} />
        </Stack>
      </div>
    </Stack>
  )
}
