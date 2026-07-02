// Settings: schema-driven config forms with reload semantics, cross-field
// validation (server-authoritative), secrets management, and appearance.

import { ReactNode, useMemo, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ApiError, ConfigField, SecretEntry, api } from '../lib/api'
import { Page } from '../app/shell'
import { Accent, Theme, applyTheme, loadTheme } from '../app/theme'
import {
  Button,
  ConfirmDialog,
  Dot,
  FieldError,
  ReloadBadge,
  Segmented,
  Select,
  SectionLabel,
  Skeleton,
  Stepper,
  TextInput,
  Toggle,
} from '../components/ui'

// ── section model ────────────────────────────────────────────

interface SectionDef {
  id: string
  title: string
  match: (path: string) => boolean
}

const SCHEMA_SECTIONS: SectionDef[] = [
  { id: 'model', title: 'Model & Agent', match: (p) => p.startsWith('agents.defaults.') },
  { id: 'fallback', title: 'Fallback', match: (p) => p.startsWith('agents.fallback.') },
  { id: 'telegram', title: 'Telegram', match: (p) => p.startsWith('channels.telegram.') },
  { id: 'voice', title: 'Voice', match: (p) => p.startsWith('transcription.') },
  { id: 'web', title: 'Web search', match: (p) => p.startsWith('tools.web.') },
  { id: 'exec', title: 'Shell', match: (p) => p.startsWith('tools.exec.') },
  { id: 'search', title: 'File search', match: (p) => p.startsWith('tools.search.') },
  { id: 'browser', title: 'Browser', match: (p) => p.startsWith('tools.browser.') },
  { id: 'recall', title: 'Recall', match: (p) => p.startsWith('tools.recall.') },
  { id: 'heartbeat', title: 'Heartbeat', match: (p) => p.startsWith('heartbeat.') },
  { id: 'hooks', title: 'Hooks server', match: (p) => p.startsWith('hooks.') },
  {
    id: 'gateway',
    title: 'Gateway & Web',
    match: (p) => p.startsWith('gateway.') || p.startsWith('web.') || p.startsWith('daemon.'),
  },
]

const MODEL_FIELDS = new Set(['agents.defaults.model', 'agents.fallback.model'])

// ── value helpers ────────────────────────────────────────────

function toStr(v: unknown): string {
  if (Array.isArray(v)) return JSON.stringify(v)
  return String(v)
}

function differs(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) !== JSON.stringify(b)
}

function creditsMention(msg: string): boolean {
  return /key|credential|secret|oauth|token/i.test(msg)
}

// ── main page ────────────────────────────────────────────────

