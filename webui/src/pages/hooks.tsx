// Hooks: incoming webhooks — list + editor with a "how to call" panel and secret warning.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, HookDef, StatusFull } from '../lib/api'
import { Page } from '../app/shell'
import {
  Button,
  ConfirmDialog,
  EmptyState,
  FieldError,
  SectionLabel,
  Segmented,
  Select,
  Skeleton,
  TextArea,
  TextInput,
  Toggle,
} from '../components/ui'

const CHANNELS = ['web', 'telegram'] as const

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

function relMs(ms: number): string {
  return relPast(new Date(ms).toISOString())
}

// ── how-to-call panel ────────────────────────────────────────

function HowToCall({ hookId, port }: { hookId: string; port?: number }) {
  const [copied, setCopied] = useState(false)
  const host = typeof window !== 'undefined' ? window.location.hostname : 'localhost'
  const url = `http://${host}:${port ?? 18791}/hooks/${hookId}`
  const curl = `curl -X POST ${url} \\\n  -H "Content-Type: application/json" \\\n  -d '{"event":"..."}'`
  const copy = () => {
    navigator.clipboard?.writeText(url).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div className="rounded-[4px] border border-line bg-deep p-3.5">
      <div className="rb-label mb-2.5">how to call</div>
      <div className="flex items-center gap-2 rounded-[3px] border border-line2 bg-inset px-3 py-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-body">{url}</span>
        <button onClick={copy} className="flex-none font-mono text-[10.5px] text-acc hover:opacity-80">
          {copied ? 'copied' : 'copy'}
        </button>
      </div>
      <pre className="mt-2 overflow-x-auto whitespace-pre rounded-[3px] border border-line2 bg-inset px-3 py-2 font-mono text-[10.5px] leading-relaxed text-soft">
        {curl}
      </pre>
      <div className="mt-2.5 flex items-center gap-2">
        <span className="h-[4px] w-[4px] flex-none bg-warn" />
        <span className="text-[11px] text-warn">
          The hook id is the secret — anyone with this URL can trigger it.
        </span>
      </div>
    </div>
  )
}

// ── trigger history ──────────────────────────────────────────

function TriggerHistory({ hookId }: { hookId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['hook-history', hookId],
    queryFn: () => api.get<Record<string, unknown>[]>(`/api/hooks/${hookId}/history`),
  })
  const [open, setOpen] = useState<number | null>(null)
  if (isLoading) return <Skeleton className="w-1/2" />
  if (!data || data.length === 0) return <div className="text-[11px] text-muted">No triggers yet.</div>

  const timeOf = (r: Record<string, unknown>): string => {
    const raw = r.timestamp ?? r.time ?? r.ts ?? r.created_at
    if (typeof raw === 'number') return relMs(raw)
    if (typeof raw === 'string') return relPast(raw)
    return '—'
  }
  const preview = (r: Record<string, unknown>): string => {
    const payload = r.payload ?? r.body ?? r.data ?? r
    try {
      return JSON.stringify(payload)
    } catch {
      return String(payload)
    }
  }

  return (
    <div className="rounded-[4px] border border-line bg-raised">
      {data.map((rec, i) => (
        <div key={i} className="border-b border-line last:border-0">
          <button
            onClick={() => setOpen(open === i ? null : i)}
            className="flex w-full flex-col gap-1 px-3 py-2.5 text-left hover:bg-raised2/50"
          >
            <span className="font-mono text-[10.5px] text-body">{timeOf(rec)}</span>
            <span className="truncate font-mono text-[10px] text-muted">{preview(rec)}</span>
          </button>
          {open === i && (
            <pre className="mx-3 mb-2 max-h-56 overflow-auto whitespace-pre-wrap rounded-[3px] bg-deep p-2 font-mono text-[10px] text-mist">
              {JSON.stringify(rec, null, 2)}
            </pre>
          )}
        </div>
      ))}
    </div>
  )
}

// ── editor / detail ──────────────────────────────────────────

interface Form {
  name: string
  instructions: string
  mode: 'alert' | 'silent'
  channel: string
  to: string
}

