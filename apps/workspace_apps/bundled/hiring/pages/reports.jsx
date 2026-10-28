// Recruiting reports with the standard ATS formulas, so the numbers line up
// after a switch: current pipeline per job (Now / Ever), pass-through rates,
// days in stage, time to hire, time to fill, source of hire, offer acceptance and
// open requisitions. Each report states its formula under it. Filters: job,
// team, date applied. Downloads aren't possible in the sandbox, so each report
// can be copied as CSV from a text box.
const TODAY = new Date().toISOString().slice(0, 10)
const DAY = 86400000

function dayMs(v) {
  return v ? Date.parse(String(v).slice(0, 10) + 'T00:00:00Z') : NaN
}
function daysBetween(a, b) {
  const x = dayMs(a)
  const y = dayMs(b)
  return Number.isNaN(x) || Number.isNaN(y) ? null : Math.round((y - x) / DAY)
}
function avg(list) {
  const xs = list.filter((v) => v != null && !Number.isNaN(v))
  return xs.length ? Math.round((xs.reduce((s, v) => s + v, 0) / xs.length) * 10) / 10 : null
}
function pct(n, d) {
  return d ? `${Math.round((n / d) * 1000) / 10}%` : '—'
}
function appliedOf(a) {
  return a.applied_on || String(a.created_at || '').slice(0, 10)
}
function csvOf(columns, rows) {
  const esc = (v) => {
    const s = v == null ? '' : String(v)
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
  }
  return [columns.map((c) => esc(typeof c.label === 'string' ? c.label : c.key)).join(','),
    ...rows.map((r) => columns.map((c) => esc(r[c.key])).join(','))].join('\n')
}

function CopyCsv({ columns, rows }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="mt-3">
      <Button size="sm" onClick={() => setOpen(!open)}>{open ? 'Hide CSV' : 'Copy as CSV'}</Button>
      {open && (
        <label className="mt-2 block text-xs text-muted-foreground">
          Select all (Ctrl/⌘ A) and copy, then paste into a spreadsheet.
          <textarea readOnly rows={Math.min(14, rows.length + 2)} value={csvOf(columns, rows)}
            onFocus={(e) => e.target.select()}
            className="mt-1 w-full rounded-md border border-border bg-background p-2 font-mono text-xs text-foreground" />
        </label>
      )}
    </div>
  )
}

function Formula({ children }) {
  return <p className="mt-2 text-xs text-muted-foreground">{children}</p>
}