export default function SettingsPage() {
  const qc = useQueryClient()
  const [active, setActive] = useState<string | null>(null)

  const { data: schema, isLoading } = useQuery({
    queryKey: ['config-schema'],
    queryFn: () => api.get<ConfigField[]>('/api/config/schema'),
  })
  const { data: diff } = useQuery({
    queryKey: ['config-diff'],
    queryFn: () =>
      api.get<{ path: string; default: unknown; current: unknown }[]>('/api/config/diff'),
  })
  const { data: secrets } = useQuery({
    queryKey: ['secrets'],
    queryFn: () =>
      api.get<{ secrets: SecretEntry[]; extra: { path: string; set: boolean }[] }>('/api/secrets'),
  })

  // local optimistic overrides + server field errors + pending restart set
  const [overrides, setOverrides] = useState<Record<string, unknown>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [pending, setPending] = useState<Record<string, 'warm' | 'cold'>>({})
  const [confirmRestart, setConfirmRestart] = useState(false)
  const [restarting, setRestarting] = useState(false)

  const getVal = (f: ConfigField): unknown => (f.path in overrides ? overrides[f.path] : f.value)

  const patchField = async (f: ConfigField, next: unknown) => {
    const prev = getVal(f)
    setOverrides((o) => ({ ...o, [f.path]: next }))
    setErrors((e) => {
      const n = { ...e }
      delete n[f.path]
      return n
    })
    try {
      await api.patch('/api/config', { path: f.path, value: toStr(next) })
      if (f.reload === 'warm' || f.reload === 'cold') {
        setPending((p) => ({ ...p, [f.path]: f.reload as 'warm' | 'cold' }))
      }
      qc.invalidateQueries({ queryKey: ['config-diff'] })
    } catch (err) {
      setOverrides((o) => ({ ...o, [f.path]: prev }))
      setErrors((e) => ({ ...e, [f.path]: err instanceof ApiError ? err.message : 'save failed' }))
    }
  }

  const doRestart = async () => {
    setConfirmRestart(false)
    setRestarting(true)
    try {
      await api.post('/api/restart')
    } catch {
      /* daemon restart may drop the connection; expected */
    } finally {
      setPending({})
      setRestarting(false)
    }
  }

  // fields grouped by section
  const grouped = useMemo(() => {
    const map: Record<string, ConfigField[]> = {}
    for (const f of schema ?? []) {
      const sec = SCHEMA_SECTIONS.find((s) => s.match(f.path))
      if (sec) (map[sec.id] ??= []).push(f)
    }
    return map
  }, [schema])

  const errorCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const path of Object.keys(errors)) {
      const sec = SCHEMA_SECTIONS.find((s) => s.match(path))
      if (sec) counts[sec.id] = (counts[sec.id] ?? 0) + 1
    }
    return counts
  }, [errors])

  const unsetSecrets = (secrets?.secrets ?? []).filter((s) => !s.set).length
  const pendingCount = Object.keys(pending).length
  const hasCold = Object.values(pending).some((r) => r === 'cold')

  const navItems: { id: string; title: string; badge?: ReactNode; divider?: boolean }[] = [
    ...SCHEMA_SECTIONS.map((s) => ({
      id: s.id,
      title: s.title,
      badge: errorCounts[s.id] ? (
        <span className="font-mono text-[10px] text-err">{errorCounts[s.id]} error</span>
      ) : undefined,
    })),
    {
      id: 'secrets',
      title: 'Secrets',
      divider: true,
      badge: unsetSecrets ? (
        <span className="font-mono text-[10px] text-muted">{unsetSecrets} unset</span>
      ) : undefined,
    },
    { id: 'appearance', title: 'Appearance' },
    {
      id: 'changed',
      title: 'Changed from defaults',
      badge: diff?.length ? (
        <span className="rounded-[2px] bg-acc/[.13] px-[6px] py-[1.5px] font-mono text-[9.5px] text-acc">
          {diff.length}
        </span>
      ) : undefined,
    },
  ]

  const desktopActive = active ?? 'model'

  const restartBar =
    pendingCount > 0 ? (
      <RestartBar
        count={pendingCount}
        reOnboard={hasCold}
        loading={restarting}
        onRestart={() => setConfirmRestart(true)}
      />
    ) : null

  const renderSection = (id: string) => {
    if (id === 'secrets') return <SecretsSection data={secrets} qc={qc} />
    if (id === 'appearance') return <AppearanceSection />
    if (id === 'changed')
      return <ChangedSection diff={diff ?? []} schema={schema ?? []} onReset={patchField} />
    return (
      <SchemaSection
        fields={grouped[id] ?? []}
        getVal={getVal}
        errors={errors}
        onChange={patchField}
        onGotoSecrets={() => setActive('secrets')}
      />
    )
  }

  if (isLoading) {
    return (
      <Page title="Settings">
        <div className="space-y-3 p-6">
          <Skeleton className="w-1/2" />
          <Skeleton className="w-2/3" />
          <Skeleton className="w-1/3" />
        </div>
      </Page>
    )
  }

  return (
    <Page title="Settings" actions={<div className="hidden lg:block">{restartBar}</div>}>
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

      {/* mobile sticky restart banner */}
      {restartBar && <div className="fixed inset-x-3 bottom-[84px] z-30 lg:hidden">{restartBar}</div>}

      <ConfirmDialog
        open={confirmRestart}
        title="Restart the daemon?"
        body="Pending changes will be applied. The connection may briefly drop."
        confirmLabel="Restart now"
        onConfirm={doRestart}
        onCancel={() => setConfirmRestart(false)}
      />
    </Page>
  )
}

// ── restart banner ───────────────────────────────────────────

function RestartBar({
  count,
  reOnboard,
  loading,
  onRestart,
}: {
  count: number
  reOnboard: boolean
  loading?: boolean
  onRestart: () => void
}) {
  return (
    <div className="flex items-center gap-3 rounded-[4px] border border-warn/30 bg-warn/10 px-3 py-2.5">
      <span className="flex-1 text-[12px] leading-tight text-ink">
        {count} change{count > 1 ? 's' : ''} {reOnboard ? 'require re-onboard' : 'pending restart'}
      </span>
      <Button variant="primary" onClick={onRestart} loading={loading}>
        Restart now
      </Button>
    </div>
  )
}

