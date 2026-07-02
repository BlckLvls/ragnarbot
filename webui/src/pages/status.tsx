// Status: gateway dashboard — services, providers, usage breakdown, live log tail.

import { ReactNode, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api, ApiError, StatusFull, UsageReport } from '../lib/api'
import { fmtTokens, relFuture } from '../lib/format'
import { Page } from '../app/shell'
import { Button, Card, ConfirmDialog, Dot, SectionLabel, Segmented, Skeleton, Toggle } from '../components/ui'

const RANGES = ['day', 'week', 'month'] as const
const LEVELS = ['all', 'info', 'warning', 'error'] as const
const PROVIDERS = ['anthropic', 'openai', 'gemini', 'openrouter'] as const

// ── layout helpers ───────────────────────────────────────────

function Panel({
  label,
  right,
  children,
  className = '',
  accent,
}: {
  label: string
  right?: ReactNode
  children: ReactNode
  className?: string
  accent?: boolean
}) {
  return (
    <Card className={`flex flex-col gap-2.5 px-[13px] py-[12px] ${accent ? 'border-acc/40' : ''} ${className}`}>
      <div className="flex items-center gap-2">
        <SectionLabel>{label}</SectionLabel>
        {right && <span className="ml-auto flex items-center">{right}</span>}
      </div>
      {children}
    </Card>
  )
}

function ServiceRow({ label, value, right }: { label: string; value: string; right?: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-[70px] flex-none text-[12px] text-body">{label}</span>
      <span className="truncate font-mono text-[10.5px] text-soft">{value}</span>
      {right && <span className="ml-auto flex items-center">{right}</span>}
    </div>
  )
}

// ── usage bars ───────────────────────────────────────────────

function BarRows({ data }: { data: Record<string, { input_tokens: number; output_tokens: number }> }) {
  const rows = Object.entries(data).map(([k, v]) => ({ label: k, tok: v.input_tokens + v.output_tokens }))
  rows.sort((a, b) => b.tok - a.tok)
  const max = Math.max(1, ...rows.map((r) => r.tok))
  if (rows.length === 0) return <div className="text-[11px] text-muted">No usage</div>
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map((r) => (
        <div key={r.label} className="flex items-center gap-2">
          <span className="w-[64px] flex-none truncate font-mono text-[10px] text-soft">{r.label}</span>
          <span className="flex h-[6px] flex-1 bg-inset">
            <span className="h-[6px] bg-acc/70" style={{ width: `${(r.tok / max) * 100}%` }} />
          </span>
          <span className="w-[36px] flex-none text-right font-mono text-[10px] text-faint">{fmtTokens(r.tok)}</span>
        </div>
      ))}
    </div>
  )
}

function UsagePanel() {
  const [range, setRange] = useState<(typeof RANGES)[number]>('day')
  const usage = useQuery({
    queryKey: ['usage', range],
    queryFn: () => api.get<UsageReport>(`/api/usage?range=${range}`),
  })
  const u = usage.data
  const t = u?.totals
  return (
    <Panel label="Usage" right={<Segmented options={RANGES} value={range} onChange={setRange} />} className="lg:col-span-2">
      {!u || !t ? (
        <Skeleton className="w-full" />
      ) : (
        <>
          <div className="flex flex-wrap gap-5">
            {(
              [
                ['input', t.input_tokens],
                ['output', t.output_tokens],
                ['cache', t.cache_read_tokens],
                ['turns', t.turns],
              ] as const
            ).map(([k, v]) => (
              <div key={k} className="flex flex-col gap-0.5">
                <span className="font-mono text-[17px] font-semibold text-ink">{fmtTokens(v)}</span>
                <span className="text-[10px] text-muted">{k}</span>
              </div>
            ))}
          </div>
          <div className="mt-1 grid grid-cols-1 gap-4 lg:grid-cols-2">
            <div>
              <div className="rb-label mb-1.5">by model</div>
              <BarRows data={u.by_model} />
            </div>
            <div>
              <div className="rb-label mb-1.5">by source</div>
              <BarRows data={u.by_source} />
            </div>
          </div>
        </>
      )}
    </Panel>
  )
}

// ── live log tail ────────────────────────────────────────────

function logLevel(line: string): 'error' | 'warning' | 'info' | 'other' {
  if (line.includes('ERROR')) return 'error'
  if (line.includes('WARNING')) return 'warning'
  if (line.includes('| INFO |')) return 'info'
  return 'other'
}

