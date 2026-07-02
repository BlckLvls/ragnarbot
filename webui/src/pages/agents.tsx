// Agents: sub-agent definitions (raw AGENT.md editor) + live runs and background jobs.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, AgentDefSummary, AgentTask } from '../lib/api'
import { relPast } from '../lib/format'
import { Page } from '../app/shell'
import {
  Button,
  EmptyState,
  FieldError,
  SectionLabel,
  Segmented,
  Sheet,
  Skeleton,
  SourceBadge,
  StatusPill,
  TextInput,
} from '../components/ui'
import { StreamDots } from '../components/pixel'

// ── types ────────────────────────────────────────────────────

interface AgentDef {
  name: string
  description: string
  model: string
  allowed_tools: string | string[]
  allowed_skills: string | string[]
  body: string
  path?: string
  reasoning_level: string
}

interface TaskProgress {
  status?: string
  elapsed?: string
  message_count?: number
  result?: string | null
  error?: string | null
  tool_counts?: Record<string, number>
  messages?: { role?: string; content?: string }[]
  [key: string]: unknown
}

// ── AGENT.md reconstruction ──────────────────────────────────

function listOrString(v: string | string[]): string {
  return Array.isArray(v) ? `[${v.join(', ')}]` : v
}

function toContent(d: AgentDef): string {
  return [
    '---',
    `name: ${d.name}`,
    `description: ${d.description}`,
    `model: ${d.model}`,
    `allowedTools: ${listOrString(d.allowed_tools)}`,
    `allowedSkills: ${listOrString(d.allowed_skills)}`,
    `reasoningLevel: ${d.reasoning_level}`,
    '---',
    '',
    d.body,
    '',
  ].join('\n')
}

function templateContent(name: string): string {
  return [
    '---',
    `name: ${name}`,
    'description: ',
    'model: default',
    'allowedTools: all',
    'reasoningLevel: inherit',
    '---',
    '',
    `# ${name}`,
    '',
    'Describe how this agent should behave.',
    '',
  ].join('\n')
}

// ── definition editor ────────────────────────────────────────

function DefEditor({
  name,
  isNew,
  onClose,
}: {
  name: string | null
  isNew: boolean
  onClose: () => void
}) {
  const qc = useQueryClient()
  const [newName, setNewName] = useState('')
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const { data: def, isLoading } = useQuery({
    queryKey: ['agent-def', name],
    queryFn: () => api.get<AgentDef>(`/api/agents/defs/${name}`),
    enabled: !isNew && !!name,
  })

  // seed the textarea once detail arrives (or immediately for a new agent)
  const seeded = isNew ? true : !!def
  const value = dirty ? content : isNew ? templateContent(newName || 'my-agent') : def ? toContent(def) : ''

  const save = useMutation({
    mutationFn: () => {
      const target = isNew ? newName.trim() : name
      if (!target) throw new Error('agent name is required')
      return api.put(`/api/agents/defs/${target}`, { content: value })
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['agent-defs'] })
      onClose()
    },
    onError: (e: Error) => setErr(e.message),
  })

  return (
    <Sheet open onClose={onClose} side title={isNew ? 'New agent' : `Edit · ${name}`}>
      <div className="space-y-4">
        {isNew && (
          <div>
            <SectionLabel className="mb-1.5">Name</SectionLabel>
            <TextInput
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              placeholder="deep-researcher"
              className="font-mono"
            />
          </div>
        )}

        <div>
          <div className="mb-1.5 flex items-center gap-2">
            <SectionLabel>AGENT.md</SectionLabel>
            <span className="ml-auto font-mono text-[9.5px] text-faint">markdown + frontmatter</span>
          </div>
          {!seeded || isLoading ? (
            <Skeleton className="h-64" />
          ) : (
            <textarea
              value={value}
              onChange={(e) => {
                setContent(e.target.value)
                setDirty(true)
              }}
              spellCheck={false}
              className="h-[52vh] w-full resize-none rounded-[3px] border border-line2 bg-deep px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-mist outline-none focus:border-acc/50"
            />
          )}
        </div>

        <div className="text-[10.5px] text-muted">
          Only workspace agents are writable — saving a builtin creates a workspace override.
        </div>

        {err && <FieldError>{err}</FieldError>}

        <div className="flex justify-end gap-2 border-t border-line pt-3">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={save.isPending} onClick={() => (setErr(null), save.mutate())}>
            Save agent
          </Button>
        </div>
      </div>
    </Sheet>
  )
}

