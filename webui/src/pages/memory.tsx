// Files: editable workspace documents with pinned memory files and downloads.

import { useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError, WorkspaceEntry } from '../lib/api'
import { Page } from '../app/shell'
import { Markdown } from '../components/markdown'
import { Button, ConfirmDialog, EmptyState, SectionLabel, Segmented, Skeleton, Toast } from '../components/ui'

const EDIT_MODES = ['edit', 'preview'] as const

const PINS: { label: string; path: string }[] = [
  { label: 'MEMORY.md', path: 'memory/MEMORY.md' },
  { label: 'IDENTITY.md', path: 'IDENTITY.md' },
  { label: 'USER.md', path: 'USER.md' },
  { label: 'TOOLS.md', path: 'TOOLS.md' },
  { label: 'HEARTBEAT.md', path: 'HEARTBEAT.md' },
]

function fmtSize(n: number | null): string {
  if (n == null) return ''
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${n} B`
}

function downloadUrl(path: string): string {
  return `/api/workspace/download?path=${encodeURIComponent(path)}`
}

function Editor({
  path,
  onClose,
  onSaved,
  onDirtyChange,
  mobile,
}: {
  path: string
  onClose: () => void
  onSaved: () => void
  onDirtyChange: (dirty: boolean) => void
  mobile?: boolean
}) {
  const [mode, setMode] = useState<(typeof EDIT_MODES)[number]>('edit')
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)

  const setDirtyState = (next: boolean) => {
    setDirty(next)
    onDirtyChange(next)
  }

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
      setDirtyState(false)
    }
    // onDirtyChange is stable for the lifetime of this keyed editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileQuery.data])

  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!dirty) return
      event.preventDefault()
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  const save = useMutation({
    mutationFn: () => api.put('/api/workspace/file', { path, content }),
    onSuccess: () => {
      setDirtyState(false)
      onSaved()
    },
  })

  const err = fileQuery.error as ApiError | null

  return (
    <div className={`flex min-h-0 flex-col ${mobile ? 'h-full' : 'lg:h-[calc(100dvh-150px)]'}`}>
      <div className="flex items-center gap-2 border-b border-line pb-2">
        {mobile && (
          <button onClick={onClose} className="min-h-10 px-1 text-[13px] text-muted hover:text-ink" aria-label="Back to files">
            ‹
          </button>
        )}
        <span className="truncate font-mono text-[11px] text-mist">{path}</span>
        {dirty && <span className="h-[5px] w-[5px] flex-none bg-acc" title="unsaved" />}
        <div className="ml-auto flex items-center gap-2">
          <a
            href={downloadUrl(path)}
            download
            className="rounded-[3px] border border-line bg-raised2 px-2.5 py-1.5 text-[11px] text-soft hover:text-ink"
          >
            Download
          </a>
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
              if (!dirty) setDirtyState(true)
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

function FileRow({
  entry,
  active,
  onClick,
}: {
  entry: WorkspaceEntry
  active: boolean
  onClick: () => void
}) {
  const depth = entry.path.split('/').length - 1
  const name = entry.path.split('/').pop() ?? entry.path
  if (entry.dir) {
    return (
      <div
        className="flex min-h-[36px] items-center font-mono text-[11.5px] font-semibold text-mist"
        style={{ paddingLeft: 10 + depth * 14 }}
      >
        {name}/
      </div>
    )
  }
  return (
    <button
      onClick={onClick}
      style={{ paddingLeft: 10 + depth * 14 }}
      className={`flex min-h-[44px] w-full items-center gap-2 rounded-[3px] pr-2.5 text-left ${
        active ? 'bg-raised2' : 'hover:bg-raised2/50'
      }`}
    >
      <span className={`flex-1 truncate font-mono text-[11.5px] ${active ? 'text-ink' : 'text-soft'}`}>
        {name}
      </span>
      <span className="font-mono text-[9.5px] text-faint">{fmtSize(entry.size)}</span>
    </button>
  )
}

export default function FilesPage() {
  const qc = useQueryClient()
  const [searchParams] = useSearchParams()
  const [selected, setSelected] = useState<string | null>(() => searchParams.get('path'))
  const [dirty, setDirty] = useState(false)
  const [pendingSelection, setPendingSelection] = useState<string | null | undefined>(undefined)
  const [toast, setToast] = useState<string | null>(null)

  const tree = useQuery({
    queryKey: ['workspace-tree'],
    queryFn: () => api.get<WorkspaceEntry[]>('/api/workspace/tree'),
  })

  const files = useMemo(
    () => (tree.data ?? []).slice().sort((a, b) => a.path.localeCompare(b.path)),
    [tree.data],
  )
  const existingPaths = useMemo(() => new Set(files.filter((e) => !e.dir).map((e) => e.path)), [files])
  const pins = PINS.filter((p) => existingPaths.has(p.path))

  const choose = (next: string | null) => {
    if (next === selected) return
    if (dirty) {
      setPendingSelection(next)
      return
    }
    setSelected(next)
  }

  const onSaved = () => {
    setToast('Saved')
    qc.invalidateQueries({ queryKey: ['workspace-tree'] })
  }

  const editor = selected ? (
    <Editor
      key={selected}
      path={selected}
      onClose={() => choose(null)}
      onSaved={onSaved}
      onDirtyChange={setDirty}
    />
  ) : (
    <EmptyState title="Pick a file to edit" />
  )

  return (
    <Page title="Files">
      <div className="flex h-full min-h-0">
        <div className="w-full min-h-0 overflow-y-auto border-line bg-panel p-3 lg:w-[330px] lg:min-w-[330px] lg:border-r">
          {pins.length > 0 && (
            <div className="mb-4">
              <SectionLabel className="mb-1 px-1">Pinned</SectionLabel>
              {pins.map((pin) => (
                <button
                  key={pin.path}
                  onClick={() => choose(pin.path)}
                  className={`flex min-h-[44px] w-full items-center rounded-[3px] px-2.5 text-left font-mono text-[11.5px] ${
                    selected === pin.path ? 'bg-raised2 text-ink' : 'text-soft hover:bg-raised2/50'
                  }`}
                >
                  {pin.label}
                </button>
              ))}
            </div>
          )}

          <SectionLabel className="mb-1 px-1">Workspace</SectionLabel>
          {tree.isLoading ? (
            <div className="space-y-2 p-2">
              <Skeleton className="w-2/3" />
              <Skeleton className="w-1/2" />
              <Skeleton className="w-3/4" />
            </div>
          ) : files.length === 0 ? (
            <EmptyState title="Workspace is empty" />
          ) : (
            files.map((entry) => (
              <FileRow
                key={entry.path}
                entry={entry}
                active={selected === entry.path}
                onClick={() => !entry.dir && choose(entry.path)}
              />
            ))
          )}
        </div>

        <div className="hidden min-w-0 flex-1 p-4 lg:block">
          <div className="h-full rounded-[4px] border border-line bg-raised p-3">{editor}</div>
        </div>
      </div>

      {selected && (
        <div className="fixed inset-0 z-40 flex flex-col bg-page p-4 pt-safe lg:hidden">
          <Editor
            key={selected}
            path={selected}
            onClose={() => choose(null)}
            onSaved={onSaved}
            onDirtyChange={setDirty}
            mobile
          />
        </div>
      )}

      <ConfirmDialog
        open={pendingSelection !== undefined}
        title="Discard unsaved changes?"
        body="Your edits to the current file have not been saved."
        confirmLabel="Discard"
        destructive
        onConfirm={() => {
          setDirty(false)
          setSelected(pendingSelection ?? null)
          setPendingSelection(undefined)
        }}
        onCancel={() => setPendingSelection(undefined)}
      />

      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </Page>
  )
}
