// The interview kit (?id = interview): everything an interviewer needs before and
// during the conversation — how to prepare, what to ask, what to look for, the
// candidate's application and what earlier interviewers want them to know — and
// the scorecard. Submitting again edits the scorecard already sent.
function arr(x) { return Array.isArray(x) ? x : [] }
function lines(text) { return String(text || '').split('\n').map((l) => l.trim()).filter(Boolean) }
function can(app, name, role) {
  const a = ((app.dataModel && app.dataModel._actions) || {})[name]
  if (a && a.enabled === false) return false
  return app.roles.includes(a ? a.role : role)
}
function parseRatings(text) {
  const out = {}
  for (const l of lines(text)) {
    const i = l.lastIndexOf(':')
    if (i > 0) out[l.slice(0, i).trim()] = l.slice(i + 1).trim()
  }
  return out
}
const SCALE = ['Strong no', 'No', 'Mixed', 'Yes', 'Strong yes']
const TONE = { 'Strong no': 'destructive', No: 'warning', Mixed: 'default', Yes: 'success', 'Strong yes': 'success' }
const EMPTY = { rating: '', ratings: {}, key_takeaways: '', notes: '', note_for_others: '', private_notes: '' }

function Choice({ label, value, options, onChange }) {
  return (
    <div className="flex flex-wrap gap-1" role="radiogroup" aria-label={label}>
      {options.map((o) => (
        <button key={o} type="button" role="radio" aria-checked={value === o} onClick={() => onChange(value === o ? '' : o)}
          className={`rounded-md border px-2 py-0.5 text-xs font-medium ${value === o ? 'border-primary bg-primary text-primary-foreground' : 'border-border bg-card text-foreground hover:bg-muted'}`}>
          {o}
        </button>
      ))}
    </div>
  )
}

