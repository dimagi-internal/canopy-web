// Voluntary self-identification TOTALS for the compliance role — never a list of
// people. US applicants may answer four optional questions; the answers are
// readable only with the compliance role, so hiring teams never see them. Every
// count here is a total over a group, and any count under 5 is shown as "<5" so
// nobody can be picked out. "Reached" a stage means the candidate was ever in
// that stage or a later one (from their stage history).
const QUESTIONS = [
  ['eeo_gender', 'Gender'],
  ['eeo_race', 'Race / ethnicity'],
  ['eeo_veteran', 'Veteran status'],
  ['eeo_disability', 'Disability'],
]
const NOT_ANSWERED = 'Not answered'
const MIN_CELL = 5

function shown(n) {
  return n < MIN_CELL ? '<5' : String(n)
}

function View({ data, app }) {
  const isCompliance = app.roles.includes('compliance')
  const failed = data.applications && !Array.isArray(data.applications) ? data.applications.error : null
  const apps = Array.isArray(data.applications) ? data.applications : []
  const jobs = (Array.isArray(data.jobs) ? data.jobs : []).filter((j) => !j.is_template)
  const [jobId, setJobId] = useState('')
  const [byJobQ, setByJobQ] = useState('eeo_gender')
  const [history, setHistory] = useState(null)

  useEffect(() => {
    app.query('stage_change', { limit: 2000 }).then(setHistory, () => setHistory([]))
  }, [])

  const stageList = (() => {
    const o = app.options('application', 'stage').map((x) => x.value)
    return (o.length ? o : app.field('application', 'stage')?.options || []).filter((s) => s !== 'Rejected')
  })()
  const idx = (s) => stageList.indexOf(s)
  const furthest = useMemo(() => {
    const out = {}
    for (const a of apps) out[a.id] = a.stage && a.stage !== 'Rejected' ? idx(a.stage) : 0
    for (const c of history || []) if (c.application in out) out[c.application] = Math.max(out[c.application], idx(c.to_stage))
    return out
  }, [apps, history, stageList.join('|')])
  const reach = (name) => (idx(name) >= 0 ? idx(name) : Infinity)
  const STAGES = [
    ['Applied', () => true],
    ['Interviewed', (a) => furthest[a.id] >= reach('Interview')],
    ['Offered', (a) => furthest[a.id] >= reach('Offer')],
    ['Hired', (a) => a.status === 'hired'],
  ]

  if (!isCompliance) {
    return <Card title="Self-ID totals"><Empty>Only people with the compliance role can see these totals.</Empty></Card>
  }
  if (failed) {
    return <Card title="Self-ID totals"><Empty>The answers couldn't be read: {failed}</Empty></Card>
  }

  const answers = (q) => {
    const opts = app.options('application', q).map((o) => o.value)
    return [...(opts.length ? opts : app.field('application', q)?.options || []), NOT_ANSWERED]
  }
  const answerOf = (a, q) => (a[q] == null || a[q] === '' ? NOT_ANSWERED : a[q])
  const mine = apps.filter((a) => !jobId || a.job === Number(jobId))
  const us = mine.filter((a) => a.region === 'US')
  const asked = (a) => QUESTIONS.some(([q]) => a[q] != null && a[q] !== '')

  return (
    <Stack gap={4}>
      <div className="rounded-lg border border-border bg-muted p-3 text-sm">
        <strong>Totals only.</strong> These answers are voluntary and only the compliance role can read them — recruiters,
        hiring managers and interviewers never see them, on this page or on a candidate. Any count under {MIN_CELL} is shown
        as “&lt;5” so no one can be identified. The questions are asked only of applicants in the United States.
      </div>
      <div className="flex flex-wrap items-end gap-4">
        <div className="min-w-[14rem]">
          <Select label="Job" value={jobId} onChange={setJobId}
            options={[{ value: '', label: 'All jobs' }, ...jobs.map((j) => ({ value: String(j.id), label: j.title }))]} />
        </div>
        <Grid cols={3}>
          <Stat label="Applicants" value={shown(mine.length)} />
          <Stat label="US applicants (asked)" value={shown(us.length)} />
          <Stat label="Answered at least one" value={shown(mine.filter(asked).length)} />
        </Grid>
      </div>
      {apps.length >= 2000 && <p className="text-sm text-warning">Only the first 500 applicants are counted.</p>}

      <Grid cols={2}>
        {QUESTIONS.map(([q, label]) => (
          <Card key={q} title={`${label} — by stage reached`}>
            <Table
              rows={answers(q).map((ans) => {
                const row = { answer: ans === NOT_ANSWERED ? ans : app.optionLabel('application', q, ans) || ans }
                for (const [st, test] of STAGES) row[st] = shown(mine.filter((a) => answerOf(a, q) === ans && test(a)).length)
                return row
              })}
              columns={[{ key: 'answer', label: 'Answer' }, ...STAGES.map(([st]) => ({ key: st, label: st, align: 'right' }))]}
            />
          </Card>
        ))}
      </Grid>

      <Card title="By job (applicants)" actions={
        <select aria-label="Question" value={byJobQ} onChange={(e) => setByJobQ(e.target.value)}
          className="rounded-md border border-border bg-background px-2 py-1 text-xs">
          {QUESTIONS.map(([q, l]) => <option key={q} value={q}>{l}</option>)}
        </select>
      }>
        <Table
          rows={jobs.map((j) => {
            const js = apps.filter((a) => a.job === j.id)
            const row = { job: j.title, total: shown(js.length) }
            for (const ans of answers(byJobQ)) row[ans] = shown(js.filter((a) => answerOf(a, byJobQ) === ans).length)
            return row
          })}
          columns={[{ key: 'job', label: 'Job' }, { key: 'total', label: 'Applicants', align: 'right' },
            ...answers(byJobQ).map((ans) => ({ key: ans, label: ans === NOT_ANSWERED ? ans : app.optionLabel('application', byJobQ, ans) || ans, align: 'right' }))]}
          empty="No jobs yet."
        />
      </Card>
      <p className="text-xs text-muted-foreground">
        Interviewed = reached the Interview stage or later; Offered = reached the Offer stage or later; Hired = marked hired.
        Rejected candidates count at the furthest stage they reached. “Not answered” includes everyone who wasn't asked.
      </p>
    </Stack>
  )
}