function LogsPanel() {
  const [level, setLevel] = useState<(typeof LEVELS)[number]>('all')
  const [auto, setAuto] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  const logs = useQuery({
    queryKey: ['logs-tail'],
    queryFn: () => api.get<{ lines: string[]; path: string }>('/api/logs/tail?lines=200'),
    refetchInterval: auto ? 10000 : false,
  })

  const lines = (logs.data?.lines ?? []).filter((l) => {
    if (level === 'all') return true
    return logLevel(l) === level
  })

  useEffect(() => {
    const el = boxRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [logs.data])

  return (
    <Panel
      label="Live log"
      className="lg:col-span-2"
      right={
        <span className="flex items-center gap-2">
          <span className="flex gap-1">
            {LEVELS.map((l) => (
              <button
                key={l}
                onClick={() => setLevel(l)}
                className={`rounded-[2px] px-2 py-1 font-mono text-[9px] ${
                  l === level ? 'bg-acc text-onacc' : 'bg-raised2 text-soft hover:text-ink'
                }`}
              >
                {l}
              </button>
            ))}
          </span>
          <button
            onClick={() => logs.refetch()}
            className="rounded-[2px] bg-raised2 px-2 py-1 font-mono text-[9px] text-soft hover:text-ink"
          >
            refresh
          </button>
          <span className="flex items-center gap-1.5 font-mono text-[9px] text-muted">
            auto
            <Toggle value={auto} onChange={setAuto} />
          </span>
        </span>
      }
    >
      <div
        ref={boxRef}
        className="max-h-96 overflow-auto rounded-[3px] bg-deep p-3 font-mono text-[10.5px] leading-[1.7]"
      >
        {logs.isLoading ? (
          <Skeleton className="w-2/3" />
        ) : lines.length === 0 ? (
          <div className="text-muted">No log lines</div>
        ) : (
          lines.map((l, i) => {
            const lv = logLevel(l)
            const color =
              lv === 'error' ? 'text-err' : lv === 'warning' ? 'text-warn' : lv === 'info' ? 'text-mist' : 'text-muted'
            return (
              <div key={i} className={`whitespace-pre-wrap break-all ${color}`}>
                {l}
              </div>
            )
          })
        )}
      </div>
      {logs.data?.path && <div className="font-mono text-[9px] text-faint">{logs.data.path}</div>}
    </Panel>
  )
}

// ── page ─────────────────────────────────────────────────────

export default function StatusPage() {
  const [confirmRestart, setConfirmRestart] = useState(false)
  const [confirmUpdate, setConfirmUpdate] = useState(false)
  const [checkResult, setCheckResult] = useState<string | null>(null)

  const status = useQuery({
    queryKey: ['status-full'],
    queryFn: () => api.get<StatusFull>('/api/status/full'),
    refetchInterval: 15000,
  })

  const restart = useMutation({ mutationFn: () => api.post<{ detail: string }>('/api/restart') })
  const check = useMutation({
    mutationFn: () => api.post<{ detail: string }>('/api/update/check'),
    onSuccess: (d) => setCheckResult(d.detail),
    onError: (e) => setCheckResult((e as ApiError)?.message ?? 'check failed'),
  })
  const runUpdate = useMutation({ mutationFn: () => api.post<{ detail: string }>('/api/update/run') })

  const s = status.data
  const allOk = s?.daemon?.status === 'running'

  return (
    <Page
      title="Status"
      actions={
        s && (
          <span className="flex items-center gap-2">
            <Dot color={allOk ? 'ok' : 'warn'} />
            <span className="font-mono text-[10px] text-soft">{allOk ? 'all systems ok' : 'check services'}</span>
          </span>
        )
      }
    >
      <div className="p-4 lg:p-6">
        {!s ? (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {[0, 1, 2, 3].map((i) => (
              <Card key={i} className="space-y-2 py-4">
                <Skeleton className="w-1/3" />
                <Skeleton className="w-2/3" />
              </Card>
            ))}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            {/* Gateway */}
            <Panel label="Gateway" right={<Dot color={allOk ? 'ok' : 'muted'} />}>
              <div className="flex items-baseline gap-2">
                <span className="text-[17px] font-bold text-ink">v{s.version}</span>
                <span className="rounded-[2px] bg-raised2 px-2 py-[2px] font-mono text-[9.5px] text-mist">
                  {s.profile}
                </span>
                <span className="ml-auto font-mono text-[10px] text-soft">daemon {s.daemon.status}</span>
              </div>
              <div className="truncate font-mono text-[10px] text-faint">{s.workspace}</div>
              <Button variant="secondary" className="self-start" onClick={() => setConfirmRestart(true)}>
                Restart gateway
              </Button>
            </Panel>

            {/* Update */}
            <Panel
              label="Update"
              accent={!!s.pending_update}
              right={
                s.pending_update ? (
                  <span className="rounded-[2px] bg-acc/[.13] px-2 py-[2px] font-mono text-[9px] text-acc">
                    update available
                  </span>
                ) : undefined
              }
            >
              {s.pending_update && (
                <div className="rounded-[4px] border border-warn/30 bg-warn/10 px-3 py-2 text-[11px] text-warn">
                  A new version is available.
                </div>
              )}
              {checkResult && (
                <pre className="whitespace-pre-wrap rounded-[3px] bg-deep p-2.5 font-mono text-[10.5px] leading-relaxed text-mist">
                  {checkResult}
                </pre>
              )}
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" onClick={() => check.mutate()} loading={check.isPending}>
                  Check for updates
                </Button>
                <Button variant="primary" onClick={() => setConfirmUpdate(true)}>
                  Update now
                </Button>
              </div>
            </Panel>

            {/* Providers */}
            <Panel label="Providers">
              <div className="grid grid-cols-[1fr_auto_auto] gap-x-6 gap-y-1.5 font-mono text-[10.5px]">
                <span />
                <span className="text-faint">key</span>
                <span className="text-faint">oauth</span>
                {PROVIDERS.map((p) => {
                  const pr = s.providers[p] ?? { api_key: false, oauth: false }
                  return (
                    <div key={p} className="contents">
                      <span className="text-body">{p}</span>
                      <Dot color={pr.api_key ? 'ok' : 'muted'} />
                      <Dot color={pr.oauth ? 'ok' : 'muted'} />
                    </div>
                  )
                })}
              </div>
            </Panel>

            {/* Channels */}
            <Panel label="Channels">
              <div className="flex items-center gap-2">
                <Dot color={s.channels.telegram.enabled ? 'ok' : 'muted'} />
                <span className="text-[12px] text-body">telegram</span>
                <span className="ml-auto font-mono text-[10.5px] text-soft">
                  {s.channels.telegram.enabled ? 'enabled' : 'off'}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Dot color="acc" />
                <span className="text-[12px] text-body">web</span>
                <span className="ml-auto font-mono text-[10.5px] text-soft">
                  {s.channels.web.clients} client{s.channels.web.clients === 1 ? '' : 's'}
                </span>
              </div>
            </Panel>

            {/* Services */}
            <Panel label="Services" className="lg:col-span-2">
              <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
                <ServiceRow
                  label="heartbeat"
                  value={s.heartbeat.enabled ? `every ${s.heartbeat.interval_m}m` : 'off'}
                  right={<Dot color={s.heartbeat.enabled ? 'ok' : 'muted'} />}
                />
                <ServiceRow
                  label="cron"
                  value={`${s.cron.jobs} job${s.cron.jobs === 1 ? '' : 's'} · next ${relFuture(s.cron.next_run_at_ms)}`}
                />
                <ServiceRow
                  label="hooks"
                  value={s.hooks.enabled ? `${s.hooks.count} · port ${s.hooks.port}` : 'off'}
                  right={<Dot color={s.hooks.enabled ? 'ok' : 'muted'} />}
                />
                <ServiceRow
                  label="recall"
                  value={s.recall.status}
                  right={<Dot color={s.recall.ready ? 'ok' : 'muted'} />}
                />
                <ServiceRow label="voice" value={s.transcription} />
              </div>
            </Panel>

            <UsagePanel />
            <LogsPanel />
          </div>
        )}
      </div>

      <ConfirmDialog
        open={confirmRestart}
        title="Restart gateway?"
        body="The daemon will restart. Active sessions reconnect automatically."
        confirmLabel="Restart"
        destructive
        onConfirm={() => {
          restart.mutate()
          setConfirmRestart(false)
        }}
        onCancel={() => setConfirmRestart(false)}
      />
      <ConfirmDialog
        open={confirmUpdate}
        title="Update now?"
        body="This updates the package and restarts the gateway."
        confirmLabel="Update"
        onConfirm={() => {
          runUpdate.mutate()
          setConfirmUpdate(false)
        }}
        onCancel={() => setConfirmUpdate(false)}
      />
    </Page>
  )
}
