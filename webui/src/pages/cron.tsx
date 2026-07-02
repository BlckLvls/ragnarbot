// Cron: scheduled jobs — list + schedule-builder editor with live server validation.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, CronJob } from '../lib/api'
import { Page } from '../app/shell'
import {
  Button,
  ConfirmDialog,
  EmptyState,
  FieldError,
  SectionLabel,
  Segmented,
  Select,
  Sheet,
  Skeleton,
  StatusPill,
  TextArea,
  TextInput,
  Toggle,
} from '../components/ui'

// ── time helpers ─────────────────────────────────────────────

const UNIT_MS = { s: 1000, m: 60_000, h: 3_600_000 } as const
type Unit = keyof typeof UNIT_MS

function splitInterval(ms: number): [number, Unit] {
  if (ms % UNIT_MS.h === 0) return [ms / UNIT_MS.h, 'h']
  if (ms % UNIT_MS.m === 0) return [ms / UNIT_MS.m, 'm']
  return [Math.round(ms / UNIT_MS.s), 's']
}

function fmtDate(ms: number): string {
  return new Date(ms).toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function relFuture(ms: number): string {
  const d = ms - Date.now()
  if (d <= 0) return 'now'
  const mins = Math.floor(d / 60_000)
  if (mins < 60) return `in ${mins}m`
  const h = Math.floor(mins / 60)
  const rm = mins % 60
  if (h < 24) return `in ${h}h ${rm}m`
  const days = Math.floor(h / 24)
  return `in ${days}d ${h % 24}h`
}

function relPast(iso: string): string {
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const today = new Date()
  const yst = new Date(Date.now() - 86_400_000)
  if (d.toDateString() === today.toDateString()) return `today ${t}`
  if (d.toDateString() === yst.toDateString()) return `yesterday ${t}`
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${t}`
}

function scheduleLabel(s: CronJob['schedule']): string {
  if (s.kind === 'every' && s.every_ms) {
    const [n, u] = splitInterval(s.every_ms)
    return `every ${n}${u}`
  }
  if (s.kind === 'at' && s.at_ms) return `once · ${fmtDate(s.at_ms)}`
  if (s.kind === 'cron') return s.expr || 'cron'
  return '—'
}

function toLocalInput(ms: number): string {
  const d = new Date(ms - new Date().getTimezoneOffset() * 60_000)
  return d.toISOString().slice(0, 16)
}

// ── log record ───────────────────────────────────────────────

interface CronLog {
  timestamp: string
  job_name?: string
  mode?: string
  input?: string
  output?: string
  status?: string
  duration_s?: number
  error?: string
}

// ── editor form ──────────────────────────────────────────────

const CHANNELS = ['web', 'telegram'] as const

interface Form {
  name: string
  schedKind: 'at' | 'every' | 'cron'
  atLocal: string
  everyNum: number
  everyUnit: Unit
  expr: string
  tz: string
  message: string
  mode: 'isolated' | 'session'
  agent: string
  channel: string
  to: string
  deliver: boolean
  deleteAfterRun: boolean
}

function initForm(job: CronJob | null): Form {
  const s = job?.schedule
  const [everyNum, everyUnit] = s?.kind === 'every' && s.every_ms ? splitInterval(s.every_ms) : [15, 'm' as Unit]
  return {
    name: job?.name ?? '',
    schedKind: s?.kind ?? 'every',
    atLocal: s?.kind === 'at' && s.at_ms ? toLocalInput(s.at_ms) : '',
    everyNum,
    everyUnit,
    expr: s?.kind === 'cron' ? s.expr ?? '' : '',
    tz: s?.tz ?? '',
    message: job?.payload.message ?? '',
    mode: job?.payload.mode ?? 'isolated',
    agent: job?.payload.agent ?? '',
    channel: job?.payload.channel ?? 'web',
    to: job?.payload.to ?? '',
    deliver: job?.payload.deliver ?? false,
    deleteAfterRun: job?.delete_after_run ?? false,
  }
}

function buildSchedule(f: Form): CronJob['schedule'] {
  if (f.schedKind === 'at') return { kind: 'at', at_ms: f.atLocal ? new Date(f.atLocal).getTime() : null }
  if (f.schedKind === 'every') return { kind: 'every', every_ms: f.everyNum * UNIT_MS[f.everyUnit] }
  return { kind: 'cron', expr: f.expr.trim(), tz: f.tz.trim() || null }
}

function schedChanged(a: CronJob['schedule'], b: CronJob['schedule']): boolean {
  if (a.kind !== b.kind) return true
  if (a.kind === 'at') return a.at_ms !== b.at_ms
  if (a.kind === 'every') return a.every_ms !== b.every_ms
  return a.expr !== b.expr || (a.tz ?? '') !== (b.tz ?? '')
}

function CronEditor({ job, onClose }: { job: CronJob | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [f, setF] = useState<Form>(() => initForm(job))
  const [err, setErr] = useState<string | null>(null)
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((p) => ({ ...p, [k]: v }))

  const scheduleDirty = job ? schedChanged(job.schedule, buildSchedule(f)) : true

  const save = useMutation({
    mutationFn: async () => {
      const schedule = buildSchedule(f)
      const body = {
        name: f.name.trim() || 'unnamed',
        schedule,
        message: f.message,
        mode: f.mode,
        deliver: f.deliver,
        channel: f.channel,
        to: f.to.trim() || null,
        delete_after_run: f.deleteAfterRun,
        agent: f.mode === 'isolated' && f.agent.trim() ? f.agent.trim() : null,
      }
      if (!job) return api.post('/api/cron', body)
      if (scheduleDirty) {
        await api.delete(`/api/cron/${job.id}`)
        return api.post('/api/cron', body)
      }
      return api.patch(`/api/cron/${job.id}`, {
        name: body.name,
        message: body.message,
        mode: body.mode,
        agent: body.agent,
      })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['cron'] })
      onClose()
    },
    onError: (e: Error) => setErr(e.message),
  })

  return (
    <Sheet open onClose={onClose} side title={job ? `Edit · ${job.name}` : 'New cron job'}>
      <div className="space-y-4">
        <div>
          <SectionLabel className="mb-1.5">Name</SectionLabel>
          <TextInput value={f.name} onChange={(e) => set('name', e.target.value)} placeholder="morning-digest" />
        </div>

        <div>
          <SectionLabel className="mb-1.5">Schedule</SectionLabel>
          <Segmented
            options={['at', 'every', 'cron'] as const}
            value={f.schedKind}
            onChange={(v) => set('schedKind', v)}
            labels={{ at: 'One-time', every: 'Interval', cron: 'Cron' }}
          />
          <div className="mt-2.5">
            {f.schedKind === 'at' && (
              <TextInput type="datetime-local" value={f.atLocal} onChange={(e) => set('atLocal', e.target.value)} />
            )}
            {f.schedKind === 'every' && (
              <div className="flex items-center gap-2">
                <TextInput
                  type="number"
                  min={1}
                  value={f.everyNum}
                  onChange={(e) => set('everyNum', Math.max(1, Number(e.target.value) || 1))}
                  className="w-24"
                />
                <Select value={f.everyUnit} onChange={(e) => set('everyUnit', e.target.value as Unit)} className="w-28">
                  <option value="s">seconds</option>
                  <option value="m">minutes</option>
                  <option value="h">hours</option>
                </Select>
              </div>
            )}
            {f.schedKind === 'cron' && (
              <div className="space-y-2">
                <TextInput
                  value={f.expr}
                  onChange={(e) => set('expr', e.target.value)}
                  placeholder="30 3 * * *"
                  className="font-mono tracking-[1px]"
                />
                <div className="rounded-[3px] border border-line bg-deep px-3 py-2">
                  <div className="rb-label mb-1">expression</div>
                  <div className="font-mono text-[11px] text-mist">{f.expr.trim() || '— none —'}</div>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[11px] text-soft">Timezone</span>
                  <TextInput value={f.tz} onChange={(e) => set('tz', e.target.value)} placeholder="Europe/Kyiv" />
                </div>
              </div>
            )}
          </div>
          {job && scheduleDirty && (
            <div className="mt-1.5 text-[10.5px] text-muted">changing the schedule recreates the job (id changes)</div>
          )}
        </div>

        <div>
          <SectionLabel className="mb-1.5">Prompt</SectionLabel>
          <TextArea
            rows={3}
            value={f.message}
            onChange={(e) => set('message', e.target.value)}
            placeholder="What should the agent do on each run?"
          />
        </div>

        <div>
          <SectionLabel className="mb-1.5">Mode</SectionLabel>
          <Segmented options={['isolated', 'session'] as const} value={f.mode} onChange={(v) => set('mode', v)} />
          <div className="mt-1.5 text-[10.5px] text-muted">
            {f.mode === 'isolated'
              ? 'fresh context each run, result posted to the delivery target'
              : 'runs inside your main conversation session'}
          </div>
        </div>

        {f.mode === 'isolated' && (
          <div>
            <SectionLabel className="mb-1.5">Agent (optional)</SectionLabel>
            <TextInput value={f.agent} onChange={(e) => set('agent', e.target.value)} placeholder="default" />
          </div>
        )}

        <div>
          <SectionLabel className="mb-1.5">Delivery</SectionLabel>
          <div className="flex items-center gap-2">
            <Select value={f.channel} onChange={(e) => set('channel', e.target.value)} className="w-32">
              {CHANNELS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </Select>
            <TextInput value={f.to} onChange={(e) => set('to', e.target.value)} placeholder="recipient (optional)" />
          </div>
          <div className="mt-2 flex items-center justify-between">
            <span className="text-[12px] text-body">Deliver result</span>
            <Toggle value={f.deliver} onChange={(v) => set('deliver', v)} />
          </div>
        </div>

        <div className="flex items-center justify-between">
          <span className="text-[12px] text-body">Delete after run</span>
          <Toggle value={f.deleteAfterRun} onChange={(v) => set('deleteAfterRun', v)} />
        </div>

        {err && <FieldError>{err}</FieldError>}

        <div className="flex justify-end gap-2 border-t border-line pt-3">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} onClick={() => (setErr(null), save.mutate())}>
            {job ? 'Save job' : 'Create job'}
          </Button>
        </div>
      </div>
    </Sheet>
  )
}

// ── history (expanded) ───────────────────────────────────────

function JobHistory({ jobId }: { jobId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['cron-logs', jobId],
    queryFn: () => api.get<CronLog[]>(`/api/cron/${jobId}/logs`),
  })
  const [open, setOpen] = useState<number | null>(null)
  if (isLoading) return <Skeleton className="mt-1 w-1/2" />
  if (!data || data.length === 0) return <div className="px-3 py-2 text-[11px] text-muted">No runs yet.</div>
  return (
    <div className="space-y-1 px-3 py-2">
      <div className="rb-label mb-1">history</div>
      {data.map((log, i) => (
        <div key={i}>
          <button
            onClick={() => setOpen(open === i ? null : i)}
            className="flex w-full items-center gap-2 py-[3px] text-left"
          >
            <StatusPill status={log.status ?? 'skipped'} />
            <span className="font-mono text-[10.5px] text-body">{relPast(log.timestamp)}</span>
            {log.duration_s != null && (
              <span className="ml-auto font-mono text-[9.5px] text-faint">{log.duration_s.toFixed(0)}s</span>
            )}
          </button>
          {open === i && (log.output || log.error) && (
            <pre className="mb-1 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-[3px] bg-deep p-2 font-mono text-[10px] text-mist">
              {log.error || log.output}
            </pre>
          )}
        </div>
      ))}
    </div>
  )
}

// ── job row ──────────────────────────────────────────────────

function JobRow({ job, onEdit }: { job: CronJob; onEdit: () => void }) {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [confirmDel, setConfirmDel] = useState(false)
  const invalidate = () => qc.invalidateQueries({ queryKey: ['cron'] })

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.patch(`/api/cron/${job.id}`, { enabled }),
    onSuccess: invalidate,
  })
  const run = useMutation({
    mutationFn: () => api.post(`/api/cron/${job.id}/run`),
  })
  const del = useMutation({
    mutationFn: () => api.delete(`/api/cron/${job.id}`),
    onSuccess: invalidate,
  })

  const nextMs = job.state.next_run_at_ms
  const status = job.state.last_status

  return (
    <div className={`rounded-[4px] border bg-raised ${job.state.last_error ? 'border-err/30' : 'border-line'} ${!job.enabled ? 'opacity-60' : ''}`}>
      <div className="flex flex-col gap-2.5 p-3 lg:flex-row lg:items-center lg:gap-4">
        <div className="flex items-center gap-2 lg:w-[26%] lg:min-w-0">
          <span className="truncate text-[13px] font-semibold text-ink">{job.name}</span>
          {job.delete_after_run && (
            <span className="rounded-[2px] bg-raised2 px-1.5 py-[1.5px] font-mono text-[8.5px] text-soft">once</span>
          )}
        </div>
        <div className="flex flex-1 flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-[12px] text-body">{scheduleLabel(job.schedule)}</span>
          <span className="font-mono text-[11px] text-mist">
            {!job.enabled ? 'paused' : nextMs ? relFuture(nextMs) : '—'}
          </span>
          {status && <StatusPill status={status} />}
        </div>
        <div className="flex items-center gap-2 lg:justify-end">
          <Toggle value={job.enabled} onChange={(v) => toggle.mutate(v)} />
          <Button variant="secondary" onClick={() => run.mutate()} loading={run.isPending} title="Run now">
            ▶
          </Button>
          <Button variant="secondary" onClick={onEdit} title="Edit">
            Edit
          </Button>
          <Button variant="destructive" onClick={() => setConfirmDel(true)} title="Delete">
            ×
          </Button>
        </div>
      </div>

      {job.state.last_error && (
        <div className="mx-3 mb-2 rounded-[3px] border border-err/25 bg-err/[.06] px-3 py-2 font-mono text-[10.5px] leading-relaxed text-err/90">
          {job.state.last_error}
        </div>
      )}

      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full border-t border-line px-3 py-1.5 text-left font-mono text-[9.5px] text-muted hover:text-ink"
      >
        {expanded ? '▾ hide history' : '▸ run history'}
      </button>
      {expanded && <JobHistory jobId={job.id} />}

      <ConfirmDialog
        open={confirmDel}
        title={`Delete "${job.name}"?`}
        body="This removes the job and its schedule permanently."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          del.mutate()
          setConfirmDel(false)
        }}
        onCancel={() => setConfirmDel(false)}
      />
    </div>
  )
}

// ── page ─────────────────────────────────────────────────────

export default function CronPage() {
  const { data, isLoading } = useQuery({
    queryKey: ['cron'],
    queryFn: () => api.get<CronJob[]>('/api/cron'),
    refetchInterval: 30_000,
  })
  const [editing, setEditing] = useState<CronJob | null>(null)
  const [creating, setCreating] = useState(false)

  const jobs = data ?? []
  const nextRun = jobs
    .map((j) => j.state.next_run_at_ms)
    .filter((v): v is number => !!v)
    .sort((a, b) => a - b)[0]

  return (
    <Page
      title="Cron"
      actions={
        <Button variant="primary" onClick={() => setCreating(true)}>
          + New job
        </Button>
      }
    >
      <div className="p-4 lg:p-6">
        <div className="mb-3 flex items-center gap-3">
          <span className="font-mono text-[10px] text-muted">
            {jobs.length} job{jobs.length === 1 ? '' : 's'}
            {nextRun ? ` · next ${relFuture(nextRun)}` : ''}
          </span>
          <Button variant="primary" className="ml-auto lg:hidden" onClick={() => setCreating(true)}>
            + New
          </Button>
        </div>

        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-14" />
            <Skeleton className="h-14" />
          </div>
        ) : jobs.length === 0 ? (
          <EmptyState
            title="No scheduled jobs yet"
            action={
              <Button variant="primary" onClick={() => setCreating(true)}>
                + New job
              </Button>
            }
          />
        ) : (
          <div className="space-y-2">
            {jobs.map((job) => (
              <JobRow key={job.id} job={job} onEdit={() => setEditing(job)} />
            ))}
          </div>
        )}
      </div>

      {(editing || creating) && (
        <CronEditor
          job={editing}
          onClose={() => {
            setEditing(null)
            setCreating(false)
          }}
        />
      )}
    </Page>
  )
}
