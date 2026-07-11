// Files: a desktop-like workspace explorer with safe viewing, editing, and image previews.

import { KeyboardEvent, useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError, WorkspaceEntry } from '../lib/api'
import { Page } from '../app/shell'
import { Markdown } from '../components/markdown'
import { Button, ConfirmDialog, EmptyState, SectionLabel, Skeleton, Toast } from '../components/ui'

type TreeNode = WorkspaceEntry & {
  name: string
  children: TreeNode[]
}

const CORE_FILES = [
  { path: 'memory/MEMORY.md', label: 'Memory', description: 'Long-term context' },
  { path: 'IDENTITY.md', label: 'Identity', description: 'Who Ragnarbot is' },
  { path: 'USER.md', label: 'User', description: 'About you' },
  { path: 'TOOLS.md', label: 'Tools', description: 'Workspace capabilities' },
  { path: 'HEARTBEAT.md', label: 'Heartbeat', description: 'Autonomous routine' },
] as const

const EXPANDED_STORAGE_KEY = 'ragnarbot.workspace.expanded'

function fmtSize(n: number | null): string {
  if (n == null) return ''
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${n} B`
}

function fmtDate(timestamp: number | null): string {
  if (!timestamp) return 'Unknown'
  return new Date(timestamp).toLocaleString([], {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

function downloadUrl(path: string): string {
  return `/api/workspace/download?path=${encodeURIComponent(path)}`
}

function previewUrl(path: string): string {
  return `/api/workspace/preview?path=${encodeURIComponent(path)}`
}

function extensionLabel(path: string): string {
  const name = path.split('/').pop() ?? path
  const ext = name.includes('.') ? name.split('.').pop() : 'TXT'
  return (ext || 'TXT').slice(0, 4).toUpperCase()
}

function parentPaths(path: string): string[] {
  const parts = path.split('/')
  return parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join('/'))
}

function loadExpanded(): Set<string> {
  try {
    const value = JSON.parse(localStorage.getItem(EXPANDED_STORAGE_KEY) ?? '[]')
    return new Set(Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [])
  } catch {
    return new Set()
  }
}

function buildTree(entries: WorkspaceEntry[]): TreeNode[] {
  const nodes = new Map<string, TreeNode>()
  for (const entry of entries) {
    nodes.set(entry.path, {
      ...entry,
      name: entry.path.split('/').pop() ?? entry.path,
      children: [],
    })
  }

  const roots: TreeNode[] = []
  for (const node of nodes.values()) {
    const split = node.path.lastIndexOf('/')
    const parent = split === -1 ? null : nodes.get(node.path.slice(0, split))
    if (parent?.dir) parent.children.push(node)
    else roots.push(node)
  }

  const sortNodes = (items: TreeNode[]) => {
    items.sort((a, b) => {
      if (a.dir !== b.dir) return a.dir ? -1 : 1
      return a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
    })
    items.forEach((item) => sortNodes(item.children))
  }
  sortNodes(roots)
  return roots
}

function filterTree(nodes: TreeNode[], rawQuery: string): TreeNode[] {
  const query = rawQuery.trim().toLocaleLowerCase()
  if (!query) return nodes

  return nodes.flatMap((node) => {
    const ownMatch = node.path.toLocaleLowerCase().includes(query)
    if (ownMatch) return [node]
    if (!node.dir) return []
    const children = filterTree(node.children, query)
    return children.length ? [{ ...node, children }] : []
  })
}

function FileBadge({ entry, active = false }: { entry: WorkspaceEntry; active?: boolean }) {
  if (entry.dir) {
    return (
      <span className="relative h-[15px] w-[18px] flex-none" aria-hidden="true">
        <span className={`absolute left-[1px] top-0 h-[4px] w-[8px] rounded-t-[2px] ${active ? 'bg-acc' : 'bg-muted'}`} />
        <span className={`absolute inset-x-0 bottom-0 h-[12px] rounded-[2px] ${active ? 'bg-acc' : 'bg-soft'}`} />
      </span>
    )
  }
  return (
    <span
      className={`flex h-[22px] min-w-[27px] flex-none items-center justify-center rounded-[3px] border px-1 font-mono text-[7.5px] font-semibold tracking-tight ${
        active ? 'border-acc/60 bg-acc/10 text-acc' : 'border-line2 bg-raised2 text-muted'
      }`}
      aria-hidden="true"
    >
      {entry.kind === 'image' ? 'IMG' : entry.kind === 'video' ? 'VID' : extensionLabel(entry.path)}
    </span>
  )
}

function CoreFileCard({
  entry,
  label,
  description,
  active,
  onOpen,
}: {
  entry: WorkspaceEntry
  label: string
  description: string
  active: boolean
  onOpen: () => void
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className={`group min-w-0 rounded-[5px] border p-2.5 text-left transition-colors ${
        active
          ? 'border-acc/35 bg-acc/10'
          : 'border-line bg-raised/70 hover:border-line2 hover:bg-raised2/70'
      }`}
    >
      <div className="flex min-w-0 items-center gap-2">
        <FileBadge entry={entry} active={active} />
        <span className={`truncate text-[11.5px] font-semibold ${active ? 'text-acc' : 'text-mist group-hover:text-ink'}`}>
          {label}
        </span>
      </div>
      <div className="mt-1.5 truncate text-[9.5px] text-muted">{description}</div>
    </button>
  )
}

function TreeRow({
  node,
  level,
  expanded,
  forceExpanded,
  selected,
  onToggle,
  onOpen,
}: {
  node: TreeNode
  level: number
  expanded: Set<string>
  forceExpanded: boolean
  selected: string | null
  onToggle: (path: string) => void
  onOpen: (entry: WorkspaceEntry) => void
}) {
  const isOpen = node.dir && (forceExpanded || expanded.has(node.path))
  const active = selected === node.path
  const handleKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (!node.dir) return
    if (event.key === 'ArrowRight' && !isOpen) {
      event.preventDefault()
      onToggle(node.path)
    } else if (event.key === 'ArrowLeft' && isOpen) {
      event.preventDefault()
      onToggle(node.path)
    }
  }

  return (
    <div role="none">
      <button
        type="button"
        role="treeitem"
        aria-level={level}
        aria-expanded={node.dir ? isOpen : undefined}
        aria-selected={!node.dir ? active : undefined}
        onClick={() => (node.dir ? onToggle(node.path) : onOpen(node))}
        onKeyDown={handleKeyDown}
        className={`group flex min-h-[36px] w-full min-w-0 items-center gap-2 rounded-[4px] pr-2 text-left outline-none transition-colors focus-visible:ring-1 focus-visible:ring-acc/70 ${
          active ? 'bg-acc/10' : 'hover:bg-raised2/60'
        }`}
        style={{ paddingLeft: 8 + (level - 1) * 15 }}
        title={node.path}
      >
        {node.dir ? (
          <span className={`w-2.5 flex-none font-mono text-[10px] ${isOpen ? 'text-acc' : 'text-faint group-hover:text-soft'}`} aria-hidden="true">
            {isOpen ? '⌄' : '›'}
          </span>
        ) : (
          <span className="w-2.5 flex-none" />
        )}
        <FileBadge entry={node} active={active} />
        <span className={`min-w-0 flex-1 truncate font-mono text-[11px] ${
          active ? 'text-ink' : node.dir ? 'font-semibold text-mist' : 'text-soft group-hover:text-mist'
        }`}>
          {node.name}
        </span>
        {!node.dir && (
          <span className="flex-none font-mono text-[8.5px] text-faint">{fmtSize(node.size)}</span>
        )}
      </button>
      {node.dir && isOpen && node.children.length > 0 && (
        <div role="group">
          {node.children.map((child) => (
            <TreeRow
              key={child.path}
              node={child}
              level={level + 1}
              expanded={expanded}
              forceExpanded={forceExpanded}
              selected={selected}
              onToggle={onToggle}
              onOpen={onOpen}
            />
          ))}
        </div>
      )}
    </div>
  )
}

function FileViewer({
  entry,
  onClose,
  onSaved,
  onDirtyChange,
  mobile,
}: {
  entry: WorkspaceEntry
  onClose: () => void
  onSaved: () => void
  onDirtyChange: (dirty: boolean) => void
  mobile?: boolean
}) {
  const [mode, setMode] = useState<'view' | 'edit'>('view')
  const [content, setContent] = useState('')
  const [original, setOriginal] = useState('')
  const [wrap, setWrap] = useState(true)
  const [confirmCancel, setConfirmCancel] = useState(false)
  const [mediaFailed, setMediaFailed] = useState(false)
  const isImage = entry.kind === 'image'
  const isVideo = entry.kind === 'video'
  const isMedia = isImage || isVideo
  const isMarkdown = /\.md$/i.test(entry.path)
  const dirty = !isMedia && content !== original

  const fileQuery = useQuery({
    queryKey: ['workspace-file', entry.path],
    queryFn: () =>
      api.get<{ path: string; content: string }>(
        `/api/workspace/file?path=${encodeURIComponent(entry.path)}`,
      ),
    enabled: !isMedia,
    retry: false,
  })

  useEffect(() => {
    if (!fileQuery.data) return
    setContent(fileQuery.data.content)
    setOriginal(fileQuery.data.content)
    setMode('view')
  }, [fileQuery.data])

  useEffect(() => {
    onDirtyChange(dirty)
  }, [dirty, onDirtyChange])

  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => {
      if (!dirty) return
      event.preventDefault()
    }
    window.addEventListener('beforeunload', guard)
    return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  const save = useMutation({
    mutationFn: () => api.put('/api/workspace/file', { path: entry.path, content }),
    onSuccess: () => {
      setOriginal(content)
      setMode('view')
      onSaved()
    },
  })

  const requestCancel = useCallback(() => {
    if (dirty) setConfirmCancel(true)
    else setMode('view')
  }, [dirty])

  useEffect(() => {
    const shortcuts = (event: globalThis.KeyboardEvent) => {
      if (mode !== 'edit') return
      if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 's') {
        event.preventDefault()
        if (dirty && !save.isPending) save.mutate()
      } else if (event.key === 'Escape') {
        event.preventDefault()
        requestCancel()
      }
    }
    window.addEventListener('keydown', shortcuts)
    return () => window.removeEventListener('keydown', shortcuts)
  }, [dirty, mode, requestCancel, save])

  const err = fileQuery.error as ApiError | null
  const parts = entry.path.split('/')

  return (
    <div className={`flex h-full min-h-0 min-w-0 flex-col ${mobile ? 'bg-page' : ''}`}>
      <div className="flex min-h-[54px] flex-wrap items-center gap-x-3 gap-y-2 border-b border-line px-3 py-2.5 lg:px-4">
        {mobile && (
          <button
            type="button"
            onClick={onClose}
            className="flex h-9 w-9 flex-none items-center justify-center rounded-[4px] border border-line bg-raised text-[18px] text-mist hover:text-ink"
            aria-label="Back to workspace"
          >
            ‹
          </button>
        )}
        <div className="min-w-[140px] flex-1">
          <div className="flex min-w-0 items-center gap-1 font-mono text-[10px]">
            {parts.map((part, index) => (
              <span key={`${part}-${index}`} className="contents">
                {index > 0 && <span className="text-faint">/</span>}
                <span className={index === parts.length - 1 ? 'truncate text-mist' : 'text-faint'}>{part}</span>
              </span>
            ))}
            {dirty && <span className="ml-1 h-[6px] w-[6px] flex-none rounded-full bg-acc" title="Unsaved changes" />}
          </div>
          <div className="mt-0.5 text-[9.5px] text-muted">
            {isImage ? 'Image preview' : isVideo ? 'Video preview' : mode === 'edit' ? 'Editing' : 'Read only'} · {fmtSize(entry.size)}
          </div>
        </div>

        <div className="ml-auto flex flex-wrap items-center justify-end gap-1.5">
          {!isMedia && mode === 'view' && (
            <button
              type="button"
              onClick={() => setWrap((value) => !value)}
              className="min-h-8 rounded-[4px] border border-line bg-raised px-2.5 font-mono text-[9.5px] text-soft hover:border-line2 hover:text-ink"
              aria-pressed={wrap}
            >
              {wrap ? 'Wrap on' : 'Wrap off'}
            </button>
          )}
          {!isMedia && mode === 'view' && (
            <button
              type="button"
              onClick={() => fileQuery.refetch()}
              disabled={fileQuery.isFetching}
              className="min-h-8 rounded-[4px] border border-line bg-raised px-2.5 text-[10.5px] text-soft hover:border-line2 hover:text-ink disabled:opacity-40"
            >
              {fileQuery.isFetching ? 'Reloading…' : 'Reload'}
            </button>
          )}
          <a
            href={downloadUrl(entry.path)}
            download
            className="flex min-h-8 items-center rounded-[4px] border border-line bg-raised px-2.5 text-[10.5px] text-soft hover:border-line2 hover:text-ink"
          >
            Download
          </a>
          {!isMedia && mode === 'view' && !err && (
            <Button variant="primary" onClick={() => setMode('edit')}>Edit file</Button>
          )}
          {!isMedia && mode === 'edit' && (
            <>
              <Button variant="secondary" onClick={requestCancel}>Cancel</Button>
              <Button
                variant="primary"
                onClick={() => save.mutate()}
                loading={save.isPending}
                disabled={!dirty || fileQuery.isLoading || !!err}
              >
                Save
              </Button>
            </>
          )}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto bg-deep/35">
        {isMedia ? (
          <div className="flex min-h-full items-center justify-center p-5 lg:p-8">
            {!entry.previewable ? (
              <EmptyState
                title={`This ${entry.kind} is larger than the 100 MB inline preview limit`}
                action={<a className="text-[11px] text-acc hover:underline" href={downloadUrl(entry.path)}>Download the original</a>}
                className="w-full max-w-lg"
              />
            ) : mediaFailed ? (
              <EmptyState
                title={`This ${entry.kind} could not be previewed by the browser`}
                action={<a className="text-[11px] text-acc hover:underline" href={downloadUrl(entry.path)}>Download the original</a>}
                className="w-full max-w-lg"
              />
            ) : isImage ? (
              <a
                href={previewUrl(entry.path)}
                target="_blank"
                rel="noreferrer"
                className="flex max-h-full max-w-full items-center justify-center rounded-[7px] border border-line bg-[linear-gradient(45deg,rgb(var(--rb-raised))_25%,transparent_25%),linear-gradient(-45deg,rgb(var(--rb-raised))_25%,transparent_25%),linear-gradient(45deg,transparent_75%,rgb(var(--rb-raised))_75%),linear-gradient(-45deg,transparent_75%,rgb(var(--rb-raised))_75%)] bg-[length:20px_20px] bg-[position:0_0,0_10px,10px_-10px,-10px_0] p-3 shadow-[0_16px_50px_rgba(0,0,0,.22)]"
                title="Open full-size image"
              >
                <img
                  src={previewUrl(entry.path)}
                  alt={entry.path.split('/').pop() ?? 'Workspace image'}
                  onError={() => setMediaFailed(true)}
                  className="max-h-[calc(100dvh-210px)] max-w-full object-contain"
                />
              </a>
            ) : (
              <video
                src={previewUrl(entry.path)}
                controls
                preload="metadata"
                onError={() => setMediaFailed(true)}
                className="max-h-[calc(100dvh-210px)] max-w-full rounded-[7px] border border-line bg-black shadow-[0_16px_50px_rgba(0,0,0,.28)]"
              >
                Your browser does not support video playback.
              </video>
            )}
          </div>
        ) : fileQuery.isLoading ? (
          <div className="space-y-3 p-5">
            <Skeleton className="w-3/4" />
            <Skeleton className="w-1/2" />
            <Skeleton className="w-2/3" />
            <Skeleton className="w-5/6" />
          </div>
        ) : err ? (
          <div className="p-4">
            <div className="rounded-[5px] border border-err/30 bg-err/10 px-3 py-2.5 text-[11.5px] text-err">
              Cannot open this file: {err.message}
            </div>
          </div>
        ) : mode === 'edit' ? (
          <textarea
            autoFocus
            value={content}
            onChange={(event) => setContent(event.target.value)}
            spellCheck={false}
            aria-label={`Edit ${entry.path}`}
            className="h-full min-h-[420px] w-full resize-none border-0 bg-deep/20 p-4 font-mono text-[12px] leading-[1.7] text-ink outline-none focus:bg-deep/40 lg:p-5"
          />
        ) : isMarkdown ? (
          <article className={`mx-auto min-h-full max-w-4xl px-5 py-6 lg:px-10 lg:py-8 ${wrap ? '' : 'min-w-max max-w-none'}`}>
            <Markdown>{content}</Markdown>
          </article>
        ) : (
          <pre className={`min-h-full p-4 font-mono text-[11.5px] leading-[1.7] text-body lg:p-5 ${wrap ? 'whitespace-pre-wrap break-words' : 'min-w-max whitespace-pre'}`}>
            {content || <span className="text-faint">This file is empty.</span>}
          </pre>
        )}
      </div>

      <div className="flex min-h-8 flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-3 py-1.5 font-mono text-[8.5px] uppercase tracking-wider text-faint lg:px-4">
        <span>{entry.kind}</span>
        <span>{fmtSize(entry.size)}</span>
        <span>Modified {fmtDate(entry.modified)}</span>
        {mode === 'edit' && <span className="ml-auto normal-case tracking-normal text-muted">⌘/Ctrl+S to save · Esc to cancel</span>}
      </div>

      {save.isError && (
        <div className="border-t border-err/20 bg-err/10 px-4 py-2 text-[11px] text-err">
          {(save.error as ApiError)?.message ?? 'Save failed'}
        </div>
      )}

      <ConfirmDialog
        open={confirmCancel}
        title="Discard unsaved changes?"
        body="The file will return to its last saved version."
        confirmLabel="Discard"
        destructive
        onConfirm={() => {
          setContent(original)
          setMode('view')
          setConfirmCancel(false)
        }}
        onCancel={() => setConfirmCancel(false)}
      />
    </div>
  )
}

function WorkspaceWelcome({
  files,
  images,
  videos,
  folders,
}: {
  files: number
  images: number
  videos: number
  folders: number
}) {
  return (
    <div className="flex h-full min-h-[420px] items-center justify-center p-6">
      <div className="w-full max-w-xl text-center">
        <div className="mx-auto mb-5 flex h-16 w-20 items-end justify-center rounded-[7px] border border-line bg-raised p-3 shadow-[0_12px_34px_rgba(0,0,0,.18)]">
          <span className="relative block h-7 w-11">
            <span className="absolute left-0 top-0 h-2 w-5 rounded-t-[3px] bg-acc/70" />
            <span className="absolute inset-x-0 bottom-0 h-6 rounded-[3px] bg-acc" />
          </span>
        </div>
        <h2 className="text-[18px] font-semibold tracking-[-0.2px] text-ink">Your workspace, in one place</h2>
        <p className="mx-auto mt-2 max-w-md text-[12px] leading-relaxed text-soft">
          Open a file from the explorer. Text stays read only until you choose to edit it, while images and videos open in dedicated previews.
        </p>
        <div className="mx-auto mt-6 grid max-w-md grid-cols-4 divide-x divide-[var(--rb-line)] rounded-[5px] border border-line bg-raised/60 py-3">
          {[
            [files, 'files'],
            [folders, 'folders'],
            [images, 'images'],
            [videos, 'videos'],
          ].map(([value, label]) => (
            <div key={label}>
              <div className="font-mono text-[14px] font-semibold text-mist">{value}</div>
              <div className="mt-0.5 font-mono text-[8.5px] uppercase tracking-wider text-faint">{label}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

export default function FilesPage() {
  const qc = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selected, setSelected] = useState<string | null>(() => searchParams.get('path'))
  const [expanded, setExpanded] = useState<Set<string>>(loadExpanded)
  const [query, setQuery] = useState('')
  const [dirty, setDirty] = useState(false)
  const [pendingSelection, setPendingSelection] = useState<string | null | undefined>(undefined)
  const [toast, setToast] = useState<string | null>(null)

  const tree = useQuery({
    queryKey: ['workspace-tree'],
    queryFn: () => api.get<WorkspaceEntry[]>('/api/workspace/tree'),
  })

  const entries = useMemo(() => tree.data ?? [], [tree.data])
  const files = useMemo(() => entries.filter((entry) => !entry.dir), [entries])
  const folders = useMemo(() => entries.filter((entry) => entry.dir), [entries])
  const images = useMemo(() => files.filter((entry) => entry.kind === 'image'), [files])
  const videos = useMemo(() => files.filter((entry) => entry.kind === 'video'), [files])
  const entryByPath = useMemo(() => new Map(files.map((entry) => [entry.path, entry])), [files])
  const coreFiles = CORE_FILES.flatMap((core) => {
    const entry = entryByPath.get(core.path)
    return entry ? [{ ...core, entry }] : []
  })
  const nestedTree = useMemo(() => buildTree(entries), [entries])
  const visibleTree = useMemo(() => filterTree(nestedTree, query), [nestedTree, query])
  const selectedEntry = selected ? entryByPath.get(selected) : undefined

  useEffect(() => {
    localStorage.setItem(EXPANDED_STORAGE_KEY, JSON.stringify([...expanded]))
  }, [expanded])

  useEffect(() => {
    if (!selected) return
    const parents = parentPaths(selected)
    if (!parents.length) return
    setExpanded((current) => {
      const next = new Set(current)
      let changed = false
      for (const parent of parents) {
        if (!next.has(parent)) {
          next.add(parent)
          changed = true
        }
      }
      return changed ? next : current
    })
  }, [selected])

  const commitSelection = useCallback((next: string | null) => {
    setSelected(next)
    setSearchParams(next ? { path: next } : {}, { replace: true })
  }, [setSearchParams])

  const choose = useCallback((next: string | null) => {
    if (next === selected) return
    if (dirty) {
      setPendingSelection(next)
      return
    }
    commitSelection(next)
  }, [commitSelection, dirty, selected])

  const toggleFolder = useCallback((path: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }, [])

  const onSaved = () => {
    setToast('File saved')
    qc.invalidateQueries({ queryKey: ['workspace-tree'] })
    qc.invalidateQueries({ queryKey: ['workspace-file', selected] })
  }

  const editor = selectedEntry ? (
    <FileViewer
      key={selectedEntry.path}
      entry={selectedEntry}
      onClose={() => choose(null)}
      onSaved={onSaved}
      onDirtyChange={setDirty}
    />
  ) : selected && !tree.isLoading ? (
    <div className="flex h-full items-center justify-center p-6">
      <EmptyState
        title="This file is no longer available in the workspace"
        action={<Button variant="secondary" onClick={() => commitSelection(null)}>Back to explorer</Button>}
        className="w-full max-w-md"
      />
    </div>
  ) : (
    <WorkspaceWelcome
      files={files.length}
      folders={folders.length}
      images={images.length}
      videos={videos.length}
    />
  )

  return (
    <Page
      title="Files"
      actions={
        <span className="font-mono text-[9.5px] uppercase tracking-wider text-faint">
          {files.length} files · {folders.length} folders
        </span>
      }
    >
      <div className="flex h-full min-h-0 min-w-0">
        <aside className="flex h-full min-h-0 w-full flex-col border-line bg-panel lg:w-[370px] lg:min-w-[370px] lg:border-r" aria-label="Workspace explorer">
          <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-4 pt-3">
            {coreFiles.length > 0 && (
              <section className="mb-5" aria-labelledby="core-files-label">
                <div className="mb-2 flex items-center gap-2 px-1">
                  <span id="core-files-label"><SectionLabel>Core files</SectionLabel></span>
                  <span className="ml-auto font-mono text-[8.5px] text-faint">{coreFiles.length}</span>
                </div>
                <div className="grid grid-cols-2 gap-1.5">
                  {coreFiles.map((core) => (
                    <CoreFileCard
                      key={core.path}
                      entry={core.entry}
                      label={core.label}
                      description={core.description}
                      active={selected === core.path}
                      onOpen={() => choose(core.path)}
                    />
                  ))}
                </div>
              </section>
            )}

            <section aria-labelledby="workspace-tree-label">
              <div className="mb-2 flex items-center gap-2 px-1">
                <span id="workspace-tree-label"><SectionLabel>Explorer</SectionLabel></span>
                <button
                  type="button"
                  onClick={() => setExpanded(new Set())}
                  className="ml-auto font-mono text-[8.5px] text-faint hover:text-soft"
                >
                  collapse all
                </button>
                <button
                  type="button"
                  onClick={() => tree.refetch()}
                  disabled={tree.isFetching}
                  className="font-mono text-[8.5px] text-faint hover:text-soft disabled:opacity-40"
                >
                  {tree.isFetching ? 'refreshing…' : 'refresh'}
                </button>
              </div>

              <label className="mb-2 flex min-h-9 items-center gap-2 rounded-[5px] border border-line bg-deep px-2.5 focus-within:border-acc/40">
                <span className="font-mono text-[12px] text-faint" aria-hidden="true">⌕</span>
                <span className="sr-only">Filter workspace files</span>
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Filter files and folders"
                  className="min-w-0 flex-1 border-0 bg-transparent font-mono text-[10.5px] text-mist outline-none placeholder:text-faint"
                />
                {query && (
                  <button type="button" onClick={() => setQuery('')} className="text-[14px] text-faint hover:text-soft" aria-label="Clear filter">×</button>
                )}
              </label>

              {tree.isLoading ? (
                <div className="space-y-3 p-2">
                  <Skeleton className="w-2/3" />
                  <Skeleton className="ml-4 w-1/2" />
                  <Skeleton className="ml-4 w-3/4" />
                  <Skeleton className="w-1/2" />
                </div>
              ) : tree.isError ? (
                <EmptyState
                  title={(tree.error as ApiError)?.message ?? 'Could not load the workspace'}
                  action={<Button variant="secondary" onClick={() => tree.refetch()}>Try again</Button>}
                />
              ) : entries.length === 0 ? (
                <EmptyState title="Workspace is empty" />
              ) : visibleTree.length === 0 ? (
                <EmptyState title={`No files match “${query}”`} />
              ) : (
                <div role="tree" aria-label="Workspace files" className="space-y-px">
                  {visibleTree.map((node) => (
                    <TreeRow
                      key={node.path}
                      node={node}
                      level={1}
                      expanded={expanded}
                      forceExpanded={!!query.trim()}
                      selected={selected}
                      onToggle={toggleFolder}
                      onOpen={(entry) => choose(entry.path)}
                    />
                  ))}
                </div>
              )}
            </section>
          </div>

          <div className="flex min-h-9 items-center gap-3 border-t border-line px-4 font-mono text-[8.5px] uppercase tracking-wider text-faint">
            <span>{files.length} files</span>
            {images.length > 0 && <span>{images.length} images</span>}
            {videos.length > 0 && <span>{videos.length} videos</span>}
            <span className="ml-auto">read safely</span>
          </div>
        </aside>

        <main className="hidden min-w-0 flex-1 bg-page p-3 lg:block">
          <div className="h-full min-h-0 overflow-hidden rounded-[6px] border border-line bg-raised shadow-[0_10px_35px_rgba(0,0,0,.12)]">
            {editor}
          </div>
        </main>
      </div>

      {selected && selectedEntry && (
        <div className="fixed inset-0 z-40 flex flex-col bg-page pt-safe lg:hidden">
          <FileViewer
            key={selectedEntry.path}
            entry={selectedEntry}
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
        confirmLabel="Discard and continue"
        destructive
        onConfirm={() => {
          setDirty(false)
          commitSelection(pendingSelection ?? null)
          setPendingSelection(undefined)
        }}
        onCancel={() => setPendingSelection(undefined)}
      />

      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </Page>
  )
}