// ── schema-driven section ────────────────────────────────────

function SchemaSection({
  fields,
  getVal,
  errors,
  onChange,
  onGotoSecrets,
}: {
  fields: ConfigField[]
  getVal: (f: ConfigField) => unknown
  errors: Record<string, string>
  onChange: (f: ConfigField, v: unknown) => void
  onGotoSecrets: () => void
}) {
  if (!fields.length)
    return <div className="text-[12px] text-muted">No settings in this section.</div>
  return (
    <div className="flex flex-col gap-[18px]">
      {fields.map((f) => (
        <FieldRow
          key={f.path}
          field={f}
          value={getVal(f)}
          error={errors[f.path]}
          onChange={(v) => onChange(f, v)}
          onGotoSecrets={onGotoSecrets}
        />
      ))}
    </div>
  )
}

function FieldRow({
  field,
  value,
  error,
  onChange,
  onGotoSecrets,
}: {
  field: ConfigField
  value: unknown
  error?: string
  onChange: (v: unknown) => void
  onGotoSecrets: () => void
}) {
  const changed = differs(value, field.default)
  return (
    <div className="flex flex-col gap-[7px]">
      <div className="flex items-center gap-2">
        <span className="text-[12.5px] font-semibold text-ink">{field.label || field.path}</span>
        {changed && (
          <button
            onClick={() => onChange(field.default)}
            title={`reset to default: ${toStr(field.default)}`}
            className="font-mono text-[10px] text-muted hover:text-ink"
          >
            reset
          </button>
        )}
        <span className="ml-auto">
          <ReloadBadge reload={field.reload} />
        </span>
      </div>
      <FieldControl field={field} value={value} error={!!error} onChange={onChange} />
      {error && (
        <FieldError>
          <span>
            {error}
            {creditsMention(error) && (
              <>
                {' — '}
                <button onClick={onGotoSecrets} className="underline hover:text-ink">
                  add key in Secrets
                </button>
              </>
            )}
          </span>
        </FieldError>
      )}
    </div>
  )
}

