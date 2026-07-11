// Cron: compact monitoring and control. Creation stays agent-first through Chat.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, CronJob } from '../lib/api'
import { relFuture, relPast } from '../lib/format'
import { Page } from '../app/shell'
import { Button, ConfirmDialog, EmptyState, Skeleton, StatusPill, Toggle } from '../components/ui'

interface CronLog {
  timestamp: string
  input?: string
  output?: string
  status?: string
  duration_s?: number
  error?: string
}

const UNIT_MS = { s: 1000, m: 60_000, h: 3_600_000 } as const

function scheduleLabel(schedule: CronJob['schedule']): string {
  if (schedule.kind === 'every' && schedule.every_ms) {
    if (schedule.every_ms % UNIT_MS.h === 0) return `every ${schedule.every_ms / UNIT_MS.h}h`
    if (schedule.every_ms % UNIT_MS.m === 0) return `every ${schedule.every_ms / UNIT_MS.m}m`
    return `every ${Math.round(schedule.every_ms / UNIT_MS.s)}s`
  }
  if (schedule.kind === 'at' && schedule.at_ms) {
    return `once · ${new Date(schedule.at_ms).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    })}`
  }
  if (schedule.kind === 'cron') return schedule.expr || 'cron'
  return '—'
}

function RunHistory({ jobId }: { jobId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['cron-logs', jobId],
    queryFn: () => api.get<CronLog[]>(`/api/cron/${jobId}/logs`),
  })
  const [showAll, setShowAll] = useState(false)
  const [open, setOpen] = useState<number | null>(null)
  if (isLoading) return <Skeleton className="m-3 w-1/2" />
  if (!data?.length) return <div className="px-3 py-3 text-[11px] text-muted">No runs yet.</div>

  const logs = showAll ? data : data.slice(0, 5)
  return (
    <div className="border-t border-line px-3 py-2.5">
      <div className="mb-1.5 flex items-center gap-2">
        <span className="rb-label">Recent runs</span>
        {data.length > 5 && (
          <button onClick={() => setShowAll(!showAll)} className="ml-auto font-mono text-[9.5px] text-acc">
            {showAll ? 'show less' : `show all ${data.length}`}
          </button>
        )}
      </div>
      {logs.map((log, index) => (
        <div key={`${log.timestamp}-${index}`} className="border-t border-line first:border-0">
          <button
            onClick={() => setOpen(open === index ? null : index)}
            className="flex min-h-[40px] w-full items-center gap-2 text-left"
          >
            <StatusPill status={log.status ?? 'unknown'} />
            <span className="font-mono text-[10.5px] text-body">{relPast(log.timestamp)}</span>
            {log.duration_s != null && (
              <span className="ml-auto font-mono text-[9.5px] text-faint">{log.duration_s.toFixed(1)}s</span>
            )}
            <span className="font-mono text-[9px] text-faint">{open === index ? '▾' : '▸'}</span>
          </button>
          {open === index && (
            <pre className="mb-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-[3px] bg-deep p-2.5 font-mono text-[10px] text-mist">
              {log.error || log.output || 'No output'}
            </pre>
          )}
        </div>
      ))}
    </div>
  )
}

