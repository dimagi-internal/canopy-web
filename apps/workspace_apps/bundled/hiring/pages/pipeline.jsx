// The pipeline: a job's active candidates, one column per stage. Stages come from
// the app's schema, so a stage the workspace adds is a new column with no change
// here. Hired and rejected candidates are counts (and a list under the status
// filter). Tick candidates to move or reject them together.
function arr(x) { return Array.isArray(x) ? x : [] }
function can(app, name, role) {
  const a = ((app.dataModel && app.dataModel._actions) || {})[name]
  if (a && a.enabled === false) return false
  return app.roles.includes(a ? a.role : role)
}
function daysSince(iso) {
  if (!iso) return null
  const t = new Date(iso).getTime()
  return Number.isNaN(t) ? null : Math.max(0, Math.floor((Date.now() - t) / 86400000))
}
function tagList(tags) { return String(tags || '').split(',').map((t) => t.trim()).filter(Boolean) }
const CONFIRM_ABOVE = 30
const NO_REJECT = { reason: '', note: '', email: true, template: '', subject: '', body: '' }

function View({ data, app }) {
  const jobs = arr(data.jobs)
  const apps = arr(data.applications)
  const allTemplates = arr(data.templates)
  const rejectionTemplates = allTemplates.filter((t) => t.kind === 'Rejection')
  const templates = rejectionTemplates.length ? rejectionTemplates : allTemplates
  const canMove = can(app, 'move_stage', 'recruiter')
  const canReject = can(app, 'reject', 'recruiter')
  const canRejectEmail = can(app, 'reject_and_email', 'recruiter')
  const canAct = canMove || canReject || canRejectEmail

  const [jobId, setJobId] = useState(app.params.job ? Number(app.params.job) : 0)
  const [status, setStatus] = useState(app.params.status || 'active')
  const [search, setSearch] = useState('')
  const [selected, setSelected] = useState({})
  const [panel, setPanel] = useState(null) // 'move' | 'reject'
  const [moveTo, setMoveTo] = useState('')
  const [rej, setRej] = useState(NO_REJECT)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)
  const [err, setErr] = useState(null)

  const stageOpts = app.options('application', 'stage')
  const columns = stageOpts.map((o) => o.value).filter((s) => s !== 'Rejected' && s !== 'Hired')
  const nextOf = Object.fromEntries(columns.map((s, i) => [s, columns[i + 1]]))
  const jobTitle = useMemo(() => Object.fromEntries(jobs.map((j) => [j.id, j.title])), [jobs])
  const nameOf = useMemo(() => Object.fromEntries(apps.map((a) => [a.id, a.name])), [apps])
  const forJob = apps.filter((a) => !jobId || a.job === jobId)
  const q = search.trim().toLowerCase()
  const shown = forJob.filter((a) => a.status === status
    && (!q || `${a.name} ${a.email} ${a.tags || ''}`.toLowerCase().includes(q)))
  const ids = Object.keys(selected).filter((k) => selected[k]).map(Number).filter((id) => shown.some((a) => a.id === id))
  const count = (st) => forJob.filter((a) => a.status === st).length

  function toggle(id, on) { setSelected((s) => ({ ...s, [id]: on })) }
  function toggleAll(list, on) { setSelected((s) => ({ ...s, ...Object.fromEntries(list.map((a) => [a.id, on])) })) }
  function clear() { setSelected({}); setPanel(null); setConfirming(false); setRej(NO_REJECT); setMoveTo('') }
  function pickTemplate(id) {
    const t = templates.find((x) => String(x.id) === String(id))
    setRej((r) => ({ ...r, template: id, subject: t ? t.subject : '', body: t ? t.body : '' }))
  }

  async function runBulk(action, opts, label) {
    if (ids.length > CONFIRM_ABOVE && !confirming) { setConfirming(true); return }
    setBusy(true)
    setErr(null)
    try {
      const out = await app.bulk(action, { records: ids, ...opts })
      setResult({ label, done: arr(out && out.done).length, failed: arr(out && out.failed) })
      clear()
      app.refresh()
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }
  const bulkMove = () => moveTo && runBulk('move_stage', { input: { stage: moveTo } },
    `Moved to ${app.optionLabel('application', 'stage', moveTo)}`)
  function bulkReject() {
    const input = { rejection_reason: rej.reason || null, rejection_note: rej.note || null }
    if (rej.email && canRejectEmail) {
      const params = {}
      if (rej.subject) params.subject = rej.subject
      if (rej.body) params.body = rej.body
      return runBulk('reject_and_email', { input, params }, 'Rejected and emailed')
    }
    return runBulk('reject', { input }, 'Rejected')
  }
  async function quickMove(a, stage) {
    setErr(null)
    try { await app.act('move_stage', { record: a.id, input: { stage } }); app.refresh() } catch (e) { setErr(e.message) }
  }

  function card(a) {
    const days = daysSince(a.stage_entered_at || a.created_at)
    return (
      <div key={a.id} className="rounded-md border border-border bg-card p-2 text-sm shadow-sm">
        <div className="flex items-start gap-2">
          {canAct && status === 'active' && (
            <input type="checkbox" className="mt-1" checked={!!selected[a.id]} onChange={(e) => toggle(a.id, e.target.checked)} />
          )}
          <div className="min-w-0 flex-1">
            <button type="button" className="text-left font-medium hover:underline" onClick={() => app.go('candidate', { id: a.id })}>{a.name}</button>
            {!jobId && <div className="truncate text-xs text-muted-foreground">{jobTitle[a.job] || 'No job'}</div>}
            <div className="text-xs text-muted-foreground">
              {app.optionLabel('application', 'source', a.source) || 'Unknown source'}
              {days != null && <> · <span className={days > 14 ? 'text-warning' : ''}>{days}d in stage</span></>}
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              {(a.source === 'Referral' || a.referrer) && <Badge tone="success">Referral</Badge>}
              {a.do_not_email && <Badge tone="warning">Do not email</Badge>}
              {tagList(a.tags).map((t) => <Badge key={t}>{t}</Badge>)}
            </div>
            <Slot name="card_badges" record={a} app={app} />
            {canMove && status === 'active' && nextOf[a.stage] && (
              <div className="mt-1">
                <Button size="sm" onClick={() => quickMove(a, nextOf[a.stage])}>→ {app.optionLabel('application', 'stage', nextOf[a.stage])}</Button>
              </div>
            )}
          </div>
        </div>
      </div>
    )
  }

  return (
    <Stack gap={4}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <Select label="Job" value={String(jobId)} onChange={(v) => { setJobId(Number(v)); setSelected({}) }}
            options={[{ value: '0', label: 'All jobs' },
              ...jobs.filter((j) => !j.is_template).map((j) => ({ value: String(j.id), label: `${j.title}${j.status !== 'open' ? ` (${app.optionLabel('job', 'status', j.status)})` : ''}` }))]} />
          <Select label="Status" value={status} onChange={(v) => { setStatus(v); setSelected({}); setPanel(null) }}
            options={[{ value: 'active', label: `Active (${count('active')})` }, { value: 'hired', label: `Hired (${count('hired')})` },
              { value: 'rejected', label: `Rejected (${count('rejected')})` }]} />
          <Input label="Find" value={search} onChange={setSearch} placeholder="Name, email or tag" />
        </div>
        <div className="flex gap-3">
          <Stat label="Active" value={count('active')} />
          <Stat label="Hired" value={count('hired')} tone="success" />
          <Stat label="Rejected" value={count('rejected')} tone="destructive" />
        </div>
      </div>

      {result && (
        <div className="rounded-md border border-border bg-muted px-3 py-2 text-sm">
          {result.label}: <span className="text-success">{result.done} done</span>
          {result.failed.length > 0 && <> · <span className="text-destructive">{result.failed.length} failed</span>
            <ul className="mt-1 list-disc pl-5 text-xs">{result.failed.map((f) => <li key={f.id}>{nameOf[f.id] || `#${f.id}`}: {f.error}</li>)}</ul></>}
          <button type="button" className="ml-2 text-xs underline" onClick={() => setResult(null)}>dismiss</button>
        </div>
      )}
      {err && <div className="text-sm text-destructive">{err}</div>}

      {canAct && status === 'active' && ids.length > 0 && (
        <Card title={`${ids.length} selected`} actions={<Button size="sm" onClick={clear}>Clear</Button>}>
          <Stack gap={3}>
            <div className="flex flex-wrap gap-2">
              {canMove && <Button tone={panel === 'move' ? 'primary' : 'default'} onClick={() => { setPanel('move'); setConfirming(false) }}>Move to stage…</Button>}
              {(canReject || canRejectEmail) && <Button tone="destructive" onClick={() => { setPanel('reject'); setConfirming(false) }}>Reject…</Button>}
            </div>
            {panel === 'move' && (
              <div className="flex flex-wrap items-end gap-2">
                <Select label="Move to" value={moveTo} onChange={setMoveTo}
                  options={[{ value: '', label: 'Choose a stage…' }, ...columns.map((s) => ({ value: s, label: app.optionLabel('application', 'stage', s) }))]} />
                <Button tone="primary" disabled={busy || !moveTo} onClick={bulkMove}>Move {ids.length}</Button>
              </div>
            )}
            {panel === 'reject' && (
              <Stack gap={3}>
                <Grid cols={2}>
                  <Select label="Reason" value={rej.reason} onChange={(reason) => setRej((r) => ({ ...r, reason }))}
                    options={[{ value: '', label: 'Choose a reason…' }, ...app.options('application', 'rejection_reason')]} />
                  <Input label="Note (team only)" value={rej.note} onChange={(note) => setRej((r) => ({ ...r, note }))} />
                </Grid>
                {canRejectEmail && (
                  <label className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={rej.email} onChange={(e) => setRej((r) => ({ ...r, email: e.target.checked }))} />
                    Email each candidate (skipped for anyone marked do-not-email)
                  </label>
                )}
                {canRejectEmail && rej.email && (
                  <Stack gap={2}>
                    <Select label="Email template" value={rej.template} onChange={pickTemplate}
                      options={[{ value: '', label: 'The app’s standard rejection email' }, ...templates.map((t) => ({ value: String(t.id), label: t.name }))]} />
                    {rej.template && <Input label="Subject" value={rej.subject} onChange={(subject) => setRej((r) => ({ ...r, subject }))} />}
                    {rej.template && <TextArea label="Email" rows={6} value={rej.body} onChange={(body) => setRej((r) => ({ ...r, body }))} />}
                    <div className="text-xs text-muted-foreground">{'{{name}}'}, {'{{job.title}}'} and other fields are filled in for each candidate when the email is sent.</div>
                  </Stack>
                )}
                <div><Button tone="destructive" disabled={busy} onClick={bulkReject}>{rej.email && canRejectEmail ? `Reject ${ids.length} and send email` : `Reject ${ids.length}`}</Button></div>
              </Stack>
            )}
            {confirming && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm">
                <div className="font-medium text-destructive">You are about to {panel === 'reject' ? 'reject' : 'move'} {ids.length} candidates at once.</div>
                <div className="mt-2 flex gap-2">
                  <Button tone="destructive" disabled={busy} onClick={panel === 'reject' ? bulkReject : bulkMove}>Yes, {panel === 'reject' ? 'reject' : 'move'} {ids.length}</Button>
                  <Button onClick={() => setConfirming(false)}>Go back</Button>
                </div>
              </div>
            )}
            {busy && <Loading />}
          </Stack>
        </Card>
      )}

      {status === 'active' ? (
        <div className="overflow-x-auto pb-2">
          <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.max(columns.length, 1)}, minmax(190px, 1fr))`, minWidth: `${columns.length * 200}px` }}>
            {columns.map((stage) => {
              const here = shown.filter((a) => a.stage === stage)
              const allOn = here.length > 0 && here.every((a) => selected[a.id])
              return (
                <div key={stage} className="rounded-lg border border-border bg-muted p-2">
                  <div className="mb-2 flex items-center justify-between gap-2 px-1 text-sm font-semibold">
                    <span className="flex items-center gap-2">
                      {canAct && here.length > 0 && <input type="checkbox" checked={allOn} onChange={(e) => toggleAll(here, e.target.checked)} />}
                      {app.optionLabel('application', 'stage', stage)}
                    </span>
                    <Badge>{here.length}</Badge>
                  </div>
                  <Stack gap={2}>
                    {here.map(card)}
                    {here.length === 0 && <div className="px-1 py-3 text-center text-xs text-muted-foreground">—</div>}
                  </Stack>
                  {stage === 'Applied' && here.length > 0 && (
                    <button type="button" className="mt-2 w-full text-xs text-primary hover:underline" onClick={() => app.go('review', jobId ? { job: jobId } : {})}>Review these one by one →</button>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      ) : (
        <Card title={status === 'hired' ? 'Hired' : 'Rejected'}>
          <Table rows={shown} empty="Nobody here." columns={[
            { key: 'name', label: 'Name', render: (a) => <button type="button" className="text-left hover:underline" onClick={() => app.go('candidate', { id: a.id })}>{a.name}</button> },
            { key: 'job', label: 'Job', render: (a) => jobTitle[a.job] || '' },
            { key: 'source', label: 'Source' },
            ...(status === 'rejected'
              ? [{ key: 'rejection_reason', label: 'Reason' }, { key: 'rejected_on', label: 'Rejected on' }]
              : [{ key: 'hired_on', label: 'Hired on' }]),
          ]} />
        </Card>
      )}
    </Stack>
  )
}