function FieldControl({
  field,
  value,
  error,
  onChange,
}: {
  field: ConfigField
  value: unknown
  error: boolean
  onChange: (v: unknown) => void
}) {
  // model pickers
  if (MODEL_FIELDS.has(field.path) && field.options) {
    return <ModelSelect field={field} value={String(value ?? '')} onChange={onChange} />
  }
  // telegram allow list → tag editor
  if (field.type === 'list') {
    return <TagEditor value={Array.isArray(value) ? (value as string[]) : []} onChange={onChange} />
  }
  // booleans
  if (field.type === 'bool') {
    return <Toggle value={!!value} onChange={onChange} />
  }
  // enums
  if (field.enum && field.enum.length) {
    if (field.enum.length <= 4) {
      return <Segmented options={field.enum} value={String(value ?? field.enum[0])} onChange={onChange} />
    }
    return (
      <Select value={String(value ?? '')} onChange={(e) => onChange(e.target.value)}>
        {field.enum.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </Select>
    )
  }
  // numbers
  if (field.type === 'int' || field.type === 'float') {
    const ge = field.ge
    const le = field.le
    const step = field.type === 'float' ? 0.1 : 1
    const bounded = ge != null && le != null && le - ge <= 200
    if (bounded) {
      return (
        <div className="flex items-center gap-2">
          <Stepper
            value={Number(value ?? ge ?? 0)}
            onChange={(v) => onChange(field.type === 'float' ? Math.round(v * 10) / 10 : v)}
            min={ge}
            max={le}
            step={step}
          />
          <span className="font-mono text-[10px] text-faint">
            min {ge} · max {le}
          </span>
        </div>
      )
    }
    return (
      <TextInput
        type="number"
        error={error}
        value={String(value ?? '')}
        min={ge}
        max={le}
        step={step}
        onChange={(e) => {
          if (e.target.value !== '') onChange(Number(e.target.value))
        }}
        className="max-w-[220px] font-mono"
      />
    )
  }
  // fallback: text
  return (
    <TextInput
      error={error}
      value={String(value ?? '')}
      onChange={(e) => onChange(e.target.value)}
      className="font-mono"
    />
  )
}

function ModelSelect({
  field,
  value,
  onChange,
}: {
  field: ConfigField
  value: string
  onChange: (v: string) => void
}) {
  const options = field.options ?? []
  const selected = options.find((o) => o.id === value)
  const groups = useMemo(() => {
    const g: Record<string, typeof options> = {}
    for (const o of options) (g[o.provider_name] ??= []).push(o)
    return g
  }, [options])
  return (
    <div className="flex flex-col gap-1.5">
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— select model —</option>
        {Object.entries(groups).map(([prov, models]) => (
          <optgroup key={prov} label={prov}>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </optgroup>
        ))}
      </Select>
      {selected && (
        <span className="font-mono text-[9.5px] text-faint">
          {selected.provider_name}
          {selected.vision && ' · vision'}
          {selected.oauth && ' · oauth'}
          {selected.description && ` · ${selected.description}`}
        </span>
      )}
    </div>
  )
}

function TagEditor({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  const [draft, setDraft] = useState('')
  const add = () => {
    const t = draft.trim()
    if (t && !value.includes(t)) onChange([...value, t])
    setDraft('')
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-[3px] border border-line2 bg-raised px-2.5 py-2">
      {value.map((t) => (
        <span
          key={t}
          className="inline-flex items-center gap-1.5 rounded-[2px] bg-raised2 px-2 py-1 font-mono text-[11px] text-mist"
        >
          {t}
          <button
            onClick={() => onChange(value.filter((x) => x !== t))}
            className="text-faint hover:text-err"
          >
            ×
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder="add…"
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault()
            add()
          }
        }}
        onBlur={add}
        className="min-w-[80px] flex-1 bg-transparent py-1 text-[12px] text-ink outline-none placeholder:text-muted"
      />
    </div>
  )
}

// ── changed-from-defaults view ───────────────────────────────

function ChangedSection({
  diff,
  schema,
  onReset,
}: {
  diff: { path: string; default: unknown; current: unknown }[]
  schema: ConfigField[]
  onReset: (f: ConfigField, v: unknown) => void
}) {
  if (!diff.length)
    return <div className="text-[12px] text-muted">Everything is at its default value.</div>
  return (
    <div className="flex flex-col gap-2">
      {diff.map((d) => {
        const field = schema.find((f) => f.path === d.path)
        return (
          <div
            key={d.path}
            className="flex items-center gap-3 rounded-[4px] border border-line bg-raised px-3 py-2.5"
          >
            <div className="min-w-0 flex-1">
              <div className="font-mono text-[11px] text-mist">{d.path}</div>
              <div className="mt-0.5 font-mono text-[10px] text-faint">
                {toStr(d.default)} → <span className="text-acc">{toStr(d.current)}</span>
              </div>
            </div>
            {field && (
              <button
                onClick={() => onReset(field, field.default)}
                className="font-mono text-[10px] text-muted hover:text-ink"
              >
                reset
              </button>
            )}
          </div>
        )
      })}
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
  const [revealed, setRevealed] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)

  const reveal = async () => {
    if (revealed !== null) {
      setRevealed(null)
      return
    }
    const r = await api.post<{ value: string }>('/api/secrets/reveal', { path: entry.path })
    setRevealed(r.value)
  }
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

  const tail = entry.path.replace(/^(providers|services|channels)\./, '')
  return (
    <div className={`flex flex-wrap items-center gap-2 px-3 py-3 ${first ? '' : 'border-t border-line'}`}>
      <Dot color={entry.set ? 'ok' : 'err'} />
      <span className="font-mono text-[11.5px] text-ink">{tail}</span>
      {editing ? (
        <div className="ml-auto flex w-full items-center gap-2 sm:w-auto">
          <TextInput
            autoFocus
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
            {entry.set ? (revealed !== null ? revealed : '••••••') : 'not set'}
          </span>
          {entry.set && (
            <button
              onClick={reveal}
              className="rounded-[2px] bg-raised2 px-2 py-1.5 font-mono text-[10px] text-soft hover:text-ink"
            >
              {revealed !== null ? 'hide' : 'reveal'}
            </button>
          )}
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
        {extra.map((e, i) => (
          <div
            key={e.path}
            className={`flex items-center gap-2 px-3 py-3 ${i === 0 ? '' : 'border-t border-line'}`}
          >
            <span className="font-mono text-[11.5px] text-ink">{e.path.replace(/^extra\./, '')}</span>
            <span className="ml-auto font-mono text-[11px] text-muted">••••••</span>
          </div>
        ))}
        {adding ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-3">
            <TextInput
              value={key}
              placeholder="KEY_NAME"
              onChange={(e) => setKey(e.target.value)}
              className="max-w-[160px] font-mono"
            />
            <TextInput
              value={val}
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
