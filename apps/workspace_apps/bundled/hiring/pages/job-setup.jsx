// One job's setup (?id): its details and pay range, hiring team, job post and
// public link, openings, interview plan and approvals — and the buttons that
// move it through draft → awaiting approval → open → closed. A draft with
// approvers is sent for approval and opens itself when the last one approves;
// a draft with none may be opened directly. Neither happens without a pay range
// while the app's "A job cannot open without a pay range" setting is on.
const DETAIL_FIELDS = ['title', 'department', 'office', 'location', 'country', 'employment_type', 'workplace', 'description',
  'requisition_id', 'openings', 'confidential', 'is_template', 'pay_min', 'pay_max', 'pay_currency', 'pay_period',
  'hiring_manager', 'recruiter', 'coordinator', 'hiring_team', 'valid_through']
const STEP_FIELDS = ['position', 'name', 'stage', 'duration', 'interviewers', 'prep', 'questions', 'attributes']
const BOARDS = [
  { src: 'devex', label: 'Devex' }, { src: 'reliefweb', label: 'ReliefWeb' }, { src: 'idealist', label: 'Idealist' },
  { src: 'naukri', label: 'Naukri' }, { src: 'brightermonday', label: 'BrighterMonday' }, { src: 'fuzu', label: 'Fuzu' },
]
const STATUS_TONE = { draft: 'default', pending_approval: 'warning', open: 'success', closed: 'default' }
const APPROVAL_TONE = { pending: 'warning', approved: 'success', declined: 'destructive' }

function inputsOf(app, action, fallback) {
  const a = (app.dataModel && app.dataModel._actions) || {}
  return (a[action] && a[action].input) || fallback
}
function blank(v) { return v == null || v === '' }
function money(n) { return Number(n).toLocaleString('en-US') }
function payText(j, app) {
  if (blank(j.pay_min) && blank(j.pay_max)) return ''
  const per = app.optionLabel('job', 'pay_period', j.pay_period || 'YEAR')
  const range = !blank(j.pay_min) && !blank(j.pay_max) ? `${money(j.pay_min)}–${money(j.pay_max)}` : money(blank(j.pay_min) ? j.pay_max : j.pay_min)
  return `${j.pay_currency || 'USD'} ${range} per ${per}`
}
function hostOrigin() {
  try {
    const a = window.location.ancestorOrigins
    if (a && a.length) return a[0]
  } catch (e) { /* not available in this browser */ }
  return ''
}

function Err({ err, at }) {
  return err && err.at === at ? <div className="text-sm text-destructive" role="alert">{err.msg}</div> : null
}

