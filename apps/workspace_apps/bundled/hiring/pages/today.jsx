// Today: what needs me, across every page, in one list — my interviews today,
// the scorecards I owe, approvals waiting on me, and (for a recruiter or a
// hiring manager) applicants to review, candidates who have sat in a stage too
// long, offers to send and offers waiting on an answer. Each item opens where
// it is done. Reads only what this person may see (per-job access applies).
function arr(x) { return Array.isArray(x) ? x : [] }
function people(text) { return String(text || '').split(/[,;\s]+/).map((e) => e.trim().toLowerCase()).filter(Boolean) }
function localDay(offsetDays) {
  const d = new Date(Date.now() + (offsetDays || 0) * 86400000)
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
}
function daysSince(iso) {
  if (!iso) return 0
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000)
}
const STUCK_DAYS = 14
const OFFER_WAIT_DAYS = 5
const PICK_WAIT_DAYS = 3

function Item({ title, detail, action, onClick, tone }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 py-2 text-sm">
      <div className="min-w-0">
        <div className="font-medium">{title}</div>
        {detail && <div className="text-xs text-muted-foreground">{detail}</div>}
      </div>
      <Button size="sm" tone={tone || 'default'} onClick={onClick}>{action}</Button>
    </div>
  )
}

function Section({ title, items, more }) {
  if (!items.length) return null
  return (
    <Card title={<span>{title} <span className="text-muted-foreground">({items.length})</span></span>}>
      <div className="divide-y divide-border">{items.slice(0, 8)}</div>
      {items.length > 8 && more}
    </Card>
  )
}

