// Models: pick the active/fallback model and manage custom OpenAI-compatible servers.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, CatalogModel, CustomServer, ModelsOverview, api } from '../lib/api'
import { Page } from '../app/shell'
import {
  Button,
  Card,
  ConfirmDialog,
  Dot,
  EmptyState,
  FieldError,
  SectionLabel,
  Skeleton,
  TextInput,
  Toggle,
} from '../components/ui'

interface SelectResult {
  status?: string
  detail?: string
  error?: string
  restart_required?: boolean
}

export default function ModelsPage() {
  const qc = useQueryClient()
  const [restartNeeded, setRestartNeeded] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)

  const { data, isLoading } = useQuery({
    queryKey: ['models'],
    queryFn: () => api.get<ModelsOverview>('/api/models'),
  })

  const invalidate = () => qc.invalidateQueries({ queryKey: ['models'] })

  const select = useMutation({
    mutationFn: ({ model, target }: { model: string; target: 'primary' | 'fallback' }) =>
      api.post<SelectResult>('/api/models/select', { model, target }),
    onSuccess: (result, vars) => {
      invalidate()
      if (vars.target === 'primary' && result.restart_required) {
        setRestartNeeded(true)
        setNotice(null)
      } else {
        setRestartNeeded(false)
        setNotice(result.detail ?? (vars.target === 'primary' ? 'Model switched.' : 'Fallback model updated.'))
      }
    },
    onError: (error) => setNotice((error as ApiError)?.message ?? 'Could not switch model.'),
  })

  return (
    <Page title="Models">
      <div className="mx-auto max-w-[760px] space-y-6 px-5 py-6 lg:px-8">
        {restartNeeded && <RestartBanner onDone={() => setRestartNeeded(false)} />}
        {notice && (
          <div role="status" className="rounded-[3px] border border-acc/25 bg-acc/[.06] p-3 text-[11.5px] text-mist">
            {notice}
            <button onClick={() => setNotice(null)} className="ml-2 text-muted hover:text-ink">×</button>
          </div>
        )}

        {isLoading || !data ? (
          <div className="space-y-3">
            <Skeleton className="w-2/3" />
            <Skeleton className="w-1/2" />
            <Skeleton className="w-3/4" />
          </div>
        ) : (
          <>
            <ActiveModelCard data={data} />
            <ProviderList
              data={data}
              onSelect={(model, target) => select.mutate({ model, target })}
              selecting={select.isPending}
            />
            <CustomServers data={data} onChanged={invalidate} onSelect={(model) => select.mutate({ model, target: 'primary' })} />
          </>
        )}
      </div>
    </Page>
  )
}

// ── active model ─────────────────────────────────────────────

function modelLabel(data: ModelsOverview, id: string | null): string {
  if (!id) return 'none'
  for (const p of data.providers)
    for (const m of p.models) if (m.id === id) return `${m.name} · ${p.name}`
  for (const s of data.custom)
    for (const m of s.models) if (m.full_id === id) return `${m.name} · ${s.name}`
  return id
}

function ActiveModelCard({ data }: { data: ModelsOverview }) {
  const { current } = data
  return (
    <Card className="space-y-2">
      <SectionLabel>Active model</SectionLabel>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[15px] font-bold text-ink">{modelLabel(data, current.model)}</span>
        <span className="rounded-[2px] bg-raised2 px-2 py-0.5 font-mono text-[9.5px] text-mist">{current.auth_method}</span>
        <span className="rounded-[2px] bg-raised2 px-2 py-0.5 font-mono text-[9.5px] text-mist">reasoning: {current.reasoning_level}</span>
      </div>
      <div className="font-mono text-[10px] text-faint">{current.model}</div>
      <div className="text-[11px] text-soft">
        Fallback: <span className="font-mono text-[10.5px] text-mist">{modelLabel(data, current.fallback_model)}</span>
      </div>
    </Card>
  )
}

function RestartBanner({ onDone }: { onDone: () => void }) {
  const [confirm, setConfirm] = useState(false)
  const restart = useMutation({
    mutationFn: () => api.post('/api/restart'),
    onSuccess: onDone,
  })
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-[3px] border border-warn/25 bg-warn/[.06] p-3 text-[11.5px] text-mist">
      <Dot color="warn" pulse />
      <span className="min-w-0 flex-1">Model saved. Restart the gateway to apply.</span>
      <Button variant="primary" onClick={() => setConfirm(true)} loading={restart.isPending}>
        Restart now
      </Button>
      <ConfirmDialog
        open={confirm}
        title="Restart gateway?"
        body="The connection will briefly drop and reconnect automatically."
        confirmLabel="Restart"
        destructive
        onConfirm={() => {
          restart.mutate()
          setConfirm(false)
        }}
        onCancel={() => setConfirm(false)}
      />
    </div>
  )
}