// ── definitions tab ──────────────────────────────────────────

function DefinitionsTab() {
  const { data, isLoading } = useQuery({
    queryKey: ['agent-defs'],
    queryFn: () => api.get<AgentDefSummary[]>('/api/agents/defs'),
  })
  const [editing, setEditing] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  const defs = data ?? []
  return (
    <div className="p-4 lg:p-6">
      <div className="mb-3 flex items-center gap-3">
        <span className="font-mono text-[10px] text-muted">
          {defs.length} definition{defs.length === 1 ? '' : 's'}
        </span>
        <Button variant="primary" className="ml-auto" onClick={() => setCreating(true)}>
          + New agent
        </Button>
      </div>

      {isLoading ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Skeleton className="h-20" />
          <Skeleton className="h-20" />
        </div>
      ) : defs.length === 0 ? (
        <EmptyState title="No agent definitions" />
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {defs.map((d) => (
            <button
              key={d.name}
              onClick={() => setEditing(d.name)}
              className="flex flex-col gap-2 rounded-[4px] border border-line bg-raised p-3.5 text-left hover:border-line2"
            >
              <div className="flex items-center gap-2">
                <span className="truncate text-[13.5px] font-semibold text-ink">{d.name}</span>
                <span className="ml-auto">
                  <SourceBadge source={d.source} />
                </span>
              </div>
              <span className="line-clamp-2 text-[11.5px] leading-relaxed text-soft">{d.description}</span>
            </button>
          ))}
        </div>
      )}

      {(editing || creating) && (
        <DefEditor
          name={editing}
          isNew={creating}
          onClose={() => {
            setEditing(null)
            setCreating(false)
          }}
        />
      )}
    </div>
  )
}

// ── run task card ────────────────────────────────────────────