function initForm(hook: HookDef | null): Form {
  return {
    name: hook?.name ?? '',
    instructions: hook?.instructions ?? '',
    mode: hook?.mode ?? 'alert',
    channel: hook?.channel ?? 'web',
    to: hook?.to ?? '',
  }
}

function HookDetail({
  hook,
  port,
  onClose,
}: {
  hook: HookDef | null
  port?: number
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [f, setF] = useState<Form>(() => initForm(hook))
  const [err, setErr] = useState<string | null>(null)
  const [confirmDel, setConfirmDel] = useState(false)
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((p) => ({ ...p, [k]: v }))
  const invalidate = () => qc.invalidateQueries({ queryKey: ['hooks'] })

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: f.name.trim(),
        instructions: f.instructions,
        mode: f.mode,
        channel: f.channel,
        to: f.to.trim() || null,
      }
      return hook ? api.patch(`/api/hooks/${hook.id}`, body) : api.post('/api/hooks', body)
    },
    onSuccess: () => {
      invalidate()
      onClose()
    },
    onError: (e: Error) => setErr(e.message),
  })
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.patch(`/api/hooks/${hook!.id}`, { enabled }),
    onSuccess: invalidate,
  })
  const del = useMutation({
    mutationFn: () => api.delete(`/api/hooks/${hook!.id}`),
    onSuccess: () => {
      invalidate()
      onClose()
    },
  })

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-line px-4 py-3 lg:px-6">
        <button className="text-[13px] text-muted hover:text-ink lg:hidden" onClick={onClose}>
          ‹ Back
        </button>
        <span className="truncate text-[14px] font-semibold text-ink">{hook ? hook.name : 'New hook'}</span>
        {hook && <Toggle value={hook.enabled} onChange={(v) => toggle.mutate(v)} />}
        {hook && (
          <Button variant="destructive" className="ml-auto" onClick={() => setConfirmDel(true)}>
            Delete
          </Button>
        )}
        <button className="hidden text-[16px] leading-none text-muted hover:text-ink lg:block" onClick={onClose}>
          ×
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 lg:p-6">
        <div className="grid gap-4 lg:grid-cols-2">
          <div>
            <SectionLabel className="mb-1.5">Name</SectionLabel>
            <TextInput value={f.name} onChange={(e) => set('name', e.target.value)} placeholder="github-push" />
          </div>
          <div>
            <SectionLabel className="mb-1.5">Mode</SectionLabel>
            <Segmented options={['alert', 'silent'] as const} value={f.mode} onChange={(v) => set('mode', v)} />
            <div className="mt-1.5 text-[10.5px] text-muted">
              alert = agent decides &amp; messages you · silent = runs quietly
            </div>
          </div>
        </div>

        <div>
          <SectionLabel className="mb-1.5">Instructions</SectionLabel>
          <TextArea
            rows={4}
            value={f.instructions}
            onChange={(e) => set('instructions', e.target.value)}
            placeholder="What should the agent do when this hook fires?"
          />
        </div>

        <div className="grid gap-4 lg:grid-cols-2">
          <div>
            <SectionLabel className="mb-1.5">Delivery channel</SectionLabel>
            <Select value={f.channel} onChange={(e) => set('channel', e.target.value)}>
              {CHANNELS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <SectionLabel className="mb-1.5">Recipient</SectionLabel>
            <TextInput value={f.to} onChange={(e) => set('to', e.target.value)} placeholder="optional" />
          </div>
        </div>

        {err && <FieldError>{err}</FieldError>}

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} onClick={() => (setErr(null), save.mutate())}>
            {hook ? 'Save hook' : 'Create hook'}
          </Button>
        </div>

        {hook && (
          <>
            <HowToCall hookId={hook.id} port={port} />
            <div>
              <SectionLabel className="mb-2">Trigger history</SectionLabel>
              <TriggerHistory hookId={hook.id} />
            </div>
          </>
        )}
      </div>

      {hook && (
        <ConfirmDialog
          open={confirmDel}
          title={`Delete "${hook.name}"?`}
          body="The hook URL will stop working immediately."
          confirmLabel="Delete"
          destructive
          onConfirm={() => {
            del.mutate()
            setConfirmDel(false)
          }}
          onCancel={() => setConfirmDel(false)}
        />
      )}
    </div>
  )
}