function View({ data, app, scope }) {
  const me = String((scope && scope.viewer && scope.viewer.email) || '').toLowerCase()
  const firstName = String((scope && scope.viewer && scope.viewer.name) || '').split(' ')[0]
  const jobs = arr(data.jobs)
  const applications = arr(data.applications)
  const interviews = arr(data.interviews)
  const scorecards = arr(data.scorecards)
  const approvals = arr(data.approvals)
  const offers = arr(data.offers)
  const recruiter = app.roles.includes('recruiter')
  const today = localDay(0)

  const job = useMemo(() => Object.fromEntries(jobs.map((j) => [j.id, j])), [jobs])
  const cand = useMemo(() => Object.fromEntries(applications.map((a) => [a.id, a])), [applications])
  const myJobs = new Set(jobs.filter((j) => [j.hiring_manager, j.recruiter, j.coordinator].map((e) => String(e || '').toLowerCase()).includes(me)
    || people(j.hiring_team).includes(me)).map((j) => j.id))
  const managed = new Set(jobs.filter((j) => String(j.hiring_manager || '').toLowerCase() === me).map((j) => j.id))
  // A recruiter sees the jobs they are named on, or every job (the default when they are named on none).
  const [everyJob, setEveryJob] = useState(false)
  const allJobs = recruiter && (everyJob || myJobs.size === 0)
  const mine = (jobId) => allJobs || myJobs.has(jobId)
  const who = (appId) => (cand[appId] && cand[appId].name) || 'A candidate'
  const titleOf = (jobId) => (job[jobId] && job[jobId].title) || ''

  // My interviews today, and the scorecards I still owe.
  const myInterviews = interviews.filter((x) => people(x.interviewers).includes(me))
  const todays = myInterviews.filter((x) => x.status === 'Scheduled' && x.date === today)
    .sort((x, y) => String(x.time || '').localeCompare(String(y.time || '')))
  const wrote = (x) => scorecards.some((s) => s.interview === x.id && String(s.interviewer || '').toLowerCase() === me)
  const owed = myInterviews.filter((x) => (x.status === 'Done' || (x.status === 'Scheduled' && x.date && x.date < today)) && !wrote(x))
    .sort((x, y) => String(x.date || '').localeCompare(String(y.date || '')))

  // Approvals waiting on me.
  const toApprove = approvals.filter((a) => a.status === 'pending' && String(a.approver || '').toLowerCase() === me)

  // Applicants to review, by job (the jobs I work on; a recruiter with none: every job).
  const newByJob = {}
  for (const a of applications) {
    if (a.kind === 'applicant' && a.stage === 'Applied' && a.status === 'active' && (recruiter || managed.has(a.job)) && mine(a.job)) {
      newByJob[a.job] = (newByJob[a.job] || 0) + 1
    }
  }
  // Hiring-manager review: candidates waiting on the hiring manager.
  const forHm = applications.filter((a) => a.status === 'active' && a.stage === 'Hiring manager review' && managed.has(a.job))
  // Candidates sitting in one stage too long.
  const stuck = recruiter ? applications.filter((a) => a.status === 'active' && a.kind === 'applicant'
    && !['Applied', 'Offer', 'Hired', 'Rejected'].includes(a.stage) && mine(a.job)
    && daysSince(a.stage_entered_at || a.updated_at || a.created_at) >= STUCK_DAYS)
    .sort((x, y) => String(x.stage_entered_at || '').localeCompare(String(y.stage_entered_at || ''))) : []
  // Interviews where the candidate has not picked a time yet.
  const unpicked = recruiter ? interviews.filter((x) => x.status === 'Requested' && mine(x.job)
    && daysSince(x.updated_at || x.created_at) >= PICK_WAIT_DAYS) : []
  // Offers to send, and offers out with no answer.
  const toSend = recruiter ? offers.filter((o) => o.status === 'approved' && mine(o.job)) : []
  const waiting = recruiter ? offers.filter((o) => o.status === 'sent' && daysSince(o.sent_on) >= OFFER_WAIT_DAYS && mine(o.job)) : []

  const sections = [
    ['Your interviews today', todays.map((x) => (
      <Item key={`i${x.id}`} title={`${x.time || ''} ${who(x.application)}`.trim()} detail={[app.optionLabel('interview', 'kind', x.kind), titleOf(x.job)].filter(Boolean).join(' · ')}
        action="Open the kit" tone="primary" onClick={() => app.go('kit', { id: x.id })} />
    ))],
    ['Scorecards you owe', owed.map((x) => (
      <Item key={`s${x.id}`} title={who(x.application)} detail={[app.optionLabel('interview', 'kind', x.kind), x.date, titleOf(x.job)].filter(Boolean).join(' · ')}
        action="Write scorecard" tone="primary" onClick={() => app.go('kit', { id: x.id })} />
    ))],
    ['Waiting for your approval', toApprove.map((a) => (
      <Item key={`a${a.id}`} title={a.summary || `${a.kind} approval`} detail={a.kind === 'Offer' ? 'An offer' : titleOf(a.job)}
        action="Review" tone="primary" onClick={() => app.go('approvals')} />
    ))],
    ['Waiting on you as hiring manager', forHm.map((a) => (
      <Item key={`h${a.id}`} title={a.name} detail={titleOf(a.job)} action="Open" onClick={() => app.go('candidate', { id: a.id })} />
    ))],
    ['New applicants to review', Object.entries(newByJob).sort((x, y) => y[1] - x[1]).map(([jobId, n]) => (
      <Item key={`n${jobId}`} title={titleOf(Number(jobId)) || 'No job'} detail={`${n} new ${n === 1 ? 'applicant' : 'applicants'}`}
        action="Review" onClick={() => app.go('review', { job: jobId })} />
    ))],
    ['Offers ready to send', toSend.map((o) => (
      <Item key={`o${o.id}`} title={who(o.application)} detail={o.title || titleOf(o.job)} action="Send" tone="primary" onClick={() => app.go('offers', { id: o.id })} />
    ))],
    [`Offers out ${OFFER_WAIT_DAYS}+ days with no answer`, waiting.map((o) => (
      <Item key={`w${o.id}`} title={who(o.application)} detail={`Sent ${daysSince(o.sent_on)} days ago · ${o.title || titleOf(o.job)}`}
        action="Follow up" onClick={() => app.go('offers', { id: o.id })} />
    ))],
    [`In one stage ${STUCK_DAYS}+ days`, stuck.map((a) => (
      <Item key={`k${a.id}`} title={a.name} detail={`${app.optionLabel('application', 'stage', a.stage)} for ${daysSince(a.stage_entered_at || a.updated_at || a.created_at)} days · ${titleOf(a.job)}`}
        action="Open" onClick={() => app.go('candidate', { id: a.id })} />
    ))],
    ['Still picking an interview time', unpicked.map((x) => (
      <Item key={`p${x.id}`} title={who(x.application)} detail={`Asked ${daysSince(x.updated_at || x.created_at)} days ago · ${titleOf(x.job)}`}
        action="Open" onClick={() => app.go('candidate', { id: x.application })} />
    ))],
  ].filter(([, items]) => items.length)

  const count = sections.reduce((n, [, items]) => n + items.length, 0)
  return (
    <Stack gap={4}>
      <div>
        <div className="text-lg font-semibold">{firstName ? `Hi ${firstName}.` : 'Hello.'} {count ? `${count} ${count === 1 ? 'thing needs' : 'things need'} you.` : 'Nothing needs you right now.'}</div>
        <div className="flex flex-wrap items-center gap-3 text-sm text-muted-foreground">
          <span>{allJobs ? 'From every job.' : myJobs.size ? 'From the jobs you are on the hiring team of.' : 'Interviews, scorecards and approvals for you.'}</span>
          {recruiter && myJobs.size > 0 && (
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={everyJob} onChange={(e) => setEveryJob(e.target.checked)} />
              Every job, not just mine
            </label>
          )}
        </div>
      </div>
      {sections.length === 0 && (
        <Card>
          <Stack gap={2}>
            <Empty>All caught up.</Empty>
            <div className="flex flex-wrap justify-center gap-2">
              <Button size="sm" onClick={() => app.go('jobs')}>Jobs</Button>
              {(recruiter || app.roles.includes('interviewer')) && <Button size="sm" onClick={() => app.go('pipeline')}>Pipeline</Button>}
              <Button size="sm" onClick={() => app.go('refer')}>Refer someone</Button>
            </div>
          </Stack>
        </Card>
      )}
      {sections.map(([title, items]) => (
        <Section key={title} title={title} items={items}
          more={<div className="pt-2 text-xs text-muted-foreground">and {items.length - 8} more</div>} />
      ))}
    </Stack>
  )
}