function ProgressView({ taskId }: { taskId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['agent-task', taskId],
    queryFn: () => api.get<TaskProgress>(`/api/agents/tasks/${taskId}`),
    refetchInterval: 5000,
  })
  if (isLoading) return <Skeleton className="w-1/2" />
  if (!data) return null
  const tools = data.tool_counts ? Object.entries(data.tool_counts) : []
  const msgs = (data.messages ?? []).slice(-6)
  return (
    <div className="mt-1 space-y-2 rounded-[3px] border border-line bg-deep p-3">
      <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-soft">
        {data.elapsed && <span>elapsed {data.elapsed}</span>}
        {data.message_count != null && <span>{data.message_count} msgs</span>}
        {tools.map(([t, n]) => (
          <span key={t}>
            {t}×{n}
          </span>
        ))}
      </div>
      {data.error && (
        <div className="rounded-[3px] border border-err/25 bg-err/[.06] px-2.5 py-2 font-mono text-[10.5px] text-err/90">
          {data.error}
        </div>
      )}
      {data.result && (
        <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10.5px] text-mist">{data.result}</pre>
      )}
      {msgs.length > 0 && (
        <div className="space-y-1.5 border-t border-line pt-2">
          {msgs.map((m, i) => (
            <div key={i} className="text-[11px]">
              <span className="font-mono text-[9px] uppercase text-faint">{m.role ?? '?'}</span>
              <div className="whitespace-pre-wrap text-body">{(m.content ?? '').slice(0, 400)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function TaskCard({ task }: { task: AgentTask }) {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(false)
  const [msg, setMsg] = useState('')
  const invalidate = () => qc.invalidateQueries({ queryKey: ['agent-tasks'] })
  const running = task.status === 'running'

  const stop = useMutation({ mutationFn: () => api.post(`/api/agents/tasks/${task.id}/stop`), onSuccess: invalidate })
  const dismiss = useMutation({
    mutationFn: () => api.post(`/api/agents/tasks/${task.id}/dismiss`),
    onSuccess: invalidate,
  })
  const send = useMutation({
    mutationFn: () => api.post(`/api/agents/tasks/${task.id}/message`, { content: msg.trim() }),
    onSuccess: () => {
      setMsg('')
      qc.invalidateQueries({ queryKey: ['agent-task', task.id] })
    },
  })

  return (
    <div
      className={`rounded-[4px] border bg-raised p-3.5 ${
        task.status === 'error' ? 'border-err/25' : 'border-line'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        {running ? <StreamDots /> : <span className="h-[5px] w-[5px] bg-ok" />}
        <span className="text-[13px] font-semibold text-ink">{task.agent}</span>
        <StatusPill status={task.status} />
        <span className="ml-auto font-mono text-[10px] text-faint">
          {task.message_count} msg · {relPast(task.created_at)}
        </span>
      </div>
      {task.label && <div className="mt-1.5 text-[11.5px] text-soft">{task.label}</div>}

      <div className="mt-2.5 flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => setExpanded(!expanded)}>
          {expanded ? 'Hide' : 'View'}
        </Button>
        {running && (
          <Button variant="destructive" onClick={() => stop.mutate()} loading={stop.isPending}>
            ■ Stop
          </Button>
        )}
        {!running && (
          <Button variant="secondary" onClick={() => dismiss.mutate()} loading={dismiss.isPending}>
            Dismiss
          </Button>
        )}
      </div>

      {running && (
        <div className="mt-2 flex gap-2">
          <TextInput
            value={msg}
            onChange={(e) => setMsg(e.target.value)}
            placeholder="Send a message to this run…"
            onKeyDown={(e) => {
              if (e.key === 'Enter' && msg.trim()) send.mutate()
            }}
          />
          <Button variant="secondary" disabled={!msg.trim()} loading={send.isPending} onClick={() => send.mutate()}>
            Send
          </Button>
        </div>
      )}

      {expanded && <ProgressView taskId={task.id} />}
    </div>
  )
}

// ── runs tab ─────────────────────────────────────────────────

function RunsTab() {
  const qc = useQueryClient()
  const { data: tasks, isLoading } = useQuery({
    queryKey: ['agent-tasks'],
    queryFn: () => api.get<AgentTask[]>('/api/agents/tasks'),
    refetchInterval: 5000,
  })
  const { data: jobs } = useQuery({
    queryKey: ['jobs'],
    queryFn: () => api.get<{ summary: string }>('/api/jobs'),
    refetchInterval: 5000,
  })

  const list = tasks ?? []
  return (
    <div className="space-y-6 p-4 lg:p-6">
      <div>
        <SectionLabel className="mb-2">Sub-agents</SectionLabel>
        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-20" />
            <Skeleton className="h-20" />
          </div>
        ) : list.length === 0 ? (
          <EmptyState title="No active runs" />
        ) : (
          <div className="space-y-2">
            {list.map((t) => (
              <TaskCard key={t.id} task={t} />
            ))}
          </div>
        )}
      </div>

      <div>
        <div className="mb-2 flex items-center gap-2">
          <SectionLabel>Background jobs</SectionLabel>
          <button
            onClick={() => qc.invalidateQueries({ queryKey: ['jobs'] })}
            className="ml-auto font-mono text-[10px] text-acc hover:opacity-80"
          >
            refresh
          </button>
        </div>
        <pre className="overflow-x-auto whitespace-pre-wrap rounded-[4px] border border-line bg-deep p-3 font-mono text-[10.5px] leading-relaxed text-mist">
          {jobs?.summary?.trim() || 'No background jobs.'}
        </pre>
      </div>
    </div>
  )
}

// ── page ─────────────────────────────────────────────────────

export default function AgentsPage() {
  const [tab, setTab] = useState<'defs' | 'runs'>('defs')
  const { data: tasks } = useQuery({
    queryKey: ['agent-tasks'],
    queryFn: () => api.get<AgentTask[]>('/api/agents/tasks'),
    refetchInterval: 5000,
  })
  const active = (tasks ?? []).filter((t) => t.status === 'running').length

  return (
    <Page
      title="Agents"
      actions={
        <Segmented
          options={['defs', 'runs'] as const}
          value={tab}
          onChange={setTab}
          labels={{ defs: 'Definitions', runs: active ? `Runs · ${active}` : 'Runs' }}
        />
      }
    >
      <div className="border-b border-line px-4 py-2.5 lg:hidden">
        <Segmented
          options={['defs', 'runs'] as const}
          value={tab}
          onChange={setTab}
          labels={{ defs: 'Definitions', runs: active ? `Runs · ${active}` : 'Runs' }}
        />
      </div>
      {tab === 'defs' ? <DefinitionsTab /> : <RunsTab />}
    </Page>
  )
}
