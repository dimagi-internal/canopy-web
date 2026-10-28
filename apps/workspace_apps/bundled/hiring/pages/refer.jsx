// For anyone in the workspace: refer someone for an open job, and see the people
// you've referred. A referral creates the candidate in the job's pipeline with
// source "Referral" and you credited. Where the viewer may read candidates, each
// referral also shows how far the candidate has got.
const REFER_BUILT_IN = ['job', 'name', 'email', 'linkedin', 'note']

function View({ data, app, scope }) {
  // Only roles with a live public post: a confidential search is never shown to the whole workspace.
  const jobs = (Array.isArray(data.jobs) ? data.jobs : []).filter((j) => !j.confidential && !j.is_template && j.post_status !== 'offline')
  const referrals = Array.isArray(data.referrals) ? data.referrals : []
  const me = String(scope?.viewer?.email || '').toLowerCase()
  const mine = referrals.filter((r) => String(r.referrer || '').toLowerCase() === me)
    .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))
  const jobTitle = Object.fromEntries(jobs.map((j) => [j.id, j.title]))
  const [form, setForm] = useState({ job: app.params?.job ? String(app.params.job) : '' })
  const [state, setState] = useState({ kind: 'idle' })
  const [extraJobs, setExtraJobs] = useState({})
  const [progress, setProgress] = useState(null)
  const set = (k) => (v) => setForm((f) => ({ ...f, [k]: v }))

  // Referrals for jobs that have since closed aren't in the open-jobs source.
  const missing = [...new Set(mine.map((r) => r.job).filter((id) => id && !jobTitle[id]))]
  useEffect(() => {
    if (missing.length === 0) return
    app.query('job', { where: { id: missing }, limit: 2000 })
      .then((rows) => setExtraJobs(Object.fromEntries(rows.map((j) => [j.id, j.title]))), () => {})
  }, [missing.join(',')])
  // Candidate stages are readable only by the hiring team; others just see the referral.
  useEffect(() => {
    if (!me) return
    app.query('application', { where: { referrer: scope.viewer.email }, limit: 2000 })
      .then((rows) => setProgress(rows), () => setProgress(null))
  }, [me, mine.length])

  function statusOf(r) {
    if (!progress) return null
    const a = progress.find((x) => (r.application && x.id === r.application)
      || (String(x.email || '').toLowerCase() === String(r.email || '').toLowerCase() && x.job === r.job))
    if (!a) return <span className="text-muted-foreground">—</span>
    if (a.status === 'hired') return <Badge tone="success">Hired</Badge>
    if (a.status === 'rejected') return <Badge>Not moving forward</Badge>
    return <Badge tone="warning">{app.optionLabel('application', 'stage', a.stage) || a.stage}</Badge>
  }

  async function submit() {
    setState({ kind: 'sending' })
    const input = { ...form, job: Number(form.job) }
    for (const k of Object.keys(input)) if (input[k] === '' || input[k] == null) delete input[k]
    try {
      await app.act('refer', { input })
      setForm({ job: '' })
      setState({ kind: 'sent', name: input.name, job: jobTitle[input.job] })
      app.refresh()
    } catch (err) {
      setState({ kind: 'failed', message: err.message })
    }
  }

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,26rem)_1fr]">
      <Card title="Refer someone">
        {jobs.length === 0 ? (
          <Empty>There are no open jobs to refer people to right now.</Empty>
        ) : (
          <Form onSubmit={submit}>
            <Stack gap={3}>
              <p className="text-sm text-muted-foreground">
                Know someone who'd be great? Tell us who and why — they join the job's pipeline as a referral, credited to you.
              </p>
              <Select label="Job" value={form.job} onChange={set('job')}
                options={[{ value: '', label: 'Choose an open job…' },
                  ...jobs.map((j) => ({ value: String(j.id), label: [j.title, j.department].filter(Boolean).join(' — ') }))]} />
              <Input label="Their name" required value={form.name} onChange={set('name')} />
              <Input label="Their email" type="email" required value={form.email} onChange={set('email')} />
              <Input label="LinkedIn" type="url" placeholder="https://linkedin.com/in/…" value={form.linkedin} onChange={set('linkedin')} />
              <TextArea label="Why they'd be great" rows={4} value={form.note} onChange={set('note')}
                placeholder="How you know them, what they're good at, why this role" />
              <FieldInputs collection="referral" action="refer" exclude={REFER_BUILT_IN} values={form} onChange={setForm} />
              <p className="text-xs text-muted-foreground">Please let them know you're referring them, so a recruiter's email isn't a surprise.</p>
              {state.kind === 'failed' && <p role="alert" className="text-sm text-destructive">{state.message}</p>}
              {state.kind === 'sent' && (
                <p role="status" className="text-sm text-success">Thanks — {state.name} is referred{state.job ? ` for ${state.job}` : ''}.</p>
              )}
              <div>
                <Button type="submit" tone="primary" disabled={state.kind === 'sending' || !form.job}>
                  {state.kind === 'sending' ? 'Sending…' : 'Refer them'}
                </Button>
              </div>
            </Stack>
          </Form>
        )}
      </Card>

      <Card title={`My referrals (${mine.length})`}>
        <Table
          rows={mine}
          empty="You haven't referred anyone yet."
          columns={[
            { key: 'name', label: 'Person', render: (r) => (
              <div><div className="font-medium">{r.name}</div><div className="text-xs text-muted-foreground">{r.email}</div></div>
            ) },
            { key: 'job', label: 'For', render: (r) => jobTitle[r.job] || extraJobs[r.job] || <span className="text-muted-foreground">A job no longer listed</span> },
            { key: 'created_at', label: 'Referred', render: (r) => String(r.created_at || '').slice(0, 10) },
            ...(progress ? [{ key: 'status', label: 'Where they are', render: statusOf }] : []),
          ]}
        />
        {!progress && mine.length > 0 && (
          <p className="mt-3 text-xs text-muted-foreground">How each referral is progressing is visible to the hiring team — ask the job's recruiter for an update.</p>
        )}
      </Card>
    </div>
  )
}
