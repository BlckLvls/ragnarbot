// Settings: intentionally small deep controls. Operational config stays agent-managed.

import { ReactNode, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { ApiError, SecretEntry, StatusFull, api } from '../lib/api'
import { Page } from '../app/shell'
import { Accent, Theme, applyTheme, loadTheme } from '../app/theme'
import {
  Button,
  Card,
  ConfirmDialog,
  Dot,
  Segmented,
  SectionLabel,
  Skeleton,
  TextInput,
  Toggle,
} from '../components/ui'

interface SoulConfigField {
  path: string
  value: boolean
  label: string
}

// ── main page ────────────────────────────────────────────────

export default function SettingsPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [active, setActive] = useState<string | null>(null)

  const { data: config } = useQuery({
    queryKey: ['config-schema'],
    queryFn: () => api.get<SoulConfigField[]>('/api/config/schema'),
  })
  const { data: secrets } = useQuery({
    queryKey: ['secrets'],
    queryFn: () =>
      api.get<{ secrets: SecretEntry[]; extra: { path: string; set: boolean }[] }>('/api/secrets'),
  })

  const unsetSecrets = (secrets?.secrets ?? []).filter((s) => !s.set).length

  const navItems: { id: string; title: string; badge?: ReactNode; divider?: boolean }[] = [
    {
      id: 'secrets',
      title: 'Secrets',
      badge: unsetSecrets ? (
        <span className="font-mono text-[10px] text-muted">{unsetSecrets} unset</span>
      ) : undefined,
    },
    { id: 'appearance', title: 'Appearance' },
    { id: 'experimental', title: 'Experimental', divider: true },
    { id: 'integrations', title: 'Integrations' },
    { id: 'system', title: 'System' },
    { id: 'diagnostics', title: 'Diagnostics' },
  ]

  const desktopActive = active ?? 'secrets'

  const renderSection = (id: string) => {
    if (id === 'secrets') return <SecretsSection data={secrets} qc={qc} />
    if (id === 'appearance') return <AppearanceSection />
    if (id === 'experimental') return <SoulSection field={config?.[0]} />
    if (id === 'integrations') return <IntegrationsSection onOpenHooks={() => navigate('/hooks')} />
    if (id === 'system') return <SystemSection />
    if (id === 'diagnostics') return <DiagnosticsSection />
    return null
  }

  return (
    <Page title="Settings">
      <div className="flex h-full min-h-0">
        {/* desktop sub-nav */}
        <div className="hidden w-[220px] min-w-[220px] flex-col gap-[1px] overflow-y-auto border-r border-line bg-panel p-2 lg:flex">
          {navItems.map((it) => (
            <button
              key={it.id}
              onClick={() => setActive(it.id)}
              className={`flex items-center gap-2 rounded-[3px] px-2.5 py-2 text-left text-[12.5px] ${
                it.divider ? 'mt-1.5 border-t border-line pt-3' : ''
              } ${desktopActive === it.id ? 'bg-raised2 text-ink' : 'text-soft hover:bg-raised2/60'}`}
            >
              <span className="flex-1 truncate">{it.title}</span>
              {it.badge}
            </button>
          ))}
        </div>

        {/* main column */}
        <div className="min-h-0 flex-1 overflow-y-auto">
          {/* mobile section list */}
          {active === null && (
            <div className="lg:hidden">
              {navItems.map((it) => (
                <button
                  key={it.id}
                  onClick={() => setActive(it.id)}
                  className={`flex w-full items-center gap-3 px-4 py-[13px] text-left hover:bg-raised2/50 ${
                    it.divider ? 'mt-1.5 border-t border-line' : ''
                  }`}
                >
                  <span className="flex-1 text-[13.5px] font-medium text-ink">{it.title}</span>
                  {it.badge}
                  <span className="text-faint">›</span>
                </button>
              ))}
            </div>
          )}

          {/* section form — mobile shows only when opened; desktop always */}
          <div className={active === null ? 'hidden lg:block' : ''}>
            <div className="flex items-center gap-2 border-b border-line px-4 py-3 lg:hidden">
              <button
                onClick={() => setActive(null)}
                className="inline-flex items-center gap-1 text-[13px] text-soft"
              >
                <span className="text-[15px]">‹</span> Settings
              </button>
              <span className="ml-1 text-[14px] font-semibold text-ink">
                {navItems.find((n) => n.id === desktopActive)?.title}
              </span>
            </div>
            <div className="mx-auto max-w-[640px] px-5 py-6 lg:px-8">
              {renderSection(desktopActive)}
            </div>
          </div>
        </div>
      </div>

    </Page>
  )
}