// ── list item ────────────────────────────────────────────────

function HookListItem({ hook, active, onClick }: { hook: HookDef; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full flex-col gap-1.5 rounded-[3px] px-3 py-3 text-left ${active ? 'bg-raised2' : 'hover:bg-raised/60'}`}
    >
      <div className="flex items-center gap-2">
        <span className="truncate text-[13px] font-semibold text-ink">{hook.name}</span>
        <span
          className={`ml-auto rounded-[2px] px-1.5 py-[1.5px] font-mono text-[8.5px] ${
            hook.mode === 'alert' ? 'bg-acc/[.13] text-acc' : 'bg-raised2 text-soft'
          }`}
        >
          {hook.mode}
        </span>
        {!hook.enabled && (
          <span className="rounded-[2px] bg-raised2 px-1.5 py-[1.5px] font-mono text-[8.5px] text-muted">off</span>
        )}
      </div>
      <span className="font-mono text-[10px] text-faint">
        {hook.trigger_count} trigger{hook.trigger_count === 1 ? '' : 's'} · {relMs(hook.updated_at_ms)}
      </span>
    </button>
  )
}

// ── page ─────────────────────────────────────────────────────

export default function HooksPage() {
  const { data, isLoading } = useQuery({
    queryKey: ['hooks'],
    queryFn: () => api.get<HookDef[]>('/api/hooks'),
    refetchInterval: 30_000,
  })
  const { data: status } = useQuery({
    queryKey: ['status-full'],
    queryFn: () => api.get<StatusFull>('/api/status/full'),
  })
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  const hooks = data ?? []
  const selected = hooks.find((h) => h.id === selectedId) ?? null
  const port = status?.hooks?.port
  const showDetail = creating || !!selected

  const openNew = () => {
    setCreating(true)
    setSelectedId(null)
  }
  const closeDetail = () => {
    setCreating(false)
    setSelectedId(null)
  }

  return (
    <Page
      title="Hooks"
      actions={
        <Button variant="primary" onClick={openNew}>
          + New hook
        </Button>
      }
    >
      <div className="flex h-full min-h-0">
        {/* list — full width on mobile, sidebar on desktop */}
        <div
          className={`min-h-0 flex-1 overflow-y-auto lg:max-w-[300px] lg:flex-none lg:border-r lg:border-line ${
            showDetail ? 'hidden lg:block' : ''
          }`}
        >
          <div className="flex items-center gap-2 px-4 py-3">
            <span className="font-mono text-[10px] text-muted">
              {hooks.length} hook{hooks.length === 1 ? '' : 's'}
              {port ? ` · port ${port}` : ''}
            </span>
            <Button variant="primary" className="ml-auto lg:hidden" onClick={openNew}>
              + New
            </Button>
          </div>
          <div className="space-y-1 px-2 pb-4">
            {isLoading ? (
              <>
                <Skeleton className="h-12" />
                <Skeleton className="h-12" />
              </>
            ) : hooks.length === 0 ? (
              <EmptyState title="No hooks yet" className="mx-2" />
            ) : (
              hooks.map((h) => (
                <HookListItem
                  key={h.id}
                  hook={h}
                  active={h.id === selectedId}
                  onClick={() => {
                    setSelectedId(h.id)
                    setCreating(false)
                  }}
                />
              ))
            )}
          </div>
        </div>

        {/* detail / editor */}
        {showDetail && (
          <div className="min-h-0 flex-1 overflow-hidden">
            <HookDetail key={selected?.id ?? 'new'} hook={selected} port={port} onClose={closeDetail} />
          </div>
        )}
        {!showDetail && (
          <div className="hidden min-h-0 flex-1 items-center justify-center lg:flex">
            <span className="text-[12px] text-muted">Select a hook or create one.</span>
          </div>
        )}
      </div>
    </Page>
  )
}