function View({ app, scope }) {
  const id = Number(app.params.id)
  const me = String((scope && scope.viewer && scope.viewer.email) || '').toLowerCase()
  const recruiter = app.roles.includes('recruiter')
  const [iv, setIv] = useState(undefined)
  const [cand, setCand] = useState(null)
  const [job, setJob] = useState(null)
  const [step, setStep] = useState(null)
  const [cards, setCards] = useState([])
  const [tab, setTab] = useState('prep')
  const [f, setF] = useState(EMPTY)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState(null)
  const [err, setErr] = useState(null)

  async function load() {
    const x = arr(await app.query('interview', { where: { id } }))[0] || null
    setIv(x)
    if (!x) return
    const [c, j, s, sc] = await Promise.all([
      app.query('application', { where: { id: x.application } }).catch(() => []),
      x.job ? app.query('job', { where: { id: x.job } }).catch(() => []) : Promise.resolve([]),
      x.step ? app.query('interview_step', { where: { id: x.step } }).catch(() => []) : Promise.resolve([]),
      app.query('scorecard', { where: { application: x.application }, limit: 100 }).catch(() => []),
    ])
    const candidate = arr(c)[0] || null
    setCand(candidate)
    let theJob = arr(j)[0] || null
    if (!theJob && candidate && candidate.job) theJob = arr(await app.query('job', { where: { id: candidate.job } }).catch(() => []))[0] || null
    setJob(theJob)
    let theStep = arr(s)[0] || null
    if (!theStep && theJob) {
      // Scheduled without naming a plan step: use the job's step that names this
      // interview's kind, else the one for the candidate's current stage.
      const plan = arr(await app.query('interview_step', { where: { job: theJob.id }, order: 'position' }).catch(() => []))
      const kind = String(x.kind || '').toLowerCase()
      theStep = plan.find((p) => kind && String(p.name || '').toLowerCase().includes(kind))
        || plan.find((p) => candidate && p.stage === candidate.stage) || null
    }
    setStep(theStep)
    setCards(arr(sc))
    const mine = arr(sc).find((k) => k.interview === id && String(k.interviewer || '').toLowerCase() === me)
    if (mine) {
      setF({ rating: mine.rating || '', ratings: parseRatings(mine.attribute_ratings), key_takeaways: mine.key_takeaways || '',
        notes: mine.notes || '', note_for_others: mine.note_for_others || '', private_notes: mine.private_notes || '' })
    }
  }
  useEffect(() => { load().catch((e) => { setErr(e.message); setIv(null) }) }, [id])

  if (iv === undefined) return <Loading />
  if (iv === null) return <Empty>{err || 'No such interview.'}</Empty>

  const mine = cards.find((k) => k.interview === id && String(k.interviewer || '').toLowerCase() === me) || null
  const earlier = cards.filter((k) => String(k.interviewer || '').toLowerCase() !== me && (k.note_for_others || k.key_takeaways))
  const attributes = lines(step && step.attributes)
  const extraAttrs = Object.keys(f.ratings).filter((k) => !attributes.includes(k))
  const allAttrs = [...attributes, ...extraAttrs]
  const ratingOpts = app.options('scorecard', 'rating').map((o) => o.value)
  const overallOpts = ratingOpts.length ? ratingOpts : ['Strong no', 'No', 'Yes', 'Strong yes', 'No decision']
  const interviewers = String(iv.interviewers || '').split(/[,;\s]+/).filter(Boolean)
  const onPanel = interviewers.map((e) => e.toLowerCase()).includes(me)
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v }))

  async function submit() {
    if (!f.rating) { setErr('Choose an overall recommendation.'); return }
    setSaving(true)
    setErr(null)
    const input = {
      rating: f.rating,
      attribute_ratings: allAttrs.filter((a) => f.ratings[a]).map((a) => `${a}: ${f.ratings[a]}`).join('\n') || null,
      key_takeaways: f.key_takeaways || null, notes: f.notes || null, note_for_others: f.note_for_others || null,
    }
    if (recruiter) input.private_notes = f.private_notes || null
    try {
      if (mine) await app.act('update_scorecard', { record: mine.id, input })
      else await app.act('add_scorecard', { input: { ...input, application: iv.application, interview: id } })
      setNotice(mine ? 'Scorecard updated.' : 'Scorecard submitted. Thank you.')
      await load()
    } catch (e) {
      setErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  const TABS = [['prep', 'Interview prep'], ['job', 'Job details'], ['application', 'Application']]
  return (
    <Stack gap={4}>
      <button type="button" className="text-left text-sm text-muted-foreground hover:text-foreground" onClick={() => app.go('interviews')}>← My interviews</button>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">{step ? step.name : app.optionLabel('interview', 'kind', iv.kind)}{cand ? ` with ${cand.name}` : ''}</h2>
          <div className="text-sm text-muted-foreground">
            {[job && job.title, iv.date ? `${iv.date}${iv.time ? ` ${iv.time}` : ''}${iv.timezone ? ` (${iv.timezone})` : ''}` : 'Time not set yet',
              iv.duration ? `${iv.duration} min` : null].filter(Boolean).join(' · ')}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">Interviewers: {interviewers.join(', ') || '—'}</div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={iv.status === 'Done' ? 'success' : iv.status === 'Cancelled' ? 'destructive' : 'default'}>{iv.status}</Badge>
          {iv.meeting_link && <a className="text-sm text-primary hover:underline" href={iv.meeting_link}>Join the meeting</a>}
          {cand && <Button size="sm" onClick={() => app.go('candidate', { id: cand.id })}>Candidate profile</Button>}
        </div>
      </div>
      {iv.status === 'Cancelled' && <div className="rounded-md border border-border bg-muted px-3 py-2 text-sm">This interview was cancelled.</div>}

      <div className="grid gap-4" style={{ gridTemplateColumns: 'minmax(0, 3fr) minmax(0, 2fr)' }}>
        <Stack gap={4}>
          <div className="flex gap-1 border-b border-border">
            {TABS.map(([k, label]) => (
              <button key={k} type="button" onClick={() => setTab(k)}
                className={`-mb-px border-b-2 px-3 py-1.5 text-sm ${tab === k ? 'border-primary font-medium text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>{label}</button>
            ))}
          </div>
          {tab === 'prep' && (
            <Stack gap={4}>
              <Card title="How to prepare">
                <div className="whitespace-pre-wrap text-sm">{(step && step.prep) || <span className="text-muted-foreground">No preparation notes for this interview.</span>}</div>
              </Card>
              <Card title="Questions to ask">
                {lines(step && step.questions).length ? (
                  <ol className="list-decimal space-y-1 pl-5 text-sm">{lines(step.questions).map((q, i) => <li key={i}>{q}</li>)}</ol>
                ) : <Empty>No questions in the plan.</Empty>}
              </Card>
              <Card title="What to look for">
                {attributes.length ? <div className="flex flex-wrap gap-1">{attributes.map((a) => <Badge key={a}>{a}</Badge>)}</div> : <Empty>No attributes in the plan.</Empty>}
              </Card>
              <Card title={`From earlier interviewers (${earlier.length})`}>
                {earlier.length === 0 ? <Empty>Nothing yet.</Empty> : (
                  <Stack gap={3}>
                    {earlier.map((k) => (
                      <div key={k.id} className="border-b border-border pb-2 text-sm last:border-0">
                        <div className="text-xs font-medium text-muted-foreground">{k.interviewer}</div>
                        {k.note_for_others && <div className="mt-1 whitespace-pre-wrap"><span className="text-muted-foreground">For you: </span>{k.note_for_others}</div>}
                        {k.key_takeaways && <div className="mt-1 whitespace-pre-wrap"><span className="text-muted-foreground">Take-aways: </span>{k.key_takeaways}</div>}
                      </div>
                    ))}
                  </Stack>
                )}
              </Card>
            </Stack>
          )}
          {tab === 'job' && (
            <Card title={job ? job.title : 'Job'}>
              {!job ? <Empty>No job on this interview.</Empty> : (
                <Stack gap={2}>
                  <div className="text-sm text-muted-foreground">{[job.department, job.location, job.workplace, job.employment_type].filter(Boolean).join(' · ')}</div>
                  {job.hiring_manager && <div className="text-sm">Hiring manager: {job.hiring_manager}</div>}
                  <div className="whitespace-pre-wrap text-sm">{job.description}</div>
                  <FieldValues collection="job" record={job} />
                </Stack>
              )}
            </Card>
          )}
          {tab === 'application' && (
            <Card title={cand ? cand.name : 'Candidate'}>
              {!cand ? <Empty>The candidate's record is not available to you.</Empty> : (
                <Stack gap={3}>
                  <div className="text-sm text-muted-foreground">{[cand.location, cand.country].filter(Boolean).join(' · ')}</div>
                  <div className="flex flex-wrap gap-2">
                    {cand.resume && <Button size="sm" tone="primary" onClick={() => app.openFile(cand.resume)}>Open résumé</Button>}
                    {cand.resume_url && <a className="text-sm text-primary hover:underline" href={cand.resume_url}>Résumé link</a>}
                    {cand.linkedin && <a className="text-sm text-primary hover:underline" href={cand.linkedin}>LinkedIn</a>}
                  </div>
                  <div>
                    <div className="text-xs font-medium text-muted-foreground">Cover letter</div>
                    <div className="mt-1 whitespace-pre-wrap text-sm">{cand.cover_letter || <span className="text-muted-foreground">None.</span>}</div>
                  </div>
                  <FieldValues collection="application" record={cand} />
                </Stack>
              )}
            </Card>
          )}
          <Slot name="kit_extra" record={iv} candidate={cand} job={job} step={step} app={app} />
        </Stack>

        <Card title={mine ? 'Your scorecard (submitted — edit below)' : 'Your scorecard'}>
          {!(mine ? can(app, 'update_scorecard', 'interviewer') : can(app, 'add_scorecard', 'interviewer')) ? (
            <Empty>You can't submit a scorecard here.</Empty>
          ) : (
            <Form onSubmit={submit}>
              <Stack gap={3}>
                {!onPanel && !mine && interviewers.length > 0 && <div className="text-xs text-warning">You aren't listed as an interviewer for this interview.</div>}
                <TextArea label="Key take-aways" rows={4} value={f.key_takeaways} onChange={set('key_takeaways')} />
                {allAttrs.length > 0 && (
                  <div>
                    <div className="mb-1 text-sm font-medium">Rate each attribute</div>
                    <Stack gap={2}>
                      {allAttrs.map((a) => (
                        <div key={a} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                          <span>{a}</span>
                          <Choice label={a} value={f.ratings[a] || ''} options={SCALE} onChange={(v) => setF((x) => ({ ...x, ratings: { ...x.ratings, [a]: v } }))} />
                        </div>
                      ))}
                    </Stack>
                  </div>
                )}
                <div>
                  <div className="mb-1 text-sm font-medium">Overall recommendation</div>
                  <Choice label="Overall recommendation" value={f.rating} options={overallOpts} onChange={set('rating')} />
                </div>
                <TextArea label="Notes" rows={5} value={f.notes} onChange={set('notes')} />
                <TextArea label="Note for other interviewers" rows={2} value={f.note_for_others} onChange={set('note_for_others')} />
                {recruiter && <TextArea label="Private note (recruiters only)" rows={2} value={f.private_notes} onChange={set('private_notes')} />}
                {err && <div className="text-sm text-destructive">{err}</div>}
                {notice && <div className="text-sm text-success">{notice}</div>}
                <div><Button type="submit" tone="primary" disabled={saving}>{mine ? 'Save changes' : 'Submit scorecard'}</Button></div>
                {f.rating && <div className="text-xs text-muted-foreground">Recommendation: <Badge tone={TONE[f.rating]}>{f.rating}</Badge></div>}
              </Stack>
            </Form>
          )}
        </Card>
      </div>
    </Stack>
  )
}