// ── deep settings ─────────────────────────────────────────────

function SoulSection({ field }: { field?: SoulConfigField }) {
  const qc = useQueryClient()
  const [optimistic, setOptimistic] = useState<boolean | null>(null)
  const update = useMutation({
    mutationFn: (value: boolean) =>
      api.patch('/api/config', {
        path: 'agents.defaults.experimental_soul',
        value: String(value),
      }),
    onSuccess: async () => {
      await qc.invalidateQueries({ queryKey: ['config-schema'] })
      setOptimistic(null)
    },
    onError: () => setOptimistic(null),
  })
  if (!field) return <Skeleton className="w-1/2" />
  const enabled = optimistic ?? Boolean(field.value)
  return (
    <Card className="flex items-center gap-3">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-semibold text-ink">Soul</div>
        <div className="mt-1 text-[11px] leading-relaxed text-soft">
          Apply the experimental soul prompt to the agent globally.
        </div>
        {update.isError && <div className="mt-2 text-[10.5px] text-err">Could not update Soul.</div>}
      </div>
      <Toggle
        label="Soul"
        value={enabled}
        disabled={update.isPending}
        onChange={(value) => {
          setOptimistic(value)
          update.mutate(value)
        }}
      />
    </Card>
  )
}

function IntegrationsSection({ onOpenHooks }: { onOpenHooks: () => void }) {
  return (
    <Card className="flex items-center gap-3">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-semibold text-ink">Hooks</div>
        <div className="mt-1 text-[11px] leading-relaxed text-soft">
          Manage incoming integrations, copy endpoint URLs, and inspect trigger history.
        </div>
      </div>
      <Button variant="secondary" onClick={onOpenHooks}>Open hooks</Button>
    </Card>
  )
}

function SystemSection() {
  const [checkResult, setCheckResult] = useState<string | null>(null)
  const [confirmRestart, setConfirmRestart] = useState(false)
  const [confirmUpdate, setConfirmUpdate] = useState(false)
  const status = useQuery({ queryKey: ['status-full'], queryFn: () => api.get<StatusFull>('/api/status/full') })
  const restart = useMutation({ mutationFn: () => api.post('/api/restart') })
  const check = useMutation({
    mutationFn: () => api.post<{ detail: string }>('/api/update/check'),
    onSuccess: (data) => setCheckResult(data.detail),
    onError: (error) => setCheckResult((error as ApiError)?.message ?? 'check failed'),
  })
  const update = useMutation({ mutationFn: () => api.post('/api/update/run') })
  const data = status.data

  return (
    <div className="space-y-3">
      <Card className="space-y-2">
        <SectionLabel>Runtime</SectionLabel>
        {data ? (
          <>
            <div className="flex items-center gap-2">
              <span className="text-[17px] font-bold text-ink">v{data.version}</span>
              <span className="rounded-[2px] bg-raised2 px-2 py-0.5 font-mono text-[9.5px] text-mist">{data.profile}</span>
              <span className="ml-auto font-mono text-[10px] text-soft">gateway online</span>
            </div>
            <div className="truncate font-mono text-[10px] text-faint">{data.workspace}</div>
          </>
        ) : <Skeleton className="w-2/3" />}
        <div className="flex flex-wrap gap-2 pt-1">
          <Button variant="secondary" onClick={() => setConfirmRestart(true)}>Restart gateway</Button>
          <Button variant="secondary" onClick={() => check.mutate()} loading={check.isPending}>Check for updates</Button>
          <Button variant="primary" onClick={() => setConfirmUpdate(true)}>Update now</Button>
        </div>
      </Card>
      {checkResult && (
        <pre className="whitespace-pre-wrap rounded-[3px] bg-deep p-3 font-mono text-[10.5px] text-mist">{checkResult}</pre>
      )}
      <ConfirmDialog
        open={confirmRestart}
        title="Restart gateway?"
        body="The connection will briefly drop and reconnect automatically."
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
        body="This updates ragnarbot and restarts the gateway."
        confirmLabel="Update"
        onConfirm={() => {
          update.mutate()
          setConfirmUpdate(false)
        }}
        onCancel={() => setConfirmUpdate(false)}
      />
    </div>
  )
}

