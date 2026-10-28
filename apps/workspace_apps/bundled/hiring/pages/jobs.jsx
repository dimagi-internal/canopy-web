// Jobs: every requisition grouped by where it stands — Open, Awaiting approval,
// Draft, Closed — and the template jobs new ones are copied from. Recruiters
// create a job here, blank or as a copy (a copy brings the source's interview
// plan with it), then finish setting it up on the job's own page.
const GROUPS = [
  { key: 'open', label: 'Open', tone: 'success' },
  { key: 'pending_approval', label: 'Awaiting approval', tone: 'warning' },
  { key: 'draft', label: 'Draft', tone: 'default' },
  { key: 'closed', label: 'Closed', tone: 'default' },
]
// What a copy carries over from its source (title and description are asked for).
const COPIED = ['department', 'office', 'location', 'country', 'employment_type', 'workplace', 'requisition_id', 'openings',
  'confidential', 'pay_min', 'pay_max', 'pay_currency', 'pay_period', 'hiring_manager', 'recruiter', 'coordinator',
  'hiring_team', 'valid_through']
// Blank on a template means "fill this in for each job" — a copy must answer them.
const ASK_IF_BLANK = ['department', 'office', 'location', 'requisition_id', 'hiring_manager']
const STEP_FIELDS = ['position', 'name', 'stage', 'duration', 'interviewers', 'prep', 'questions', 'attributes']
const FALLBACK_CREATE = ['title', 'department', 'office', 'location', 'country', 'employment_type', 'workplace', 'description',
  'requisition_id', 'openings', 'confidential', 'is_template', 'copied_from', 'pay_min', 'pay_max', 'pay_currency', 'pay_period',
  'hiring_manager', 'recruiter', 'coordinator', 'hiring_team', 'valid_through']

function inputsOf(app, action, fallback) {
  const a = (app.dataModel && app.dataModel._actions) || {}
  return (a[action] && a[action].input) || fallback
}

function today() { return new Date().toISOString().slice(0, 10) }

function daysBetween(from, to) {
  if (!from) return null
  const ms = new Date(to || today()).getTime() - new Date(from).getTime()
  return Number.isFinite(ms) ? Math.max(0, Math.round(ms / 86400000)) : null
}

function blank(v) { return v == null || v === '' }