function Details({ app, job, onSave }) {
  const [f, setF] = useState(() => ({ ...job, ...Object.fromEntries(DETAIL_FIELDS.map((k) => [k, job[k] ?? (k === 'confidential' || k === 'is_template' ? false : '')])) }))
  const [saved, setSaved] = useState(false)
  const set = (k) => (v) => { setSaved(false); setF((x) => ({ ...x, [k]: v })) }
  const num = (k) => (v) => set(k)(v === '' ? '' : Number(v))
  const remote = f.workplace !== 'On-site'
  async function save() {
    const allowed = inputsOf(app, 'update_job', DETAIL_FIELDS)
    const input = {}
    for (const k of allowed) if (k in f) input[k] = f[k] === '' ? null : f[k]
    if (await onSave(input)) setSaved(true)
  }
  return (
    <Form onSubmit={save}>
      <Stack gap={3}>
        <Grid cols={2}>
          <Input label="Job title" required value={f.title} onChange={set('title')} />
          <Input label={app.fieldLabel('job', 'requisition_id')} value={f.requisition_id} onChange={set('requisition_id')} />
          <Input label={app.fieldLabel('job', 'department')} value={f.department} onChange={set('department')} />
          <Input label={app.fieldLabel('job', 'office')} value={f.office} onChange={set('office')} />
          <Input label={app.fieldLabel('job', 'location')} value={f.location} onChange={set('location')} placeholder="e.g. Remote — Africa time zones" />
          <Select label={app.fieldLabel('job', 'workplace')} value={f.workplace} onChange={set('workplace')} options={app.options('job', 'workplace')} />
          <Select label={app.fieldLabel('job', 'employment_type')} value={f.employment_type} onChange={set('employment_type')} options={app.options('job', 'employment_type')} />
          <Input label={app.fieldLabel('job', 'openings')} type="number" value={String(f.openings ?? '')} onChange={num('openings')} />
          <Input label={app.fieldLabel('job', 'valid_through')} type="date" value={f.valid_through} onChange={set('valid_through')} />
        </Grid>
        {remote && (
          <Input label={<>{app.fieldLabel('job', 'country')}<span className="block text-xs font-normal text-muted-foreground">Comma-separated, e.g. United States, Kenya, India. Job boards use it to show the role only where people may apply from.</span></>}
            value={f.country} onChange={set('country')} />
        )}
        <fieldset className="rounded-md border border-border p-3">
          <legend className="px-1 text-sm font-medium">Pay range</legend>
          <Grid cols={4}>
            <Input label="From" type="number" value={String(f.pay_min ?? '')} onChange={num('pay_min')} />
            <Input label="To" type="number" value={String(f.pay_max ?? '')} onChange={num('pay_max')} />
            <Select label="Currency" value={f.pay_currency} onChange={set('pay_currency')} options={app.options('job', 'pay_currency')} />
            <Select label="Per" value={f.pay_period} onChange={set('pay_period')} options={app.options('job', 'pay_period')} />
          </Grid>
          <div className="mt-1 text-xs text-muted-foreground">Shown on the post and in the job-board feed. Massachusetts and several other places require a good-faith range.</div>
        </fieldset>
        <fieldset className="rounded-md border border-border p-3">
          <legend className="px-1 text-sm font-medium">Hiring team</legend>
          <Grid cols={3}>
            <Input label="Hiring manager" type="email" value={f.hiring_manager} onChange={set('hiring_manager')} />
            <Input label="Recruiter" type="email" value={f.recruiter} onChange={set('recruiter')} />
            <Input label="Coordinator" type="email" value={f.coordinator} onChange={set('coordinator')} />
          </Grid>
          <div className="mt-2"><Input label="Interviewers (comma-separated emails)" value={f.hiring_team} onChange={set('hiring_team')} /></div>
        </fieldset>
        <TextArea label="Description" required rows={12} value={f.description} onChange={set('description')} />
        <FieldInputs collection="job" action="update_job" values={f} onChange={(v) => { setSaved(false); setF(v) }} exclude={DETAIL_FIELDS} />
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.confidential === true} onChange={(e) => set('confidential')(e.target.checked)} /> Confidential — never posted publicly</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={f.is_template === true} onChange={(e) => set('is_template')(e.target.checked)} /> Template job — copied from, never opened</label>
        </div>
        <div className="flex items-center gap-2"><Button type="submit" tone="primary">Save details</Button>{saved && <span className="text-sm text-success">Saved.</span>}</div>
      </Stack>
    </Form>
  )
}

function ReadOnlyDetails({ app, job }) {
  const rows = [['Requisition ID', job.requisition_id], [app.fieldLabel('job', 'department'), job.department], [app.fieldLabel('job', 'office'), job.office],
    ['Location', [job.location, job.workplace, job.employment_type].filter(Boolean).join(' · ')], ['Countries', job.country],
    ['Pay range', payText(job, app)], ['Openings', job.openings], ['Hiring manager', job.hiring_manager], ['Recruiter', job.recruiter],
    ['Coordinator', job.coordinator], ['Interviewers', job.hiring_team], ['Applications close', job.valid_through]].filter(([, v]) => !blank(v))
  return (
    <div>
      <dl className="grid gap-x-4 gap-y-1 text-sm" style={{ gridTemplateColumns: 'max-content 1fr' }}>
        {rows.map(([k, v]) => <div key={k} className="contents"><dt className="text-muted-foreground">{k}</dt><dd>{String(v)}</dd></div>)}
      </dl>
      <FieldValues collection="job" record={job} />
      <div className="mt-3 whitespace-pre-wrap text-sm">{job.description}</div>
    </div>
  )
}