function DiagnosticsSection() {
  const logs = useQuery({
    queryKey: ['logs-tail'],
    queryFn: () => api.get<{ lines: string[]; path: string }>('/api/logs/tail?lines=150'),
    refetchOnWindowFocus: false,
  })
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <SectionLabel>Gateway log</SectionLabel>
        <Button variant="secondary" className="ml-auto" onClick={() => logs.refetch()} loading={logs.isFetching}>
          Refresh
        </Button>
      </div>
      <pre className="max-h-[60vh] overflow-auto whitespace-pre-wrap break-all rounded-[3px] bg-deep p-3 font-mono text-[10px] leading-relaxed text-mist">
        {logs.isLoading ? 'Loading…' : logs.data?.lines.join('\n') || 'No log lines'}
      </pre>
      {logs.data?.path && <div className="font-mono text-[9px] text-faint">{logs.data.path}</div>}
    </div>
  )
}

// ── secrets ──────────────────────────────────────────────────

const SECRET_GROUPS: { id: string; title: string; match: (p: string) => boolean }[] = [
  { id: 'providers', title: 'LLM Providers', match: (p) => p.startsWith('providers.') },
  { id: 'services', title: 'Services', match: (p) => p.startsWith('services.') },
  { id: 'telegram', title: 'Telegram', match: (p) => p.startsWith('channels.') },
]

function SecretsSection({
  data,
  qc,
}: {
  data?: { secrets: SecretEntry[]; extra: { path: string; set: boolean }[] }
  qc: ReturnType<typeof useQueryClient>
}) {
  if (!data) return <Skeleton className="w-1/2" />
  const invalidate = () => qc.invalidateQueries({ queryKey: ['secrets'] })
  return (
    <div className="flex flex-col gap-5">
      {SECRET_GROUPS.map((g) => {
        const rows = data.secrets.filter((s) => g.match(s.path))
        if (!rows.length) return null
        return (
          <div key={g.id} className="flex flex-col gap-2">
            <SectionLabel>{g.title}</SectionLabel>
            <div className="flex flex-col overflow-hidden rounded-[4px] border border-line bg-raised">
              {rows.map((s, i) => (
                <SecretRow key={s.path} entry={s} first={i === 0} onSaved={invalidate} />
              ))}
            </div>
          </div>
        )
      })}
      <CustomSecrets extra={data.extra} onChanged={invalidate} />
    </div>
  )
}