function CreateJob({ app, jobs, onCancel }) {
  const templates = jobs.filter((j) => j.is_template)
  const others = jobs.filter((j) => !j.is_template)
  const [mode, setMode] = useState(templates.length ? 'copy' : 'blank')
  const [sourceId, setSourceId] = useState(templates[0] ? String(templates[0].id) : '')
  const [f, setF] = useState({ title: '', description: '', is_template: false })
  const [busy, setBusy] = useState(null)
  const [err, setErr] = useState(null)
  const source = mode === 'copy' ? jobs.find((j) => String(j.id) === sourceId) : null
  const set = (k) => (v) => setF((x) => ({ ...x, [k]: v }))
  const mustAsk = source ? ASK_IF_BLANK.filter((k) => source.is_template && blank(source[k])) : []

  async function create() {
    setErr(null)
    const allowed = new Set(inputsOf(app, 'create_job', FALLBACK_CREATE))
    const input = {}
    if (source) for (const k of COPIED) if (!blank(source[k])) input[k] = source[k]
    for (const [k, v] of Object.entries(f)) if (!blank(v)) input[k] = v
    if (source) {
      input.copied_from = source.id
      if (blank(input.description)) input.description = source.description
    }
    for (const k of Object.keys(input)) if (!allowed.has(k)) delete input[k]
    try {
      setBusy('Creating the job…')
      const job = await app.act('create_job', { input })
      if (source) {
        setBusy('Copying the interview plan…')
        const steps = await app.query('interview_step', { where: { job: source.id }, order: 'position', limit: 100 })
        for (const s of steps) {
          const step = { job: job.id }
          for (const k of STEP_FIELDS) if (!blank(s[k])) step[k] = s[k]
          await app.act('add_step', { input: step })
        }
      }
      app.go('job-setup', { id: job.id })
    } catch (x) {
      setErr(x.message)
      setBusy(null)
    }
  }

  const sourceOptions = [
    ...templates.map((j) => ({ value: String(j.id), label: `Template · ${j.title}` })),
    ...others.map((j) => ({ value: String(j.id), label: `${j.title} (${app.optionLabel('job', 'status', j.status)})` })),
  ]
  return (
    <Card title="Create a job" actions={<Button size="sm" onClick={onCancel}>Cancel</Button>}>
      <Form onSubmit={create}>
        <Stack gap={3}>
          <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="How to start">
            <Button tone={mode === 'copy' ? 'primary' : 'default'} onClick={() => setMode('copy')}>Copy a template or another job</Button>
            <Button tone={mode === 'blank' ? 'primary' : 'default'} onClick={() => setMode('blank')}>Start blank</Button>
          </div>
          {mode === 'copy' && (
            sourceOptions.length ? (
              <>
                <Select label="Copy from" value={sourceId} onChange={setSourceId} options={sourceOptions} />
                <div className="text-xs text-muted-foreground">
                  The copy takes the source's details, pay range, hiring team and interview plan. It starts as a draft.
                </div>
              </>
            ) : <Empty>No jobs to copy yet — start blank, or mark a job as a template on its setup page.</Empty>
          )}
          <Input label="Job title" required value={f.title} onChange={set('title')} placeholder={source ? source.title : ''} />
          {mustAsk.length > 0 && (
            <>
              <div className="text-xs text-muted-foreground">The template leaves these for each job to fill in:</div>
              <Grid cols={2}>
                {mustAsk.map((k) => (
                  <Input key={k} required label={app.fieldLabel('job', k)} type={k === 'hiring_manager' ? 'email' : 'text'}
                    value={f[k]} onChange={set(k)} />
                ))}
              </Grid>
            </>
          )}
          {mode === 'blank' && (
            <>
              <Grid cols={2}>
                <Input label={app.fieldLabel('job', 'department')} value={f.department} onChange={set('department')} />
                <Input label={app.fieldLabel('job', 'office')} value={f.office} onChange={set('office')} />
                <Input label={app.fieldLabel('job', 'location')} value={f.location} onChange={set('location')} />
                <Input label={app.fieldLabel('job', 'hiring_manager')} type="email" value={f.hiring_manager} onChange={set('hiring_manager')} />
              </Grid>
              <TextArea label="Description" required rows={8} value={f.description} onChange={set('description')} />
              <FieldInputs collection="job" action="create_job" values={f} onChange={setF} />
              <label className="flex items-center gap-2 text-sm">
                <input type="checkbox" checked={f.is_template === true} onChange={(e) => set('is_template')(e.target.checked)} />
                This is a template other jobs are copied from (it never opens)
              </label>
            </>
          )}
          {err && <div className="text-sm text-destructive">{err}</div>}
          <div className="flex items-center gap-2">
            <Button type="submit" tone="primary" disabled={!!busy || (mode === 'copy' && !source)}>{busy || 'Create and set it up'}</Button>
            <span className="text-xs text-muted-foreground">You'll add openings, approvers and the post next.</span>
          </div>
        </Stack>
      </Form>
    </Card>
  )
}