function StepForm({ app, step, onSave, onCancel }) {
  const [s, setS] = useState({ name: '', stage: 'Interview', duration: 45, interviewers: '', prep: '', questions: '', attributes: '', ...step })
  const set = (k) => (v) => setS((x) => ({ ...x, [k]: v }))
  const stages = app.options('application', 'stage').filter((o) => !['Applied', 'Hired', 'Rejected'].includes(o.value))
  const stageOpts = stages.some((o) => o.value === s.stage) || !s.stage ? stages : [{ value: s.stage, label: s.stage }, ...stages]
  return (
    <Form onSubmit={() => onSave(s)}>
      <Stack gap={2}>
        <Grid cols={3}>
          <Input label="Interview" required value={s.name} onChange={set('name')} placeholder="e.g. Technical interview" />
          {stageOpts.length ? <Select label="Stage" value={s.stage} onChange={set('stage')} options={stageOpts} /> : <Input label="Stage" value={s.stage} onChange={set('stage')} />}
          <Input label="Minutes" type="number" value={String(s.duration ?? '')} onChange={(v) => set('duration')(v === '' ? null : Number(v))} />
        </Grid>
        <Input label="Default interviewers (comma-separated emails)" value={s.interviewers} onChange={set('interviewers')} />
        <TextArea label="How to prepare" rows={3} value={s.prep} onChange={set('prep')} />
        <Grid cols={2}>
          <TextArea label="Questions to ask (one per line)" rows={5} value={s.questions} onChange={set('questions')} />
          <TextArea label="What to look for (one attribute per line)" rows={5} value={s.attributes} onChange={set('attributes')} />
        </Grid>
        <div className="flex gap-2"><Button type="submit" tone="primary">Save step</Button><Button onClick={onCancel}>Cancel</Button></div>
      </Stack>
    </Form>
  )
}