function SecretRow({
  entry,
  first,
  onSaved,
}: {
  entry: SecretEntry
  first: boolean
  onSaved: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      await api.put('/api/secrets', { path: entry.path, value: draft })
      setEditing(false)
      setDraft('')
      onSaved()
    } finally {
      setBusy(false)
    }
  }

  const tail = entry.path.replace(/^(providers|services|channels|extra)\./, '')
  return (
    <div className={`flex flex-wrap items-center gap-2 px-3 py-3 ${first ? '' : 'border-t border-line'}`}>
      <Dot color={entry.set ? 'ok' : 'err'} />
      <span className="font-mono text-[11.5px] text-ink">{tail}</span>
      {editing ? (
        <div className="ml-auto flex w-full items-center gap-2 sm:w-auto">
          <TextInput
            autoFocus
            type="password"
            autoComplete="new-password"
            aria-label={`New value for ${tail}`}
            value={draft}
            placeholder="paste value"
            onChange={(e) => setDraft(e.target.value)}
            className="min-w-[160px] font-mono"
          />
          <Button variant="primary" onClick={save} loading={busy}>
            Save
          </Button>
          <button onClick={() => setEditing(false)} className="text-muted hover:text-ink text-[14px]">
            ×
          </button>
        </div>
      ) : (
        <>
          <span className="ml-auto font-mono text-[11px] text-muted">
            {entry.set ? '••••••' : 'not set'}
          </span>
          <button
            onClick={() => {
              setEditing(true)
              setDraft('')
            }}
            className="rounded-[2px] bg-raised2 px-2 py-1.5 font-mono text-[10px] text-soft hover:text-ink"
          >
            {entry.set ? 'edit' : 'add'}
          </button>
          {!entry.set && entry.api_key_url && (
            <a
              href={entry.api_key_url}
              target="_blank"
              rel="noreferrer"
              className="whitespace-nowrap font-mono text-[10px] text-acc hover:opacity-80"
            >
              get a key ↗
            </a>
          )}
        </>
      )}
    </div>
  )
}

function CustomSecrets({
  extra,
  onChanged,
}: {
  extra: { path: string; set: boolean }[]
  onChanged: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [key, setKey] = useState('')
  const [val, setVal] = useState('')
  const [busy, setBusy] = useState(false)

  const add = async () => {
    if (!key.trim()) return
    setBusy(true)
    try {
      await api.put('/api/secrets', { path: `extra.${key.trim()}`, value: val })
      setKey('')
      setVal('')
      setAdding(false)
      onChanged()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-col gap-2">
      <SectionLabel>Custom</SectionLabel>
      <div className="flex flex-col overflow-hidden rounded-[4px] border border-line bg-raised">
        {extra.map((entry, i) => (
          <SecretRow key={entry.path} entry={entry} first={i === 0} onSaved={onChanged} />
        ))}
        {adding ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-3">
            <TextInput
              value={key}
              aria-label="Custom secret name"
              placeholder="KEY_NAME"
              onChange={(e) => setKey(e.target.value)}
              className="max-w-[160px] font-mono"
            />
            <TextInput
              type="password"
              autoComplete="new-password"
              value={val}
              aria-label="Custom secret value"
              placeholder="value"
              onChange={(e) => setVal(e.target.value)}
              className="min-w-[140px] flex-1 font-mono"
            />
            <Button variant="primary" onClick={add} loading={busy}>
              Add
            </Button>
            <button onClick={() => setAdding(false)} className="text-muted hover:text-ink text-[14px]">
              ×
            </button>
          </div>
        ) : (
          <button
            onClick={() => setAdding(true)}
            className={`px-3 py-3 text-left text-[11.5px] text-acc hover:opacity-80 ${
              extra.length ? 'border-t border-line' : ''
            }`}
          >
            + Add variable
          </button>
        )}
      </div>
    </div>
  )
}

// ── appearance ───────────────────────────────────────────────

const THEMES: readonly Theme[] = ['dark', 'light']
const ACCENTS: readonly Accent[] = ['amber', 'cyan', 'bone', 'ember']

function AppearanceSection() {
  const [pref, setPref] = useState(() => loadTheme())
  const set = (theme: Theme, accent: Accent) => {
    applyTheme(theme, accent)
    setPref({ theme, accent })
  }
  return (
    <div className="flex flex-col gap-[18px]">
      <div className="flex flex-col gap-[7px]">
        <span className="text-[12.5px] font-semibold text-ink">Theme</span>
        <Segmented options={THEMES} value={pref.theme} onChange={(t) => set(t, pref.accent)} />
      </div>
      <div className="flex flex-col gap-[7px]">
        <span className="text-[12.5px] font-semibold text-ink">Accent</span>
        <Segmented options={ACCENTS} value={pref.accent} onChange={(a) => set(pref.theme, a)} />
      </div>
      <p className="text-[11px] text-muted">Stored in this browser only.</p>
    </div>
  )
}