function View({ data, app }) {
  const jobs = Array.isArray(data.jobs) ? data.jobs : []
  const recruiter = app.roles.includes('recruiter')
  const canSeeCandidates = app.roles.includes('interviewer') || recruiter
  const [creating, setCreating] = useState(false)
  const [dept, setDept] = useState('')
  const [office, setOffice] = useState('')
  const [q, setQ] = useState('')
  const [counts, setCounts] = useState({})
  const [openSeats, setOpenSeats] = useState(null)

  useEffect(() => {
    if (!canSeeCandidates) return
    app.query('application', { where: { kind: 'applicant', status: 'active' }, limit: 2000 }).then((rows) => {
      const c = {}
      for (const a of rows) c[a.job] = (c[a.job] || 0) + 1
      setCounts(c)
    }, () => {})
    app.query('opening', { where: { status: 'open' }, limit: 2000 }).then((rows) => {
      const c = {}
      for (const o of rows) c[o.job] = (c[o.job] || 0) + 1
      setOpenSeats(c)
    }, () => {})
  }, [jobs.length])

  const distinct = (k) => [...new Set(jobs.map((j) => j[k]).filter(Boolean))].sort()
  const shown = jobs.filter((j) => (!dept || j.department === dept) && (!office || j.office === office)
    && (!q || `${j.title} ${j.requisition_id || ''} ${j.location || ''}`.toLowerCase().includes(q.toLowerCase())))
  const live = shown.filter((j) => !j.is_template)
  const templates = shown.filter((j) => j.is_template)
  const open = (j) => app.go('job-setup', { id: j.id })

  if (creating && recruiter) return <CreateJob app={app} jobs={jobs} onCancel={() => setCreating(false)} />

  const columns = (group) => [
    { key: 'title', label: 'Job', render: (j) => (
      <div>
        <button type="button" className="font-medium hover:underline focus-visible:underline" onClick={() => open(j)}>{j.title}</button>
        <div className="text-xs text-muted-foreground">
          {[j.requisition_id, j.location, j.workplace].filter(Boolean).join(' · ')}
          {j.confidential && <> · <Badge tone="warning">Confidential</Badge></>}
          {j.status === 'open' && j.post_status === 'offline' && <> · <Badge>Post offline</Badge></>}
        </div>
      </div>
    ) },
    { key: 'department', label: app.fieldLabel('job', 'department') },
    { key: 'office', label: app.fieldLabel('job', 'office') },
    { key: 'hiring_manager', label: 'Hiring manager' },
    { key: 'openings', label: 'Openings', align: 'right', render: (j) => (
      openSeats ? `${openSeats[j.id] || 0} of ${j.openings || 1} open` : (j.openings || 1)
    ) },
    ...(canSeeCandidates ? [{ key: 'active', label: 'Active candidates', align: 'right', render: (j) => (
      counts[j.id] ? <button type="button" className="hover:underline" onClick={() => app.go('pipeline', { job: j.id })}>{counts[j.id]}</button> : 0
    ) }] : []),
    ...(group === 'open' || group === 'closed' ? [{ key: 'opened_on', label: group === 'open' ? 'Days open' : 'Was open', align: 'right',
      render: (j) => { const d = daysBetween(j.opened_on, group === 'closed' ? j.closed_on : null); return d == null ? '—' : `${d} d` } }] : []),
    ...(group === 'closed' ? [{ key: 'close_reason', label: 'Reason' }] : []),
  ]

  return (
    <Stack gap={4}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <Input label="Search" value={q} onChange={setQ} placeholder="Title, requisition ID, location" />
          <Select label={app.fieldLabel('job', 'department')} value={dept} onChange={setDept}
            options={[{ value: '', label: 'All' }, ...distinct('department')]} />
          <Select label={app.fieldLabel('job', 'office')} value={office} onChange={setOffice}
            options={[{ value: '', label: 'All' }, ...distinct('office')]} />
        </div>
        {recruiter && <Button tone="primary" onClick={() => setCreating(true)}>Create a job</Button>}
      </div>
      <Grid cols={4}>
        {GROUPS.map((g) => <Stat key={g.key} label={g.label} value={live.filter((j) => j.status === g.key).length} tone={g.tone} />)}
      </Grid>
      {jobs.length === 0 && <Empty>No jobs yet.{recruiter ? ' Create the first one.' : ''}</Empty>}
      {GROUPS.map((g) => {
        const rows = live.filter((j) => j.status === g.key)
        if (rows.length === 0 && g.key !== 'open') return null
        return (
          <Card key={g.key} title={`${g.label} (${rows.length})`}>
            <Table rows={rows} columns={columns(g.key)} empty={`No ${g.label.toLowerCase()} jobs.`} />
          </Card>
        )
      })}
      {templates.length > 0 && (
        <Card title={`Templates (${templates.length})`}>
          <div className="mb-2 text-xs text-muted-foreground">Templates never open. Copy one with “Create a job” to start a new requisition with its plan.</div>
          <Table rows={templates} columns={[
            { key: 'title', label: 'Template', render: (j) => <button type="button" className="font-medium hover:underline" onClick={() => open(j)}>{j.title}</button> },
            { key: 'department', label: app.fieldLabel('job', 'department') },
            { key: 'office', label: app.fieldLabel('job', 'office') },
            { key: 'copies', label: 'Jobs copied from it', align: 'right', render: (j) => jobs.filter((x) => x.copied_from === j.id).length },
          ]} />
        </Card>
      )}
      {!recruiter && <div className="text-xs text-muted-foreground">Recruiters create and change jobs; you can read them.</div>}
    </Stack>
  )
}