function View({ app, scope }) {
  const id = Number(app.params.id)
  const recruiter = app.roles.includes('recruiter')
  const [job, setJob] = useState(undefined)
  const [openings, setOpenings] = useState([])
  const [steps, setSteps] = useState([])
  const [approvals, setApprovals] = useState([])
  const [err, setErr] = useState(null)
  const [closing, setClosing] = useState(null)
  const [newOpening, setNewOpening] = useState('')
  const [closingOpening, setClosingOpening] = useState(null)
  const [editStep, setEditStep] = useState(null)
  const [approver, setApprover] = useState('')
  const [board, setBoard] = useState(BOARDS[0].src)
  const [copied, setCopied] = useState(null)
  const textRef = useRef(null)

  async function load() {
    if (!id) { setJob(null); return }
    const rows = await app.query('job', { where: { id } })
    setJob(rows[0] || null)
    if (!rows[0]) return
    app.query('opening', { where: { job: id }, order: 'created_at', limit: 200 }).then(setOpenings, () => setOpenings([]))
    app.query('interview_step', { where: { job: id }, order: 'position', limit: 100 }).then(setSteps, () => setSteps([]))
    app.query('approval', { where: { job: id, kind: 'Job' }, order: 'created_at', limit: 100 }).then(setApprovals, () => setApprovals([]))
  }
  useEffect(() => { load().catch((e) => { setErr({ at: 'top', msg: e.message }); setJob(null) }) }, [id])

  async function run(at, fn) {
    setErr(null)
    try { await fn(); await load(); return true } catch (e) { setErr({ at, msg: e.message }); return false }
  }

  if (job === undefined) return <Loading />
  if (job === null) return <Empty>{(err && err.msg) || 'No such job.'} <button type="button" className="underline" onClick={() => app.go('jobs')}>All jobs</button></Empty>

  const needsPay = app.settings.require_pay_range !== false && (blank(job.pay_min) || blank(job.pay_max))
  const payBlock = 'Add a pay range (from and to) before this job can open — the app requires one on every posting (Massachusetts pay transparency).'
  const pending = approvals.filter((a) => a.status === 'pending')
  const declined = approvals.filter((a) => a.status === 'declined')
  const allApproved = approvals.length > 0 && pending.length === 0 && declined.length === 0
  const path = `/w/${scope.workspace.slug}/app/${app.key}/job?id=${job.id}`
  const publicUrl = `${hostOrigin()}${path}`
  const posted = job.status === 'open' && job.post_status === 'live' && !job.confidential

  async function submitForApproval() {
    await run('life', async () => {
      // Asking again re-asks everyone who already answered a previous round.
      for (const a of approvals.filter((x) => x.status !== 'pending')) await app.act('reset_approval', { record: a.id })
      await app.act('submit_job', { record: job.id })
    })
  }

  const boardText = (src) => [
    [
      job.title,
      [app.settings.company_name, job.location, job.workplace, job.employment_type].filter(Boolean).join(' · '),
      job.country && job.workplace !== 'On-site' ? `Open to people living in: ${job.country}` : '',
      payText(job, app) ? `Pay: ${payText(job, app)}` : '',
      job.valid_through ? `Apply by ${job.valid_through}` : '',
    ].filter(Boolean).join('\n'),
    job.description || '',
    [`Apply here: ${publicUrl}&src=${src}`,
      app.settings.accommodation_contact ? `Need an accommodation to apply? Email ${app.settings.accommodation_contact}.` : ''].filter(Boolean).join('\n'),
  ].filter(Boolean).join('\n\n')

  async function copy(text, what) {
    try { await navigator.clipboard.writeText(text); setCopied(what) } catch (e) {
      if (textRef.current) { textRef.current.focus(); textRef.current.select() }
      setCopied(`${what}-select`)
    }
  }

  const lifecycle = recruiter && !job.is_template && (
    <div className="flex flex-wrap items-center gap-2">
      {job.status === 'draft' && approvals.length > 0 && (
        <Button tone="primary" disabled={needsPay} onClick={submitForApproval}>Ask for approval ({approvals.length})</Button>
      )}
      {job.status === 'draft' && approvals.length === 0 && (
        <Button tone="primary" disabled={needsPay} onClick={() => run('life', () => app.act('open_job', { record: job.id }))}>Open the job</Button>
      )}
      {job.status === 'pending_approval' && allApproved && (
        <Button tone="primary" disabled={needsPay} onClick={() => run('life', () => app.act('open_job', { record: job.id }))}>Open the job — all approved</Button>
      )}
      {job.status === 'closed' && (
        <Button disabled={needsPay} onClick={() => run('life', () => app.act('reopen_job', { record: job.id }))}>Reopen the job</Button>
      )}
      {['draft', 'pending_approval', 'open'].includes(job.status) && closing === null && (
        <Button tone="destructive" onClick={() => setClosing('')}>Close the job…</Button>
      )}
    </div>
  )

  return (
    <Stack gap={4}>
      <button type="button" className="text-left text-sm text-muted-foreground hover:text-foreground" onClick={() => app.go('jobs')}>← All jobs</button>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-semibold">{job.title}</h2>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
            <Badge tone={STATUS_TONE[job.status]}>{app.optionLabel('job', 'status', job.status)}</Badge>
            {job.is_template && <Badge>Template</Badge>}
            {job.confidential && <Badge tone="warning">Confidential</Badge>}
            {[job.requisition_id, job.department, job.office].filter(Boolean).join(' · ')}
            {job.opened_on && <span>· opened {job.opened_on}</span>}
            {job.closed_on && job.status === 'closed' && <span>· closed {job.closed_on}{job.close_reason ? ` (${job.close_reason})` : ''}</span>}
          </div>
        </div>
        {lifecycle}
      </div>
      {recruiter && !job.is_template && needsPay && ['draft', 'pending_approval', 'closed'].includes(job.status) && (
        <div className="rounded-md border border-border bg-muted p-3 text-sm text-warning">{payBlock}</div>
      )}
      {recruiter && job.status === 'draft' && !job.is_template && (
        <div className="text-xs text-muted-foreground">
          {approvals.length ? 'With approvers, the job opens by itself once every approver has approved.' : 'No approvers: you may open it now, or add approvers below to have it approved first.'}
        </div>
      )}
      {job.status === 'pending_approval' && (
        <div className="text-sm text-muted-foreground">Waiting on {pending.length} of {approvals.length} approver{approvals.length === 1 ? '' : 's'}{declined.length ? ` · ${declined.length} declined` : ''}.</div>
      )}
      {job.is_template && <div className="text-sm text-muted-foreground">A template never opens. Use “Create a job” on the Jobs page to copy it, interview plan and all.</div>}
      {closing !== null && (
        <Card title="Close this job">
          <Stack gap={2}>
            <Select label="Why?" value={closing} onChange={setClosing} options={[{ value: '', label: 'Choose a reason…' }, ...app.options('job', 'close_reason')]} />
            <div className="flex gap-2">
              <Button tone="destructive" disabled={!closing} onClick={async () => { if (await run('life', () => app.act('close_job', { record: job.id, input: { close_reason: closing } }))) setClosing(null) }}>Close the job</Button>
              <Button onClick={() => setClosing(null)}>Cancel</Button>
            </div>
          </Stack>
        </Card>
      )}
      <Err err={err} at="life" />
      <Err err={err} at="top" />

      <Card title="Details">
        {recruiter ? <Details key={job.updated_at} app={app} job={job} onSave={(input) => run('details', () => app.act('update_job', { record: job.id, input }))} /> : <ReadOnlyDetails app={app} job={job} />}
        <Err err={err} at="details" />
      </Card>

      <Card title="Job post" actions={recruiter && (
        <Select label="Post status" value={job.post_status || 'live'} onChange={(post_status) => run('post', () => app.act('set_post', { record: job.id, input: { post_status } }))} options={app.options('job', 'post_status')} />
      )}>
        <Stack gap={3}>
          <div className="text-sm">
            {posted ? <span className="text-success">Live — anyone with the link can read and apply.</span>
              : job.confidential ? 'Confidential: never shown on the portal, even when open.'
                : job.status !== 'open' ? 'Not public yet: the post goes live when the job opens.'
                  : 'Offline: the job stays open but the post and its link are gone.'}
          </div>
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <code className="rounded bg-muted px-2 py-1 text-xs break-all">{publicUrl}</code>
            <Button size="sm" onClick={() => copy(publicUrl, 'url')}>{copied === 'url' ? 'Copied' : 'Copy link'}</Button>
          </div>
          <div className="text-xs text-muted-foreground">Add ?src=… (e.g. &src=linkedin) to a link you share, and each application records where it came from.</div>
          <div>
            <div className="mb-1 text-sm font-medium">Copy-ready text for job boards without a feed</div>
            <div className="mb-2 flex flex-wrap gap-1" role="tablist" aria-label="Job board">
              {BOARDS.map((b) => <Button key={b.src} size="sm" tone={board === b.src ? 'primary' : 'default'} onClick={() => { setBoard(b.src); setCopied(null) }}>{b.label}</Button>)}
            </div>
            <textarea ref={textRef} readOnly rows={10} aria-label="Post text to paste" className="w-full rounded-md border border-border bg-background p-2 font-mono text-xs" value={boardText(board)} />
            <div className="mt-1 flex items-center gap-2">
              <Button size="sm" onClick={() => copy(boardText(board), board)}>{copied === board ? 'Copied' : 'Copy text'}</Button>
              {copied === `${board}-select` && <span className="text-xs text-muted-foreground">Selected — press Ctrl+C (⌘C on a Mac) to copy.</span>}
              {copied === 'url-select' && <span className="text-xs text-muted-foreground">Couldn't copy here; select the link above and copy it.</span>}
            </div>
          </div>
          <Err err={err} at="post" />
        </Stack>
      </Card>

      <Card title={`Openings (${openings.filter((o) => o.status === 'open').length} open of ${openings.length})`}>
        <Stack gap={2}>
          <Table rows={openings} empty="No openings yet. Each opening is one seat to fill; marking an offer hired closes one." columns={[
            { key: 'opening_id', label: 'Opening ID' },
            { key: 'status', label: 'Status', render: (o) => <Badge tone={o.status === 'open' ? 'success' : 'default'}>{o.status}</Badge> },
            { key: 'opened_on', label: 'Opened' },
            { key: 'closed_on', label: 'Closed', render: (o) => o.closed_on ? `${o.closed_on}${o.close_reason ? ` · ${o.close_reason}` : ''}` : '' },
            { key: 'hired_application', label: 'Filled by', render: (o) => o.hired_application ? <button type="button" className="hover:underline" onClick={() => app.go('candidate', { id: o.hired_application })}>Candidate #{o.hired_application}</button> : '' },
            ...(recruiter ? [{ key: 'act', label: '', render: (o) => o.status === 'open'
              ? (closingOpening && closingOpening.id === o.id
                ? <div className="flex gap-1"><Select value={closingOpening.reason} onChange={(reason) => setClosingOpening({ id: o.id, reason })} options={[{ value: '', label: 'Reason…' }, ...app.options('opening', 'close_reason')]} />
                  <Button size="sm" tone="destructive" disabled={!closingOpening.reason} onClick={async () => { if (await run('openings', () => app.act('close_opening', { record: o.id, input: { close_reason: closingOpening.reason } }))) setClosingOpening(null) }}>Close</Button>
                  <Button size="sm" onClick={() => setClosingOpening(null)}>Cancel</Button></div>
                : <Button size="sm" onClick={() => setClosingOpening({ id: o.id, reason: '' })}>Close…</Button>)
              : <Button size="sm" onClick={() => run('openings', () => app.act('reopen_opening', { record: o.id }))}>Reopen</Button> }] : []),
          ]} />
          {recruiter && (
            <Form onSubmit={async () => { if (await run('openings', () => app.act('add_opening', { input: { job: job.id, opening_id: newOpening } }))) setNewOpening('') }}>
              <div className="flex flex-wrap items-end gap-2">
                <Input label="New opening ID" required value={newOpening} onChange={setNewOpening} placeholder={`${job.requisition_id || `JOB-${job.id}`}-${openings.length + 1}`} />
                <Button type="submit">Add opening</Button>
              </div>
            </Form>
          )}
          {!blank(job.openings) && openings.length !== Number(job.openings) && <div className="text-xs text-muted-foreground">The job's details say {job.openings} opening{Number(job.openings) === 1 ? '' : 's'}; {openings.length} {openings.length === 1 ? 'is' : 'are'} recorded here.</div>}
          <Err err={err} at="openings" />
        </Stack>
      </Card>

      <Card title={`Interview plan (${steps.length})`} actions={recruiter && editStep === null && <Button size="sm" onClick={() => setEditStep('new')}>Add an interview</Button>}>
        <Stack gap={3}>
          {steps.length === 0 && editStep !== 'new' && <Empty>No interviews planned yet. Each step becomes an interview kit with its questions and what to look for.</Empty>}
          {steps.map((s, i) => editStep === s.id ? (
            <StepForm key={s.id} app={app} step={s} onCancel={() => setEditStep(null)} onSave={async (v) => {
              const input = {}
              for (const k of inputsOf(app, 'update_step', STEP_FIELDS)) if (k in v) input[k] = v[k] === '' ? null : v[k]
              if (await run('plan', () => app.act('update_step', { record: s.id, input }))) setEditStep(null)
            }} />
          ) : (
            <div key={s.id} className="flex flex-wrap items-start justify-between gap-2 border-b border-border pb-2 text-sm last:border-0">
              <div className="min-w-0">
                <div><span className="tabular-nums text-muted-foreground">{i + 1}.</span> <span className="font-medium">{s.name}</span> <span className="text-muted-foreground">· {s.stage || 'Interview'} · {s.duration || 45} min</span></div>
                <div className="text-xs text-muted-foreground">
                  {s.interviewers || 'No default interviewers'}
                  {' · '}{(s.questions || '').split('\n').filter((l) => l.trim()).length} questions
                  {' · '}{(s.attributes || '').split('\n').filter((l) => l.trim()).length} attributes
                </div>
              </div>
              <div className="flex gap-1">
                {recruiter && i > 0 && <Button size="sm" onClick={() => run('plan', async () => {
                  const prev = steps[i - 1]
                  await app.act('update_step', { record: s.id, input: { position: i } })
                  await app.act('update_step', { record: prev.id, input: { position: i + 1 } })
                })}>↑</Button>}
                {recruiter && <Button size="sm" onClick={() => setEditStep(s.id)}>Edit</Button>}
                {recruiter && <Button size="sm" tone="destructive" confirm={`Remove “${s.name}” from the plan?`} onClick={() => run('plan', () => app.act('remove_step', { record: s.id }))}>Remove</Button>}
              </div>
            </div>
          ))}
          {editStep === 'new' && (
            <StepForm app={app} step={{ position: steps.length + 1 }} onCancel={() => setEditStep(null)} onSave={async (v) => {
              const input = { job: job.id }
              for (const k of STEP_FIELDS) if (!blank(v[k])) input[k] = v[k]
              if (await run('plan', () => app.act('add_step', { input }))) setEditStep(null)
            }} />
          )}
          <Err err={err} at="plan" />
        </Stack>
      </Card>

      <Card title={`Approvals (${approvals.length})`}>
        <Stack gap={2}>
          <Table rows={approvals} empty="No approvers. Add one to have the job approved before it opens." columns={[
            { key: 'approver', label: 'Approver' },
            { key: 'status', label: 'Status', render: (a) => <Badge tone={APPROVAL_TONE[a.status]}>{app.optionLabel('approval', 'status', a.status)}</Badge> },
            { key: 'decided_at', label: 'Decided', render: (a) => (a.decided_at || '').slice(0, 16).replace('T', ' ') },
            { key: 'note', label: 'Note' },
            ...(recruiter ? [{ key: 'act', label: '', render: (a) => a.status === 'pending'
              ? <Button size="sm" onClick={() => run('approvals', () => app.act('remove_approver', { record: a.id }))}>Remove</Button>
              : <Button size="sm" onClick={() => run('approvals', () => app.act('reset_approval', { record: a.id }))}>Ask again</Button> }] : []),
          ]} />
          {recruiter && !job.is_template && (
            <Form onSubmit={async () => { if (await run('approvals', () => app.act('add_approver', { input: { kind: 'Job', job: job.id, approver, summary: `Open the job “${job.title}”${job.department ? ` (${job.department})` : ''}${job.pay_min != null ? `, pay ${job.pay_currency || ''} ${job.pay_min}–${job.pay_max ?? ''} per ${String(job.pay_period || 'YEAR').toLowerCase()}` : ''}, ${job.openings || 1} opening(s).` } }))) setApprover('') }}>
              <div className="flex flex-wrap items-end gap-2">
                <Input label="Add an approver (email)" type="email" required value={approver} onChange={setApprover} />
                <Button type="submit">Add approver</Button>
              </div>
              <div className="mt-1 text-xs text-muted-foreground">Approvers are emailed when they're added and approve on their Approvals page. A decline sends the job back to draft.</div>
            </Form>
          )}
          <Err err={err} at="approvals" />
        </Stack>
      </Card>

      <Slot name="job_extra" record={job} reload={load} />
    </Stack>
  )
}
