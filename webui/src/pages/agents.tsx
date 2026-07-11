// Agents: sub-agent run monitoring and emergency controls. Definitions are not exposed here.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, AgentTask } from '../lib/api'
import { relPast } from '../lib/format'
import { Page } from '../app/shell'
import { Button, EmptyState, Segmented, Skeleton, StatusPill, TextInput } from '../components/ui'
import { StreamDots } from '../components/pixel'

interface TaskProgress {
  status?: string
  elapsed?: string
  message_count?: number
  result?: string | null
  error?: string | null
  tool_counts?: Record<string, number>
  messages?: { role?: string; content?: string }[]
}

function ProgressView({ taskId }: { taskId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ['agent-task', taskId],
    queryFn: () => api.get<TaskProgress>(`/api/agents/tasks/${taskId}`),
    refetchInterval: 5000,
  })
  if (isLoading) return <Skeleton className="w-1/2" />
  if (!data) return null
  const tools = data.tool_counts ? Object.entries(data.tool_counts) : []
  const messages = (data.messages ?? []).slice(-6)
  return (
    <div className="mt-3 space-y-2 rounded-[3px] border border-line bg-deep p-3">
      <div className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px] text-soft">
        {data.elapsed && <span>elapsed {data.elapsed}</span>}
        {data.message_count != null && <span>{data.message_count} messages</span>}
        {tools.map(([tool, count]) => <span key={tool}>{tool}×{count}</span>)}
      </div>
      {data.error && (
        <div className="rounded-[3px] border border-err/25 bg-err/[.06] px-2.5 py-2 font-mono text-[10.5px] text-err/90">
          {data.error}
        </div>
      )}
      {data.result && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-[3px] bg-inset p-2.5 font-mono text-[10.5px] text-mist">
          {data.result}
        </pre>
      )}
      {messages.length > 0 && (
        <div className="space-y-1.5 border-t border-line pt-2">
          {messages.map((message, index) => (
            <div key={index} className="text-[11px]">
              <span className="font-mono text-[9px] uppercase text-faint">{message.role ?? '?'}</span>
              <div className="whitespace-pre-wrap text-body">{(message.content ?? '').slice(0, 500)}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function TaskCard({ task }: { task: AgentTask }) {
  const qc = useQueryClient()
  const [expanded, setExpanded] = useState(task.status === 'running')
  const [message, setMessage] = useState('')
  const invalidate = () => qc.invalidateQueries({ queryKey: ['agent-tasks'] })
  const running = task.status === 'running'

  const stop = useMutation({ mutationFn: () => api.post(`/api/agents/tasks/${task.id}/stop`), onSuccess: invalidate })
  const dismiss = useMutation({ mutationFn: () => api.post(`/api/agents/tasks/${task.id}/dismiss`), onSuccess: invalidate })
  const send = useMutation({
    mutationFn: () => api.post(`/api/agents/tasks/${task.id}/message`, { content: message.trim() }),
    onSuccess: () => {
      setMessage('')
      qc.invalidateQueries({ queryKey: ['agent-task', task.id] })
    },
  })

  return (
    <div className={`rounded-[4px] border bg-raised p-3.5 ${task.status === 'error' ? 'border-err/25' : 'border-line'}`}>
      <div className="flex flex-wrap items-center gap-2">
        {running ? <StreamDots /> : <span className="h-[5px] w-[5px] bg-ok" />}
        <span className="text-[13px] font-semibold text-ink">{task.agent}</span>
        <StatusPill status={task.status} />
        <span className="ml-auto font-mono text-[10px] text-faint">
          {task.message_count} msg · {relPast(task.created_at)}
        </span>
      </div>
      {task.label && <div className="mt-1.5 text-[11.5px] text-soft">{task.label}</div>}

      {running && (
        <div className="mt-3 flex gap-2">
          <TextInput
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            placeholder="Send a follow-up to this run…"
            onKeyDown={(event) => {
              if (event.key === 'Enter' && message.trim()) send.mutate()
            }}
          />
          <Button variant="secondary" disabled={!message.trim()} loading={send.isPending} onClick={() => send.mutate()}>
            Follow up
          </Button>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => setExpanded(!expanded)}>{expanded ? 'Hide details' : 'View details'}</Button>
        {running ? (
          <Button variant="destructive" onClick={() => stop.mutate()} loading={stop.isPending}>■ Stop</Button>
        ) : (
          <Button variant="secondary" onClick={() => dismiss.mutate()} loading={dismiss.isPending}>Dismiss</Button>
        )}
      </div>
      {expanded && <ProgressView taskId={task.id} />}
    </div>
  )
}

export default function AgentsPage() {
  const [filter, setFilter] = useState<'active' | 'all'>('active')
  const { data, isLoading } = useQuery({
    queryKey: ['agent-tasks'],
    queryFn: () => api.get<AgentTask[]>('/api/agents/tasks'),
    refetchInterval: 5000,
  })
  const tasks = data ?? []
  const active = tasks.filter((task) => task.status === 'running')
  const visible = filter === 'active' ? active : tasks

  return (
    <Page
      title="Agents"
      actions={
        <Segmented
          options={['active', 'all'] as const}
          value={filter}
          onChange={setFilter}
          labels={{ active: active.length ? `Active · ${active.length}` : 'Active', all: `All · ${tasks.length}` }}
        />
      }
    >
      <div className="border-b border-line px-4 py-2.5 lg:hidden">
        <Segmented
          options={['active', 'all'] as const}
          value={filter}
          onChange={setFilter}
          labels={{ active: active.length ? `Active · ${active.length}` : 'Active', all: `All · ${tasks.length}` }}
        />
      </div>
      <div className="p-4 lg:p-6">
        {isLoading ? (
          <div className="space-y-2"><Skeleton className="h-24" /><Skeleton className="h-24" /></div>
        ) : visible.length === 0 ? (
          <EmptyState title={filter === 'active' ? 'No active sub-agents' : 'No sub-agent runs'} />
        ) : (
          <div className="space-y-2">{visible.map((task) => <TaskCard key={task.id} task={task} />)}</div>
        )}
      </div>
    </Page>
  )
}