function View({ data, app }) {
  const base = (k) => (Array.isArray(data[k]) ? data[k] : [])
  const jobs = base('jobs').filter((j) => !j.is_template)
  const offers = base('offers')
  const openings = base('openings')
  const [jobId, setJobId] = useState(app.params?.job ? String(app.params.job) : '')
  const [dept, setDept] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [tab, setTab] = useState('pipeline')
  const [mode, setMode] = useState('both')
  const [forJob, setForJob] = useState(null) // {apps, changes} read for one job, past the 2000-row source cap
  const [steps, setSteps] = useState([])

  useEffect(() => {
    app.query('interview_step', { limit: 2000 }).then(setSteps, () => setSteps([]))
  }, [])
  useEffect(() => {
    if (!jobId) { setForJob(null); return }
    const id = Number(jobId)
    Promise.all([
      app.query('application', { where: { job: id, kind: 'applicant' }, limit: 2000 }),
      app.query('stage_change', { where: { job: id }, order: 'created_at', limit: 2000 }),
    ]).then(([apps, changes]) => setForJob({ id, apps, changes }), () => setForJob(null))
  }, [jobId])

  const allApps = forJob ? forJob.apps : base('applications')
  const allChanges = forJob ? forJob.changes : base('changes')
  const capped = !forJob && (base('applications').length >= 2000 || base('changes').length >= 2000)

  const stages = (() => {
    const o = app.options('application', 'stage').map((x) => x.value)
    return o.length ? o : app.field('application', 'stage')?.options || []
  })()
  const flow = stages.filter((s) => s !== 'Rejected')
  const hiredStage = flow[flow.length - 1] || 'Hired'
  const label = (s) => app.optionLabel('application', 'stage', s) || s

  const jobsF = jobs.filter((j) => (!jobId || j.id === Number(jobId)) && (!dept || (j.department || 'No team') === dept))
  const jobIds = new Set(jobsF.map((j) => j.id))
  const jobById = Object.fromEntries(jobs.map((j) => [j.id, j]))
  const inRange = (d) => (!from || (d && d >= from)) && (!to || (d && d <= to))
  const appsF = allApps.filter((a) => jobIds.has(a.job) && inRange(appliedOf(a)))
  const appIds = new Set(appsF.map((a) => a.id))

  // Each application's stage history, oldest first.
  const history = {}
  for (const c of allChanges) if (appIds.has(c.application)) (history[c.application] ||= []).push(c)
  for (const list of Object.values(history)) list.sort((x, y) => String(x.at || x.created_at).localeCompare(String(y.at || y.created_at)))
  const entered = (a) => new Set([...(history[a.id] || []).map((c) => c.to_stage), a.stage].filter(Boolean))
  // Where a candidate got to: the last stage of the flow they were moved into (a move backwards lowers it).
  const position = (a) => {
    let p = 0
    for (const c of history[a.id] || []) if (flow.includes(c.to_stage)) p = flow.indexOf(c.to_stage)
    if (!history[a.id]?.length && flow.includes(a.stage)) p = flow.indexOf(a.stage)
    return p
  }

  // --- current pipeline per job
  const stepStages = {}
  for (const s of steps) if (s.job) (stepStages[s.job] ||= new Set()).add(s.stage || 'Interview')
  const pipeRows = jobsF.map((j) => {
    const js = appsF.filter((a) => a.job === j.id)
    const row = { job: j.title, req: j.requisition_id || '', dept: j.department || '', status: j.status, hires: js.filter((a) => a.status === 'hired').length }
    for (const s of flow) {
      const ever = js.filter((a) => entered(a).has(s)).length
      const now = js.filter((a) => a.stage === s && (a.status === 'active' || s === hiredStage)).length
      const has = !stepStages[j.id] || [flow[0], 'Offer', hiredStage].includes(s) || stepStages[j.id].has(s) || ever > 0
      row[`now:${s}`] = has ? now : ''
      row[`ever:${s}`] = has ? ever : ''
    }
    return row
  })
  const pipeCols = [
    { key: 'job', label: 'Job' }, { key: 'req', label: 'Req ID' }, { key: 'dept', label: 'Team' },
    ...flow.flatMap((s) => [
      ...(mode !== 'ever' ? [{ key: `now:${s}`, label: `${label(s)}${mode === 'both' ? ' — now' : ''}`, align: 'right' }] : []),
      ...(mode !== 'now' ? [{ key: `ever:${s}`, label: `${label(s)}${mode === 'both' ? ' — ever' : ''}`, align: 'right' }] : []),
    ]),
    { key: 'hires', label: 'Hires', align: 'right' },
  ]

  // --- pass-through and days in stage
  const stay = {}
  for (const list of Object.values(history)) {
    for (let i = 0; i + 1 < list.length; i++) {
      const t0 = Date.parse(list[i].at || list[i].created_at)
      const t1 = Date.parse(list[i + 1].at || list[i + 1].created_at)
      if (!Number.isNaN(t0) && !Number.isNaN(t1)) (stay[list[i].to_stage] ||= []).push((t1 - t0) / DAY)
    }
  }
  const reached = flow.map((s, i) => appsF.filter((a) => position(a) >= i).length)
  const passRows = flow.map((s, i) => ({
    stage: label(s), reached: reached[i],
    next: i + 1 < flow.length ? reached[i + 1] : '',
    rate: i + 1 < flow.length ? pct(reached[i + 1], reached[i]) : '',
    days: s === hiredStage ? '' : avg(stay[s] || []) ?? '—',
    stays: (stay[s] || []).length,
  }))
  const passCols = [
    { key: 'stage', label: 'Stage' }, { key: 'reached', label: 'Reached', align: 'right' },
    { key: 'next', label: 'Reached next stage', align: 'right' }, { key: 'rate', label: 'Pass-through', align: 'right' },
    { key: 'days', label: 'Avg days in stage', align: 'right' }, { key: 'stays', label: 'Stays measured', align: 'right' },
  ]

  // --- time to hire
  const offersOf = {}
  for (const o of offers) if (o.application) (offersOf[o.application] ||= []).push(o)
  const hiredApps = appsF.filter((a) => a.status === 'hired')
  const hireRows = hiredApps.map((a) => {
    const os = (offersOf[a.id] || []).slice().sort((x, y) => String(x.created_at).localeCompare(String(y.created_at)))
    const o = os.find((x) => x.status === 'hired') || os[0]
    const offerDay = o ? String(o.created_at || '').slice(0, 10) : ''
    return {
      name: a.name, job: jobById[a.job]?.title || '', source: a.source || '', applied: appliedOf(a), offer: offerDay,
      hired: a.hired_on || '', to_offer: offerDay ? daysBetween(appliedOf(a), offerDay) : '', to_hire: a.hired_on ? daysBetween(appliedOf(a), a.hired_on) : '',
    }
  })
  const hireCols = [
    { key: 'name', label: 'Hired candidate' }, { key: 'job', label: 'Job' }, { key: 'source', label: 'Source' },
    { key: 'applied', label: 'Applied' }, { key: 'offer', label: 'Offer created' }, { key: 'hired', label: 'Hired on' },
    { key: 'to_offer', label: 'Days to offer', align: 'right' }, { key: 'to_hire', label: 'Days to hire date', align: 'right' },
  ]

  // --- time to fill
  const fillRows = openings.filter((o) => jobIds.has(o.job) && o.status === 'closed' && o.close_reason === 'Hired' && inRange(o.closed_on))
    .map((o) => ({ opening: o.opening_id, job: jobById[o.job]?.title || '', opened: o.opened_on || '', closed: o.closed_on || '', days: daysBetween(o.opened_on, o.closed_on) ?? '' }))
  const fillCols = [
    { key: 'opening', label: 'Opening ID' }, { key: 'job', label: 'Job' }, { key: 'opened', label: 'Opened' },
    { key: 'closed', label: 'Filled' }, { key: 'days', label: 'Days to fill', align: 'right' },
  ]

  // --- sources
  const sources = {}
  for (const a of appsF) {
    const s = a.source || 'Unknown'
    sources[s] ||= { source: s, apps: 0, hires: 0 }
    sources[s].apps++
    if (a.status === 'hired') sources[s].hires++
  }
  const sourceRows = Object.values(sources).sort((x, y) => y.hires - x.hires || y.apps - x.apps)
    .map((r) => ({ ...r, rate: pct(r.hires, r.apps), share: pct(r.hires, hiredApps.length) }))
  const sourceCols = [
    { key: 'source', label: 'Source' }, { key: 'apps', label: 'Applications', align: 'right' }, { key: 'hires', label: 'Hires', align: 'right' },
    { key: 'share', label: 'Share of hires', align: 'right' }, { key: 'rate', label: 'Hire rate', align: 'right' },
  ]

  // --- offers
  const ranged = from || to
  const offersF = offers.filter((o) => jobIds.has(o.job ?? allApps.find((a) => a.id === o.application)?.job) && (!ranged || appIds.has(o.application)))
  const byStatus = (st) => offersF.filter((o) => st.includes(o.status)).length
  const accepted = byStatus(['accepted', 'hired'])
  const declined = byStatus(['declined'])
  const offerStatuses = app.options('offer', 'status').length ? app.options('offer', 'status')
    : (app.field('offer', 'status')?.options || []).map((v) => ({ value: v, label: v }))
  const offerRows = offerStatuses.map((o) => ({ status: o.label, count: byStatus([o.value]) }))
  const offerCols = [{ key: 'status', label: 'Offer status' }, { key: 'count', label: 'Offers', align: 'right' }]

  // --- open requisitions
  const openJobs = jobsF.filter((j) => j.status === 'open')
  const openOpenings = (id) => openings.filter((o) => o.job === id && o.status === 'open').length
  const depts = {}
  for (const j of openJobs) {
    const d = j.department || 'No team'
    depts[d] ||= { dept: d, jobs: 0, openings: 0, days: [] }
    depts[d].jobs++
    depts[d].openings += openOpenings(j.id)
    depts[d].days.push(daysBetween(j.opened_on, TODAY))
  }
  const reqRows = Object.values(depts).sort((a, b) => b.jobs - a.jobs)
    .map((d) => ({ dept: d.dept, jobs: d.jobs, openings: d.openings, avg: avg(d.days) ?? '—', oldest: d.days.some((v) => v != null) ? Math.max(...d.days.filter((v) => v != null)) : '—' }))
  const reqCols = [
    { key: 'dept', label: 'Team' }, { key: 'jobs', label: 'Open jobs', align: 'right' }, { key: 'openings', label: 'Open openings', align: 'right' },
    { key: 'avg', label: 'Avg days open', align: 'right' }, { key: 'oldest', label: 'Oldest (days)', align: 'right' },
  ]
  const reqJobRows = openJobs.map((j) => ({ job: j.title, dept: j.department || '', req: j.requisition_id || '', opened: j.opened_on || '', days: daysBetween(j.opened_on, TODAY) ?? '', openings: openOpenings(j.id) }))
    .sort((a, b) => (Number(b.days) || 0) - (Number(a.days) || 0))
  const reqJobCols = [
    { key: 'job', label: 'Job' }, { key: 'dept', label: 'Team' }, { key: 'req', label: 'Req ID' }, { key: 'opened', label: 'Opened' },
    { key: 'days', label: 'Days open', align: 'right' }, { key: 'openings', label: 'Open openings', align: 'right' },
  ]

  const TABS = [
    ['pipeline', 'Current pipeline'], ['pass', 'Pass-through'], ['hire', 'Time to hire'], ['fill', 'Time to fill'],
    ['source', 'Sources'], ['offers', 'Offers'], ['reqs', 'Open requisitions'],
  ]
  const deptOptions = [...new Set(jobs.map((j) => j.department || 'No team'))].sort()

  return (
    <Stack gap={4}>
      <div className="grid grid-cols-1 gap-3 rounded-lg border border-border bg-card p-3 sm:grid-cols-4">
        <Select label="Job" value={jobId} onChange={setJobId}
          options={[{ value: '', label: 'All jobs' }, ...jobs.slice().sort((a, b) => String(a.title).localeCompare(String(b.title)))
            .map((j) => ({ value: String(j.id), label: `${j.title}${j.status !== 'open' ? ` (${app.optionLabel('job', 'status', j.status) || j.status})` : ''}` }))]} />
        <Select label="Team" value={dept} onChange={setDept} options={[{ value: '', label: 'All teams' }, ...deptOptions]} />
        <Input label="Applied from" type="date" value={from} onChange={setFrom} />
        <Input label="Applied to" type="date" value={to} onChange={setTo} />
      </div>
      {capped && <p className="text-sm text-warning">This workspace has more than 2,000 candidates or stage changes; totals across all jobs count the first 2,000. Pick a job for exact numbers.</p>}

      <Grid cols={4}>
        <Stat label="Applications" value={appsF.length} />
        <Stat label="Hires" value={hiredApps.length} tone="success" />
        <Stat label="Avg time to hire (offer created)" value={avg(hireRows.map((r) => (r.to_offer === '' ? null : r.to_offer))) ?? '—'} hint="days" />
        <Stat label="Offer acceptance" value={pct(accepted, accepted + declined)} hint={`${accepted} accepted · ${declined} declined`} />
      </Grid>

      <div role="tablist" aria-label="Reports" className="flex flex-wrap gap-1 border-b border-border">
        {TABS.map(([k, l]) => (
          <button key={k} type="button" role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
            className={`-mb-px rounded-t-md border-b-2 px-3 py-1.5 text-sm ${tab === k ? 'border-primary font-medium text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>{l}</button>
        ))}
      </div>

      {tab === 'pipeline' && (
        <Card title="Current pipeline per job" actions={
          <select aria-label="Stage columns" value={mode} onChange={(e) => setMode(e.target.value)} className="rounded-md border border-border bg-background px-2 py-1 text-xs">
            <option value="both">Now and ever</option><option value="now">Now</option><option value="ever">Ever</option>
          </select>}>
          <Table rows={pipeRows} columns={pipeCols} empty="No jobs match these filters." />
          <Formula>
            Now = active candidates in the stage today (for {label(hiredStage)}: everyone hired). Ever = candidates who were ever in the stage,
            from their stage history, each counted once per stage. A blank cell means the job doesn't use that stage (it has an interview
            plan without it and nobody has entered it); 0 means it does and nobody is there.
          </Formula>
          <CopyCsv columns={pipeCols} rows={pipeRows} />
        </Card>
      )}

      {tab === 'pass' && (
        <Card title="Pipeline history and pass-through rates">
          <Grid cols={2}>
            <Table rows={passRows} columns={passCols} />
            <BarChart data={flow.map((s, i) => ({ label: label(s), value: reached[i] }))} />
          </Grid>
          <Formula>
            Reached = candidates whose furthest stage is this one or later. A skipped stage counts as passed through; a candidate moved back
            is counted only up to the stage they were moved back to. Pass-through = reached the next stage ÷ reached this stage. Average days
            in stage = the mean time between a candidate entering the stage and their next stage change; candidates still in the stage aren't counted.
          </Formula>
          <CopyCsv columns={passCols} rows={passRows} />
        </Card>
      )}

      {tab === 'hire' && (
        <Card title="Time to hire">
          <Grid cols={2}>
            <Stat label="Average days, application → offer created" value={avg(hireRows.map((r) => (r.to_offer === '' ? null : r.to_offer))) ?? '—'} />
            <Stat label="Average days, application → hire date" value={avg(hireRows.map((r) => (r.to_hire === '' ? null : r.to_hire))) ?? '—'} />
          </Grid>
          <div className="mt-3"><Table rows={hireRows} columns={hireCols} empty="No hires match these filters." /></div>
          <Formula>
            Time to hire = days from the date applied to the day the offer was created (the standard ATS definition). The hire-date
            variant = days from the date applied to the date marked hired. Only hired candidates count.
          </Formula>
          <CopyCsv columns={hireCols} rows={hireRows} />
        </Card>
      )}

      {tab === 'fill' && (
        <Card title="Time to fill">
          <Stat label="Average days to fill" value={avg(fillRows.map((r) => (r.days === '' ? null : r.days))) ?? '—'} />
          <div className="mt-3"><Table rows={fillRows} columns={fillCols} empty="No openings filled by a hire match these filters." /></div>
          <Formula>
            Time to fill = days from the opening's open date to the date it was closed as Hired. Openings closed for another reason aren't counted.
            The date range applies to the day the opening was filled.
          </Formula>
          <CopyCsv columns={fillCols} rows={fillRows} />
        </Card>
      )}

      {tab === 'source' && (
        <Card title="Sources">
          <Grid cols={2}>
            <div><h3 className="mb-2 text-sm font-medium">Source of hire</h3><BarChart data={sourceRows.filter((r) => r.hires).map((r) => ({ label: r.source, value: r.hires }))} /></div>
            <div><h3 className="mb-2 text-sm font-medium">Applications by source</h3><BarChart data={sourceRows.map((r) => ({ label: r.source, value: r.apps }))} /></div>
          </Grid>
          <div className="mt-3"><Table rows={sourceRows} columns={sourceCols} /></div>
          <Formula>Source of hire = hired candidates by the source on their application. Hire rate = hires ÷ applications from that source.</Formula>
          <CopyCsv columns={sourceCols} rows={sourceRows} />
        </Card>
      )}

      {tab === 'offers' && (
        <Card title="Offers">
          <Grid cols={3}>
            <Stat label="Offer acceptance rate" value={pct(accepted, accepted + declined)} tone="success" />
            <Stat label="Accepted (incl. hired)" value={accepted} />
            <Stat label="Declined" value={declined} />
          </Grid>
          <div className="mt-3"><Table rows={offerRows} columns={offerCols} /></div>
          <Formula>Offer acceptance rate = accepted offers (accepted or hired) ÷ (accepted + hired + declined). Offers not yet answered or withdrawn aren't counted.</Formula>
          <CopyCsv columns={offerCols} rows={offerRows} />
        </Card>
      )}

      {tab === 'reqs' && (
        <Card title="Open requisitions by team">
          <Table rows={reqRows} columns={reqCols} empty="No open jobs match these filters." />
          <div className="mt-4"><Table rows={reqJobRows} columns={reqJobCols} empty="" /></div>
          <Formula>Open jobs only. Days open = today − the date the job opened. Open openings = the job's openings not yet closed.</Formula>
          <CopyCsv columns={reqJobCols} rows={reqJobRows} />
        </Card>
      )}
    </Stack>
  )
}
