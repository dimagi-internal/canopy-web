// Application review: new applicants one at a time, oldest first, with referrals
// and internal applicants at the front. A advances to the next stage, R opens the
// reject panel, S skips to the next one. Each decision loads the next applicant.
function arr(x) { return Array.isArray(x) ? x : [] }
function can(app, name, role) {
  const a = ((app.dataModel && app.dataModel._actions) || {})[name]
  if (a && a.enabled === false) return false
  return app.roles.includes(a ? a.role : role)
}
function domainOf(email) { return String(email || '').toLowerCase().split('@')[1] || '' }
function isReferral(a) { return a.source === 'Referral' || !!a.referrer }
function ago(iso) {
  if (!iso) return ''
  const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
  return days <= 0 ? 'today' : days === 1 ? 'yesterday' : `${days} days ago`
}
const DELAYS = [{ value: '0', label: 'Send now' }, { value: '24', label: 'Send tomorrow (in 24 hours)' }, { value: '72', label: 'Send in 3 days' }]
const BUILT_IN = ['job', 'name', 'email', 'phone', 'location', 'country', 'region', 'linkedin', 'resume', 'resume_url',
  'cover_letter', 'source', 'source_tag', 'referrer', 'credited_to', 'stage', 'status', 'tags']

function View({ data, app, scope }) {
  const jobs = arr(data.jobs)
  const pending = arr(data.applications)
  const allTemplates = arr(data.templates)
  const templates = allTemplates.filter((t) => t.kind === 'Rejection').length ? allTemplates.filter((t) => t.kind === 'Rejection') : allTemplates
  const viewer = (scope && scope.viewer && scope.viewer.email) || ''
  const ownDomain = domainOf(viewer)
  const isInternal = (a) => !!ownDomain && domainOf(a.email) === ownDomain
  const canMove = can(app, 'move_stage', 'recruiter')
  const canReject = can(app, 'reject', 'recruiter')
  const canRejectEmail = can(app, 'reject_and_email', 'recruiter')
  const canNote = can(app, 'add_note', 'interviewer')

  const [jobId, setJobId] = useState(app.params.job ? Number(app.params.job) : 0)
  const [priority, setPriority] = useState(true)
  const [decided, setDecided] = useState({}) // id -> what happened, this session
  const [skips, setSkips] = useState({}) // id -> how many times skipped
  const [rejecting, setRejecting] = useState(false)
  const [rej, setRej] = useState({ reason: '', note: '', email: true, template: '', subject: '', body: '', delay: '0' })
  const [target, setTarget] = useState('')
  const [dupes, setDupes] = useState([])
  const [feedback, setFeedback] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState(null)
  const [err, setErr] = useState(null)

  const jobTitle = useMemo(() => Object.fromEntries(jobs.map((j) => [j.id, j.title])), [jobs])
  const flow = app.options('application', 'stage').map((o) => o.value).filter((s) => s !== 'Rejected')
  const nextStage = flow[flow.indexOf('Applied') + 1] || flow.find((s) => s !== 'Applied' && s !== 'Hired') || ''
  const moveTo = target || nextStage

  const queue = useMemo(() => {
    const rank = (a) => (priority ? (isReferral(a) ? 0 : isInternal(a) ? 1 : 2) : 0)
    return pending
      .filter((a) => !decided[a.id] && (!jobId || a.job === jobId))
      .sort((x, y) => (skips[x.id] || 0) - (skips[y.id] || 0) || rank(x) - rank(y)
        || String(x.created_at || '').localeCompare(String(y.created_at || '')))
  }, [pending, decided, skips, jobId, priority, ownDomain])
  const cur = queue[0] || null
  const curId = cur ? cur.id : null

  const countsByJob = useMemo(() => {
    const c = {}
    for (const a of pending) if (!decided[a.id]) c[a.job] = (c[a.job] || 0) + 1
    return c
  }, [pending, decided])

  // A new applicant on screen: reset the panels and look for other applications with the same email.
  useEffect(() => {
    setRejecting(false)
    setTarget('')
    setFeedback('')
    setErr(null)
    setDupes([])
    if (!cur || !cur.email) return
    let live = true
    app.query('application', { where: { email: cur.email }, limit: 20 })
      .then((rows) => { if (live) setDupes(arr(rows).filter((r) => r.id !== cur.id)) })
      .catch(() => {})
    return () => { live = false }
  }, [curId])

  function pickTemplate(id) {
    const t = templates.find((x) => String(x.id) === String(id))
    setRej((r) => ({ ...r, template: id, subject: t ? t.subject : '', body: t ? t.body : '' }))
  }

  async function decide(kind, fn) {
    if (!cur || busy) return
    setBusy(true)
    setErr(null)
    try {
      const out = await fn()
      const sent = ((out && out._effects) || [])[0]
      setNotice(`${cur.name}: ${kind}${sent ? ` · email to ${sent.to} ${sent.status}` : ''}.`)
      setDecided((d) => ({ ...d, [cur.id]: kind }))
      app.refresh()
    } catch (e) {
      setErr(e.message)
    } finally {
      setBusy(false)
    }
  }
  const advance = () => moveTo && decide(`moved to ${app.optionLabel('application', 'stage', moveTo)}`,
    () => app.act('move_stage', { record: cur.id, input: { stage: moveTo } }))
  const reject = (withEmail) => decide(withEmail ? 'rejected and emailed' : 'rejected', () => {
    const input = { rejection_reason: rej.reason || null, rejection_note: rej.note || null }
    if (!withEmail) return app.act('reject', { record: cur.id, input })
    const params = {}
    if (rej.subject) params.subject = rej.subject
    if (rej.body) params.body = rej.body
    if (Number(rej.delay) > 0) params.delay_hours = Number(rej.delay)
    return app.act('reject_and_email', { record: cur.id, input, params })
  })
  const skip = () => { if (cur) setSkips((s) => ({ ...s, [cur.id]: (s[cur.id] || 0) + 1 })) }
  async function leaveFeedback() {
    if (!feedback.trim() || !cur) return
    try {
      await app.act('add_note', { input: { application: cur.id, body: feedback.trim() } })
      setFeedback('')
      setNotice(`Note added to ${cur.name}.`)
    } catch (e) { setErr(e.message) }
  }

  // Keyboard flow. The handler reads the latest state through a ref.
  const keys = useRef(null)
  keys.current = { cur, rejecting, busy, canMove, canReject: canReject || canRejectEmail, advance, skip,
    openReject: () => setRejecting(true), closeReject: () => setRejecting(false) }
  useEffect(() => {
    function onKey(e) {
      const k = keys.current
      const tag = (e.target && e.target.tagName) || ''
      if (e.key === 'Escape' && k.rejecting) { k.closeReject(); return }
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || e.metaKey || e.ctrlKey || e.altKey || !k.cur || k.busy) return
      const key = e.key.toLowerCase()
      if (key === 'a' && k.canMove && !k.rejecting) { e.preventDefault(); k.advance() }
      else if (key === 'r' && k.canReject) { e.preventDefault(); k.openReject() }
      else if (key === 's') { e.preventDefault(); k.skip() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const reviewed = Object.keys(decided).length
  const jobOptions = [{ value: '0', label: `All jobs (${pending.length - reviewed})` },
    ...jobs.filter((j) => countsByJob[j.id] || j.id === jobId)
      .map((j) => ({ value: String(j.id), label: `${j.title} (${countsByJob[j.id] || 0})` }))]

  return (
    <Stack gap={4}>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="flex flex-wrap items-end gap-3">
          <Select label="Job" value={String(jobId)} onChange={(v) => setJobId(Number(v))} options={jobOptions} />
          <label className="flex items-center gap-2 pb-1.5 text-sm text-muted-foreground">
            <input type="checkbox" checked={priority} onChange={(e) => setPriority(e.target.checked)} />
            Referrals and internal applicants first
          </label>
        </div>
        <div className="flex gap-3">
          <Stat label="To review" value={queue.length} />
          <Stat label="Reviewed now" value={reviewed} tone={reviewed ? 'success' : 'default'} />
        </div>
      </div>
      {notice && <div className="rounded-md border border-border bg-muted px-3 py-2 text-sm">{notice}</div>}
      {!cur ? (
        <Card><Empty>{pending.length ? 'Nothing left to review for this job.' : 'No new applications to review.'}</Empty></Card>
      ) : (
        <div className="grid gap-4" style={{ gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)' }}>
          <Stack gap={4}>
            <Card title={<span>{cur.name}</span>} actions={<Button size="sm" onClick={() => app.go('candidate', { id: cur.id })}>Open profile</Button>}>
              <Stack gap={3}>
                <div className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="text-muted-foreground">{jobTitle[cur.job] || 'No job'}</span>
                  {isReferral(cur) && <Badge tone="success">Referral{cur.referrer ? ` · ${cur.referrer}` : ''}</Badge>}
                  {isInternal(cur) && <Badge tone="success">Internal</Badge>}
                  {dupes.length > 0 && <Badge tone="warning">Possible duplicate</Badge>}
                  {String(cur.tags || '').split(',').map((t) => t.trim()).filter(Boolean).map((t) => <Badge key={t}>{t}</Badge>)}
                </div>
                <div className="text-sm text-muted-foreground">
                  {[cur.email, cur.phone, cur.location, cur.country].filter(Boolean).join(' · ')}
                </div>
                <div className="text-xs text-muted-foreground">
                  Applied {ago(cur.created_at)} via {app.optionLabel('application', 'source', cur.source) || 'unknown'}
                  {cur.source_tag ? ` (${cur.source_tag})` : ''}{cur.region ? ` · privacy region ${cur.region}` : ''}
                </div>
                <div className="flex flex-wrap gap-2">
                  {cur.resume && <Button size="sm" tone="primary" onClick={() => app.openFile(cur.resume)}>Open résumé</Button>}
                  {cur.resume_url && <a className="text-sm text-primary hover:underline" href={cur.resume_url}>Résumé link</a>}
                  {cur.linkedin && <a className="text-sm text-primary hover:underline" href={cur.linkedin}>LinkedIn</a>}
                  {!cur.resume && !cur.resume_url && <span className="text-sm text-muted-foreground">No résumé attached.</span>}
                </div>
              </Stack>
            </Card>
            {dupes.length > 0 && (
              <Card title="Other applications with this email">
                <Stack gap={1}>
                  {dupes.map((d) => (
                    <div key={d.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
                      <button type="button" className="text-left hover:underline" onClick={() => app.go('candidate', { id: d.id })}>
                        {d.name} · {jobTitle[d.job] || (d.kind === 'prospect' ? 'Prospect' : 'No job')}
                      </button>
                      <span className="flex gap-1">
                        <Badge>{app.optionLabel('application', 'stage', d.stage)}</Badge>
                        <Badge tone={d.status === 'rejected' ? 'destructive' : d.status === 'hired' ? 'success' : 'default'}>{app.optionLabel('application', 'status', d.status)}</Badge>
                      </span>
                    </div>
                  ))}
                </Stack>
              </Card>
            )}
            <Card title="Cover letter">
              <div className="whitespace-pre-wrap text-sm">{cur.cover_letter || <span className="text-muted-foreground">None.</span>}</div>
            </Card>
            <FieldValues collection="application" record={cur} exclude={BUILT_IN} />
            <Slot name="review_extra" record={cur} app={app} />
          </Stack>
          <Stack gap={4}>
            <Card title="Decision">
              <Stack gap={3}>
                {canMove && (
                  <Stack gap={2}>
                    <Select label="Advance to" value={moveTo} onChange={setTarget}
                      options={flow.filter((s) => s !== 'Applied' && s !== 'Hired').map((s) => ({ value: s, label: app.optionLabel('application', 'stage', s) }))} />
                    <Button tone="primary" disabled={busy || !moveTo} onClick={advance}>Advance (A)</Button>
                  </Stack>
                )}
                {(canReject || canRejectEmail) && !rejecting && <Button tone="destructive" disabled={busy} onClick={() => setRejecting(true)}>Reject… (R)</Button>}
                <Button disabled={busy} onClick={skip}>Skip for now (S)</Button>
                {!canMove && !canReject && <div className="text-xs text-muted-foreground">A recruiter advances or rejects. You can leave a note for them below.</div>}
                {err && <div className="text-sm text-destructive">{err}</div>}
              </Stack>
            </Card>
            {rejecting && (
              <Card title={`Reject ${cur.name}`}>
                <Stack gap={3}>
                  <Select label="Reason" value={rej.reason} onChange={(reason) => setRej((r) => ({ ...r, reason }))}
                    options={[{ value: '', label: 'Choose a reason…' }, ...app.options('application', 'rejection_reason')]} />
                  <TextArea label="Note (team only)" rows={2} value={rej.note} onChange={(note) => setRej((r) => ({ ...r, note }))} />
                  {canRejectEmail && (
                    <>
                      <Select label="Email template" value={rej.template} onChange={pickTemplate}
                        options={[{ value: '', label: 'The app’s standard rejection email' }, ...templates.map((t) => ({ value: String(t.id), label: t.name }))]} />
                      {rej.template && <Input label="Subject" value={rej.subject} onChange={(subject) => setRej((r) => ({ ...r, subject }))} />}
                      {rej.template && <TextArea label="Email" rows={6} value={rej.body} onChange={(body) => setRej((r) => ({ ...r, body }))} />}
                      <Select label="When" value={rej.delay} onChange={(delay) => setRej((r) => ({ ...r, delay }))} options={DELAYS} />
                      {Number(rej.delay) > 0 && <div className="text-xs text-muted-foreground">Held back, and not sent if you move them back into the process first.</div>}
                      {cur.do_not_email && <div className="text-xs text-warning">This candidate is marked do-not-email: no email will go.</div>}
                    </>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {canRejectEmail && <Button tone="destructive" disabled={busy} onClick={() => reject(true)}>Reject and send email</Button>}
                    {canReject && <Button disabled={busy} onClick={() => reject(false)}>Reject, no email</Button>}
                    <Button onClick={() => setRejecting(false)}>Cancel (Esc)</Button>
                  </div>
                </Stack>
              </Card>
            )}
            {canNote && (
              <Card title="Feedback for the team">
                <Stack gap={2}>
                  <TextArea rows={3} value={feedback} onChange={setFeedback} placeholder="A note on this applicant (everyone on the team sees it)" />
                  <div><Button size="sm" disabled={!feedback.trim()} onClick={leaveFeedback}>Add note</Button></div>
                </Stack>
              </Card>
            )}
            <div className="text-xs text-muted-foreground">Keys: A advance · R reject · S skip · Esc close the reject panel. {queue.length - 1} more after this one.</div>
          </Stack>
        </div>
      )}
    </Stack>
  )
}
