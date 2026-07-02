// Memory & Files: recall search + memory/daily-note pins + workspace file tree + split editor.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, RecallResult, WorkspaceEntry } from '../lib/api'
import { Page } from '../app/shell'
import { Markdown } from '../components/markdown'
import {
  Button,
  Card,
  EmptyState,
  SectionLabel,
  Segmented,
  Skeleton,
  TextInput,
  Toast,
} from '../components/ui'

const SCOPES = ['memory', 'chats', 'both'] as const
const TABS = ['memory', 'files'] as const
const EDIT_MODES = ['edit', 'preview'] as const

const PINS: { label: string; path: string; heartbeat?: boolean }[] = [
  { label: 'MEMORY.md', path: 'memory/MEMORY.md' },
  { label: 'IDENTITY.md', path: 'IDENTITY.md' },
  { label: 'USER.md', path: 'USER.md' },
  { label: 'TOOLS.md', path: 'TOOLS.md' },
  { label: 'HEARTBEAT.md', path: 'HEARTBEAT.md', heartbeat: true },
]

const DAILY_RE = /^memory\/\d{4}-\d{2}-\d{2}\.md$/

type SearchOutcome = { notReady: string } | { results: RecallResult[] }

function fmtSize(n: number | null): string {
  if (n == null) return ''
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${n} B`
}

// ── recall search ────────────────────────────────────────────

function ResultCard({ r }: { r: RecallResult }) {
  const label = String(r.source ?? r.path ?? r.title ?? r.id ?? '')
  const text = String(r.text ?? r.snippet ?? r.content ?? r.body ?? '')
  const score = typeof r.score === 'number' ? r.score : null
  const date = r.date ?? r.timestamp ?? r.created_at
  return (
    <Card>
      {(label || score != null) && (
        <div className="flex items-baseline gap-2">
          {label && <span className="truncate font-mono text-[10px] text-acc">{label}</span>}
          {score != null && (
            <span className="ml-auto font-mono text-[9.5px] text-faint">score {score.toFixed(2)}</span>
          )}
        </div>
      )}
      {text && (
        <div className="mt-1.5 line-clamp-4 whitespace-pre-wrap text-[11.5px] leading-relaxed text-soft">
          {text}
        </div>
      )}
      {date != null && String(date) !== '' && (
        <div className="mt-1 font-mono text-[9px] text-faint">{String(date)}</div>
      )}
    </Card>
  )
}

function RecallSearch() {
  const [query, setQuery] = useState('')
  const [scope, setScope] = useState<(typeof SCOPES)[number]>('both')
  const [results, setResults] = useState<RecallResult[] | null>(null)
  const [notReady, setNotReady] = useState<string | null>(null)

  const search = useMutation<SearchOutcome>({
    mutationFn: async () => {
      const res = await fetch('/api/recall/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, scope, top_k: 8 }),
      })
      const data = await res.json().catch(() => ({}))
      if (res.status === 503) return { notReady: String(data.status ?? data.error ?? 'building') }
      if (!res.ok) throw new ApiError(data?.error || `HTTP ${res.status}`, res.status)
      return { results: (data.results ?? []) as RecallResult[] }
    },
    onSuccess: (d) => {
      if ('notReady' in d) {
        setNotReady(d.notReady)
        setResults(null)
      } else {
        setNotReady(null)
        setResults(d.results)
      }
    },
  })

  const run = () => {
    if (query.trim()) search.mutate()
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="flex-1">
          <TextInput
            value={query}
            placeholder="Search memory & chats…"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && run()}
          />
        </div>
        <div className="flex items-center gap-2">
          <Segmented options={SCOPES} value={scope} onChange={setScope} />
          <Button variant="primary" onClick={run} loading={search.isPending} className="min-h-[38px]">
            Search
          </Button>
        </div>
      </div>

      {notReady && (
        <div className="rounded-[4px] border border-warn/30 bg-warn/10 px-3 py-2 text-[11.5px] text-warn">
          recall index not ready: {notReady}
        </div>
      )}
      {search.isError && !notReady && (
        <div className="rounded-[4px] border border-err/30 bg-err/10 px-3 py-2 text-[11.5px] text-err">
          {(search.error as ApiError)?.message ?? 'search failed'}
        </div>
      )}
      {results && (
        <div className="grid grid-cols-1 gap-2 lg:grid-cols-3">
          {results.length === 0 ? (
            <div className="lg:col-span-3">
              <EmptyState title="No matches" />
            </div>
          ) : (
            results.map((r, i) => <ResultCard key={i} r={r} />)
          )}
        </div>
      )}
    </div>
  )
}

// ── file editor ──────────────────────────────────────────────

function Editor({
  path,
  onClose,
  onSaved,
  mobile,
}: {
  path: string
  onClose: () => void
  onSaved: () => void
  mobile?: boolean
}) {
  const [mode, setMode] = useState<(typeof EDIT_MODES)[number]>('edit')
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)

  const fileQuery = useQuery({
    queryKey: ['wsfile', path],
    queryFn: () =>
      api.get<{ path: string; content: string }>(`/api/workspace/file?path=${encodeURIComponent(path)}`),
    enabled: !!path,
    retry: false,
  })

  useEffect(() => {
    if (fileQuery.data) {
      setContent(fileQuery.data.content)
      setDirty(false)
    }
  }, [fileQuery.data])

  const save = useMutation({
    mutationFn: () => api.put('/api/workspace/file', { path, content }),
    onSuccess: () => {
      setDirty(false)
      onSaved()
    },
  })

  const err = fileQuery.error as ApiError | null

  return (
    <div className={`flex min-h-0 flex-col ${mobile ? 'h-full' : 'lg:h-[70vh]'}`}>
      <div className="flex items-center gap-2 border-b border-line pb-2">
        {mobile && (
          <button onClick={onClose} className="text-[13px] text-muted hover:text-ink">
            ‹
          </button>
        )}
        <span className="truncate font-mono text-[11px] text-mist">{path}</span>
        {dirty && <span className="h-[5px] w-[5px] flex-none bg-acc" title="unsaved" />}
        <div className="ml-auto flex items-center gap-2">
          <Segmented options={EDIT_MODES} value={mode} onChange={setMode} />
          <Button
            variant="primary"
            onClick={() => save.mutate()}
            loading={save.isPending}
            disabled={!dirty || fileQuery.isLoading || !!err}
          >
            Save
          </Button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto pt-3">
        {fileQuery.isLoading ? (
          <div className="space-y-2">
            <Skeleton className="w-3/4" />
            <Skeleton className="w-1/2" />
            <Skeleton className="w-2/3" />
          </div>
        ) : err ? (
          <div className="rounded-[4px] border border-err/30 bg-err/10 px-3 py-2 text-[11.5px] text-err">
            cannot open this file: {err.message}
          </div>
        ) : mode === 'preview' ? (
          <Markdown>{content}</Markdown>
        ) : (
          <textarea
            value={content}
            onChange={(e) => {
              setContent(e.target.value)
              setDirty(true)
            }}
            spellCheck={false}
            className="min-h-[400px] w-full flex-1 resize-none rounded-[3px] border border-line2 bg-raised px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-ink outline-none focus:border-acc/50 lg:min-h-full"
          />
        )}
      </div>
      {save.isError && (
        <div className="mt-2 text-[11px] text-err">{(save.error as ApiError)?.message ?? 'save failed'}</div>
      )}
    </div>
  )
}

// ── pins & files list ────────────────────────────────────────

function PinRow({
  label,
  active,
  onClick,
  right,
}: {
  label: string
  active: boolean
  onClick: () => void
  right?: React.ReactNode
}) {
  return (
    <div
      onClick={onClick}
      className={`flex min-h-[44px] cursor-pointer items-center gap-2 rounded-[3px] px-2.5 ${
        active ? 'bg-raised2' : 'hover:bg-raised2/50'
      }`}
    >
      <span className={`flex-1 truncate font-mono text-[11.5px] ${active ? 'text-ink' : 'text-soft'}`}>
        {label}
      </span>
      {right}
    </div>
  )
}

// ── page ─────────────────────────────────────────────────────

export default function MemoryPage() {
  const qc = useQueryClient()
  const [tab, setTab] = useState<(typeof TABS)[number]>('memory')
  const [selected, setSelected] = useState<string | null>(null)
  const [toast, setToast] = useState<string | null>(null)

  const tree = useQuery({
    queryKey: ['workspace-tree'],
    queryFn: () => api.get<WorkspaceEntry[]>('/api/workspace/tree'),
  })

  const heartbeat = useMutation({
    mutationFn: () => api.post('/api/heartbeat/trigger'),
    onSuccess: () => setToast('Heartbeat triggered'),
  })

  const dailyNotes = useMemo(() => {
    return (tree.data ?? [])
      .filter((e) => !e.dir && DAILY_RE.test(e.path))
      .sort((a, b) => b.path.localeCompare(a.path))
  }, [tree.data])

  const files = useMemo(() => {
    return (tree.data ?? []).slice().sort((a, b) => a.path.localeCompare(b.path))
  }, [tree.data])

  const onSaved = () => {
    setToast('Saved')
    qc.invalidateQueries({ queryKey: ['workspace-tree'] })
  }

  return (
    <Page title="Memory">
      <div className="p-4 lg:p-6">
        <div className="mb-4">
          <RecallSearch />
        </div>

        <div className="lg:flex lg:gap-5">
          {/* left column: tabs + list */}
          <div className="lg:w-[320px] lg:flex-none">
            <div className="mb-3">
              <Segmented
                options={TABS}
                value={tab}
                onChange={setTab}
                labels={{ memory: 'Memory', files: 'Files' }}
              />
            </div>

            {tab === 'memory' ? (
              <div className="space-y-4">
                <div>
                  <SectionLabel className="mb-1 px-1">Pinned</SectionLabel>
                  {PINS.map((p) => (
                    <PinRow
                      key={p.path}
                      label={p.label}
                      active={selected === p.path}
                      onClick={() => setSelected(p.path)}
                      right={
                        p.heartbeat ? (
                          <button
                            onClick={(e) => {
                              e.stopPropagation()
                              heartbeat.mutate()
                            }}
                            disabled={heartbeat.isPending}
                            className="rounded-[2px] bg-acc/[.13] px-2 py-1 font-mono text-[9.5px] font-semibold text-acc hover:bg-acc/20 disabled:opacity-40"
                          >
                            Trigger now
                          </button>
                        ) : undefined
                      }
                    />
                  ))}
                </div>
                <div>
                  <SectionLabel className="mb-1 px-1">Daily notes</SectionLabel>
                  {dailyNotes.length === 0 ? (
                    <div className="px-2.5 py-2 text-[11px] text-muted">No daily notes yet</div>
                  ) : (
                    dailyNotes.map((e) => (
                      <PinRow
                        key={e.path}
                        label={e.path.replace(/^memory\//, '')}
                        active={selected === e.path}
                        onClick={() => setSelected(e.path)}
                      />
                    ))
                  )}
                </div>
              </div>
            ) : (
              <div className="max-h-[60vh] overflow-y-auto">
                {tree.isLoading ? (
                  <div className="space-y-2 p-2">
                    <Skeleton className="w-2/3" />
                    <Skeleton className="w-1/2" />
                    <Skeleton className="w-3/4" />
                  </div>
                ) : files.length === 0 ? (
                  <EmptyState title="Workspace is empty" />
                ) : (
                  files.map((e) => {
                    const depth = e.path.split('/').length - 1
                    const name = e.path.split('/').pop() ?? e.path
                    if (e.dir) {
                      return (
                        <div
                          key={e.path}
                          className="flex min-h-[36px] items-center font-mono text-[11.5px] font-semibold text-mist"
                          style={{ paddingLeft: 10 + depth * 14 }}
                        >
                          {name}/
                        </div>
                      )
                    }
                    return (
                      <div
                        key={e.path}
                        onClick={() => setSelected(e.path)}
                        style={{ paddingLeft: 10 + depth * 14 }}
                        className={`flex min-h-[44px] cursor-pointer items-center gap-2 rounded-[3px] pr-2.5 ${
                          selected === e.path ? 'bg-raised2' : 'hover:bg-raised2/50'
                        }`}
                      >
                        <span
                          className={`flex-1 truncate font-mono text-[11.5px] ${
                            selected === e.path ? 'text-ink' : 'text-soft'
                          }`}
                        >
                          {name}
                        </span>
                        <span className="font-mono text-[9.5px] text-faint">{fmtSize(e.size)}</span>
                      </div>
                    )
                  })
                )}
              </div>
            )}
          </div>

          {/* desktop editor pane */}
          <div className="mt-4 hidden min-w-0 flex-1 rounded-[4px] border border-line bg-raised p-3 lg:mt-0 lg:block">
            {selected ? (
              <Editor key={selected} path={selected} onClose={() => setSelected(null)} onSaved={onSaved} />
            ) : (
              <EmptyState title="Pick a file to edit" />
            )}
          </div>
        </div>
      </div>

      {/* mobile editor overlay */}
      {selected && (
        <div className="fixed inset-0 z-40 flex flex-col bg-page p-4 pt-safe lg:hidden">
          <Editor key={selected} path={selected} onClose={() => setSelected(null)} onSaved={onSaved} mobile />
        </div>
      )}

      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </Page>
  )
}