function JobCard({ job }: { job: CronJob }) {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['cron'] })
    qc.invalidateQueries({ queryKey: ['cron-logs', job.id] })
  }
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.patch(`/api/cron/${job.id}`, { enabled }),
    onSuccess: invalidate,
  })
  const run = useMutation({ mutationFn: () => api.post(`/api/cron/${job.id}/run`), onSuccess: invalidate })
  const remove = useMutation({ mutationFn: () => api.delete(`/api/cron/${job.id}`), onSuccess: invalidate })

  return (
    <div className={`rounded-[4px] border bg-raised ${job.state.last_error ? 'border-err/30' : 'border-line'}`}>
      <div className={`flex flex-col gap-3 p-3.5 lg:flex-row lg:items-center ${job.enabled ? '' : 'opacity-60'}`}>
        <div className="min-w-0 lg:w-[28%]">
          <div className="flex items-center gap-2">
            <span className="truncate text-[13px] font-semibold text-ink">{job.name}</span>
            {job.delete_after_run && (
              <span className="rounded-[2px] bg-raised2 px-1.5 py-0.5 font-mono text-[8.5px] text-soft">once</span>
            )}
          </div>
          <div className="mt-1 truncate text-[11px] text-muted">{job.payload.message}</div>
        </div>

        <div className="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-[12px] text-body">{scheduleLabel(job.schedule)}</span>
          <span className="font-mono text-[10.5px] text-mist">
            {!job.enabled ? 'paused' : job.state.next_run_at_ms ? relFuture(job.state.next_run_at_ms) : '—'}
          </span>
          {job.state.last_status && <StatusPill status={job.state.last_status} />}
        </div>

        <div className="flex items-center gap-2">
          <Toggle
            label={`${job.name}: ${job.enabled ? 'pause' : 'enable'}`}
            value={job.enabled}
            onChange={(value) => toggle.mutate(value)}
          />
          <Button variant="secondary" onClick={() => run.mutate()} loading={run.isPending} title="Run now">▶ Run</Button>
          <Button variant="secondary" onClick={() => setExpanded(!expanded)}>
            {expanded ? 'Less' : 'More'}
          </Button>
          <Button variant="destructive" onClick={() => setConfirmDelete(true)} title="Delete">×</Button>
        </div>
      </div>

      {job.state.last_error && (
        <div className="mx-3 mb-3 rounded-[3px] border border-err/25 bg-err/[.06] px-3 py-2 font-mono text-[10.5px] text-err/90">
          {job.state.last_error}
        </div>
      )}
      {expanded && (
        <>
          <div className="grid gap-2 border-t border-line px-3 py-2.5 text-[10.5px] text-soft sm:grid-cols-3">
            <span>mode · {job.payload.mode}</span>
            <span>delivery · {job.payload.deliver ? `${job.payload.channel ?? 'default'}${job.payload.to ? `:${job.payload.to}` : ''}` : 'off'}</span>
            <span>agent · {job.payload.agent || 'default'}</span>
          </div>
          <RunHistory jobId={job.id} />
        </>
      )}

      <ConfirmDialog
        open={confirmDelete}
        title={`Delete "${job.name}"?`}
        body="This removes the job and its schedule permanently."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          remove.mutate()
          setConfirmDelete(false)
        }}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  )
}

export default function CronPage() {
  const navigate = useNavigate()
  const { data, isLoading } = useQuery({
    queryKey: ['cron'],
    queryFn: () => api.get<CronJob[]>('/api/cron'),
    refetchInterval: 30_000,
  })
  const jobs = data ?? []
  const nextRun = jobs
    .map((job) => job.state.next_run_at_ms)
    .filter((value): value is number => !!value)
    .sort((a, b) => a - b)[0]

  return (
    <Page title="Cron">
      <div className="p-4 lg:p-6">
        <div className="mb-4 flex items-center gap-3">
          <span className="font-mono text-[10px] text-muted">
            {jobs.length} job{jobs.length === 1 ? '' : 's'}{nextRun ? ` · next ${relFuture(nextRun)}` : ''}
          </span>
          <Button variant="secondary" className="ml-auto" onClick={() => navigate('/')}>
            Create in Chat
          </Button>
        </div>

        {isLoading ? (
          <div className="space-y-2"><Skeleton className="h-20" /><Skeleton className="h-20" /></div>
        ) : jobs.length === 0 ? (
          <EmptyState
            title="No scheduled jobs yet"
            action={<Button variant="primary" onClick={() => navigate('/')}>Ask ragnarbot to schedule one</Button>}
          />
        ) : (
          <div className="space-y-2">{jobs.map((job) => <JobCard key={job.id} job={job} />)}</div>
        )}
      </div>
    </Page>
  )
}