// ── provider catalog ─────────────────────────────────────────

function ProviderList({
  data,
  onSelect,
  selecting,
}: {
  data: ModelsOverview
  onSelect: (model: string, target: 'primary' | 'fallback') => void
  selecting: boolean
}) {
  const [open, setOpen] = useState<string | null>(null)
  return (
    <div className="space-y-2">
      <SectionLabel>Providers</SectionLabel>
      <div className="flex flex-col overflow-hidden rounded-[4px] border border-line bg-raised">
        {data.providers.map((p, i) => (
          <div key={p.id} className={i ? 'border-t border-line' : ''}>
            <button
              onClick={() => setOpen(open === p.id ? null : p.id)}
              className="flex w-full items-center gap-2 px-3 py-3 text-left hover:bg-raised2/50"
            >
              <Dot color={p.connected ? 'ok' : 'muted'} />
              <span className="text-[12.5px] font-semibold text-ink">{p.name}</span>
              {p.oauth_set && (
                <span className="rounded-[2px] bg-raised2 px-1.5 py-px font-mono text-[8.5px] text-mist">oauth</span>
              )}
              <span className="hidden truncate text-[10.5px] text-muted sm:inline">{p.description}</span>
              {!p.connected && (
                <span className="ml-auto whitespace-nowrap font-mono text-[9.5px] text-warn">not connected</span>
              )}
              <span className={`${p.connected ? 'ml-auto' : 'ml-2'} text-faint`}>{open === p.id ? '▾' : '▸'}</span>
            </button>
            {open === p.id && (
              <ProviderModels provider={p} data={data} onSelect={onSelect} selecting={selecting} />
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

function ProviderModels({
  provider: p,
  data,
  onSelect,
  selecting,
}: {
  provider: ModelsOverview['providers'][number]
  data: ModelsOverview
  onSelect: (model: string, target: 'primary' | 'fallback') => void
  selecting: boolean
}) {
  const [search, setSearch] = useState('')
  const [freeOnly, setFreeOnly] = useState(false)

  const catalog = useQuery({
    queryKey: ['models-catalog', p.id],
    queryFn: () => api.get<{ ok: boolean; models?: CatalogModel[]; error?: string }>(
      `/api/models/catalog/${p.id}`,
    ),
    enabled: p.connected,
    staleTime: 5 * 60 * 1000,
    refetchOnWindowFocus: false,
  })

  // Live catalog when available; curated registry list as the baseline.
  const curated: CatalogModel[] = p.models.map((m) => ({
    id: m.id,
    name: m.name,
    description: m.description,
    free: false,
  }))
  const live = catalog.data?.ok ? catalog.data.models ?? [] : []
  const curatedIds = new Set(curated.map((m) => m.id))
  const merged = [...curated, ...live.filter((m) => !curatedIds.has(m.id))]

  const query = search.trim().toLowerCase()
  const shown = merged.filter(
    (m) =>
      (!freeOnly || m.free) &&
      (!query || m.id.toLowerCase().includes(query) || m.name.toLowerCase().includes(query)),
  )
  const hasFree = merged.some((m) => m.free)

  return (
    <div className="border-t border-line bg-deep/40">
      {!p.connected && p.api_key_url && (
        <div className="px-3 pt-2 text-[10.5px] text-soft">
          Set the API key in{' '}
          <a href="/settings" className="text-acc hover:opacity-80">Settings → Secrets</a>
          {' '}(<a href={p.api_key_url} target="_blank" rel="noreferrer" className="text-acc hover:opacity-80">get a key ↗</a>)
        </div>
      )}
      {p.connected && (
        <div className="flex items-center gap-2 px-3 pt-2.5">
          <TextInput
            aria-label={`Search ${p.name} models`}
            placeholder={catalog.isLoading ? 'Loading full catalog…' : `Search ${merged.length} models…`}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="max-w-[260px]"
          />
          {hasFree && (
            <button
              onClick={() => setFreeOnly(!freeOnly)}
              className={`rounded-[2px] px-2 py-1.5 font-mono text-[9.5px] uppercase tracking-wider ${
                freeOnly ? 'bg-acc text-onacc' : 'bg-raised2 text-soft hover:text-ink'
              }`}
            >
              free
            </button>
          )}
          <span className="ml-auto font-mono text-[9.5px] text-faint">{shown.length}</span>
        </div>
      )}
      {catalog.data && !catalog.data.ok && (
        <div className="px-3 pt-2 text-[10px] text-warn">
          Full catalog unavailable: {catalog.data.error}
        </div>
      )}
      <div className="max-h-[420px] overflow-y-auto">
        {shown.map((m) => (
          <ModelRow
            key={m.id}
            name={m.name}
            description={m.description}
            id={m.id}
            free={m.free}
            active={data.current.model === m.id}
            isFallback={data.current.fallback_model === m.id}
            disabled={selecting || !p.connected}
            onUse={() => onSelect(m.id, 'primary')}
            onFallback={() => onSelect(m.id, 'fallback')}
          />
        ))}
        {shown.length === 0 && (
          <div className="px-3 py-4 text-[11px] text-muted">Nothing matches.</div>
        )}
      </div>
    </div>
  )
}

function ModelRow({
  name,
  description,
  id,
  free,
  active,
  isFallback,
  disabled,
  onUse,
  onFallback,
}: {
  name: string
  description?: string
  id: string
  free?: boolean
  active: boolean
  isFallback: boolean
  disabled?: boolean
  onUse: () => void
  onFallback: () => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-2 px-3 py-2.5">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate text-[12px] font-medium text-mist">{name}</span>
          {free && <span className="rounded-[2px] bg-ok/[.15] px-1.5 py-px font-mono text-[8.5px] font-semibold text-ok">FREE</span>}
          {active && <span className="rounded-[2px] bg-acc px-1.5 py-px font-mono text-[8.5px] font-semibold text-onacc">ACTIVE</span>}
          {isFallback && <span className="rounded-[2px] bg-raised2 px-1.5 py-px font-mono text-[8.5px] text-mist">FALLBACK</span>}
        </div>
        {description && <div className="text-[10px] text-muted">{description}</div>}
        <div className="truncate font-mono text-[9px] text-faint">{id}</div>
      </div>
      {!active && (
        <Button variant="secondary" disabled={disabled} onClick={onUse}>
          Use
        </Button>
      )}
      {!isFallback && !active && (
        <button
          disabled={disabled}
          onClick={onFallback}
          className="font-mono text-[9.5px] text-soft hover:text-ink disabled:opacity-40"
        >
          set fallback
        </button>
      )}
    </div>
  )
}

// ── custom servers ───────────────────────────────────────────

function CustomServers({
  data,
  onChanged,
  onSelect,
}: {
  data: ModelsOverview
  onChanged: () => void
  onSelect: (model: string) => void
}) {
  const [adding, setAdding] = useState(false)
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <SectionLabel>Custom servers</SectionLabel>
        <Button variant="secondary" className="ml-auto" onClick={() => setAdding(true)}>
          + Add server
        </Button>
      </div>
      <p className="text-[10.5px] text-muted">
        Any OpenAI-compatible endpoint: vLLM, MLC-LLM, llama.cpp, Ollama, LM Studio…
      </p>
      {adding && <ServerForm onDone={() => { setAdding(false); onChanged() }} onCancel={() => setAdding(false)} />}
      {data.custom.length === 0 && !adding ? (
        <EmptyState title="No custom servers yet. Add a local or remote inference server to use its models." />
      ) : (
        data.custom.map((s) => (
          <ServerCard key={s.id} server={s} data={data} onChanged={onChanged} onSelect={onSelect} />
        ))
      )}
    </div>
  )
}

function ServerForm({
  server,
  onDone,
  onCancel,
}: {
  server?: CustomServer
  onDone: () => void
  onCancel: () => void
}) {
  const [name, setName] = useState(server?.name ?? '')
  const [baseUrl, setBaseUrl] = useState(server?.base_url ?? '')
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      if (server) {
        const body: Record<string, unknown> = { name, base_url: baseUrl }
        if (apiKey) body.api_key = apiKey
        await api.patch(`/api/models/custom/${server.id}`, body)
      } else {
        const body: Record<string, unknown> = { name, base_url: baseUrl }
        if (apiKey) body.api_key = apiKey
        const created = await api.post<{ id: string }>('/api/models/custom', body)
        // Discover models right away so the server is usable immediately.
        await api.post(`/api/models/custom/${created.id}/probe`, { save: true }).catch(() => {})
      }
      onDone()
    } catch (e) {
      setError((e as ApiError)?.message ?? 'Could not save server.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="space-y-2">
      <SectionLabel>{server ? `Edit ${server.name}` : 'New server'}</SectionLabel>
      <TextInput
        aria-label="Server name"
        placeholder="Name (e.g. Jetson MLC)"
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <TextInput
        aria-label="Base URL"
        placeholder="Base URL (e.g. http://127.0.0.1:8000/v1)"
        value={baseUrl}
        onChange={(e) => setBaseUrl(e.target.value)}
        className="font-mono"
      />
      <TextInput
        aria-label="API key"
        type="password"
        autoComplete="new-password"
        placeholder={server?.api_key_set ? 'API key (leave blank to keep current)' : 'API key (optional)'}
        value={apiKey}
        onChange={(e) => setApiKey(e.target.value)}
        className="font-mono"
      />
      {error && <FieldError>{error}</FieldError>}
      <div className="flex gap-2 pt-1">
        <Button variant="primary" onClick={save} loading={busy} disabled={!baseUrl || (!server && !name)}>
          {server ? 'Save' : 'Add & discover models'}
        </Button>
        <Button variant="secondary" onClick={onCancel}>Cancel</Button>
      </div>
    </Card>
  )
}

function ServerCard({
  server,
  data,
  onChanged,
  onSelect,
}: {
  server: CustomServer
  data: ModelsOverview
  onChanged: () => void
  onSelect: (model: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [probeResult, setProbeResult] = useState<string | null>(null)

  const probe = useMutation({
    mutationFn: () => api.post<{ ok: boolean; models?: string[]; added?: string[]; error?: string }>(
      `/api/models/custom/${server.id}/probe`,
      { save: true },
    ),
    onSuccess: (result) => {
      if (!result.ok) {
        setProbeResult(`Unreachable: ${result.error}`)
      } else {
        const found = result.models?.length ?? 0
        const added = result.added?.length ?? 0
        setProbeResult(`Online — ${found} model${found === 1 ? '' : 's'}${added ? `, ${added} new added` : ''}`)
        onChanged()
      }
    },
    onError: (error) => setProbeResult((error as ApiError)?.message ?? 'Probe failed.'),
  })

  const remove = useMutation({
    mutationFn: () => api.delete(`/api/models/custom/${server.id}`),
    onSuccess: () => onChanged(),
    onError: (error) => setProbeResult((error as ApiError)?.message ?? 'Could not delete server.'),
  })

  const toggleVision = useMutation({
    mutationFn: (modelId: string) =>
      api.patch(`/api/models/custom/${server.id}`, {
        models: server.models.map((m) => ({
          id: m.id,
          name: m.name === m.id ? '' : m.name,
          vision: m.id === modelId ? !m.vision : m.vision,
          max_tokens: m.max_tokens,
        })),
      }),
    onSuccess: () => onChanged(),
  })

  if (editing) {
    return <ServerForm server={server} onDone={() => { setEditing(false); onChanged() }} onCancel={() => setEditing(false)} />
  }

  return (
    <Card className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-semibold text-ink">{server.name}</span>
        <span className="truncate font-mono text-[10px] text-faint">{server.base_url}</span>
        <span className="ml-auto flex items-center gap-2">
          <Button variant="secondary" onClick={() => probe.mutate()} loading={probe.isPending}>
            Check & discover
          </Button>
          <button onClick={() => setEditing(true)} className="font-mono text-[10px] text-soft hover:text-ink">edit</button>
          <button onClick={() => setConfirmDelete(true)} className="font-mono text-[10px] text-err hover:opacity-80">delete</button>
        </span>
      </div>
      {probeResult && (
        <div role="status" className={`text-[10.5px] ${probeResult.startsWith('Online') ? 'text-ok' : 'text-err'}`}>
          {probeResult}
        </div>
      )}
      {server.models.length === 0 ? (
        <div className="text-[11px] text-muted">No models yet — hit “Check & discover”.</div>
      ) : (
        <div className="flex flex-col overflow-hidden rounded-[3px] border border-line">
          {server.models.map((m, i) => (
            <div key={m.id} className={`flex flex-wrap items-center gap-2 px-3 py-2 ${i ? 'border-t border-line' : ''}`}>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-[12px] font-medium text-mist">{m.name}</span>
                  {data.current.model === m.full_id && (
                    <span className="rounded-[2px] bg-acc px-1.5 py-px font-mono text-[8.5px] font-semibold text-onacc">ACTIVE</span>
                  )}
                  {data.current.fallback_model === m.full_id && (
                    <span className="rounded-[2px] bg-raised2 px-1.5 py-px font-mono text-[8.5px] text-mist">FALLBACK</span>
                  )}
                </div>
                <div className="truncate font-mono text-[9px] text-faint">{m.full_id}</div>
              </div>
              <span className="flex items-center gap-1.5">
                <span className="font-mono text-[9px] text-muted">vision</span>
                <Toggle
                  label={`Vision for ${m.name}`}
                  value={m.vision}
                  disabled={toggleVision.isPending}
                  onChange={() => toggleVision.mutate(m.id)}
                />
              </span>
              {data.current.model !== m.full_id && (
                <Button variant="secondary" onClick={() => onSelect(m.full_id)}>
                  Use
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
      <ConfirmDialog
        open={confirmDelete}
        title={`Delete ${server.name}?`}
        body="The server and its models will be removed from ragnarbot. The inference server itself is not touched."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          remove.mutate()
          setConfirmDelete(false)
        }}
        onCancel={() => setConfirmDelete(false)}
      />
    </Card>
  )
}
