// Agents: live run monitoring, in-memory run history, and read-only definitions.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, AgentDefinitionInfo, AgentTask } from '../lib/api'
import { relPast } from '../lib/format'
import { Page } from '../app/shell'
import { Markdown } from '../components/markdown'
import {
  Button,
  EmptyState,
  SectionLabel,
  Segmented,
  Skeleton,
  SourceBadge,
  StatusPill,
  TextInput,
} from '../components/ui'
import { StreamDots } from '../components/pixel'

type AgentsTab = 'current' | 'history' | 'agents'

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
  const statusDot = task.status === 'error' ? 'bg-err' : task.status === 'stopped' ? 'bg-muted' : 'bg-ok'

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
        {running ? <StreamDots /> : <span className={`h-[5px] w-[5px] ${statusDot}`} />}
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

function displaySectionName(value: string): string {
  return value
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

function formatInstructions(value: string): string {
  return value
    .replace(/^<([a-zA-Z0-9_-]+)>\s*$/gm, (_, section: string) => `\n## ${displaySectionName(section)}\n`)
    .replace(/^<\/[a-zA-Z0-9_-]+>\s*$/gm, '')
    .trim()
}

function ConfigItems({ value }: { value: string | string[] }) {
  const items = Array.isArray(value) ? value : [value]
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <span key={item} className="rounded-[2px] bg-raised2 px-2 py-1 font-mono text-[9.5px] text-mist">
          {item}
        </span>
      ))}
    </div>
  )
}

function AgentDefinitionCard({ agent }: { agent: AgentDefinitionInfo }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="min-w-0 overflow-hidden rounded-[4px] border border-line bg-raised">
      <button
        type="button"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
        className="flex w-full min-w-0 items-start gap-3 p-3.5 text-left hover:bg-raised2/45"
      >
        <span className="mt-1 h-[6px] w-[6px] flex-none bg-acc" />
        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-[13px] font-semibold text-ink">{agent.name}</span>
            <SourceBadge source={agent.source} />
            <span className="rounded-[2px] bg-deep px-1.5 py-0.5 font-mono text-[8.5px] uppercase text-faint">
              read only
            </span>
          </span>
          <span className="mt-1.5 block text-[11.5px] leading-relaxed text-soft">{agent.description}</span>
          <span className="mt-2 block truncate font-mono text-[9px] text-faint">{agent.path}</span>
        </span>
        <span className="mt-0.5 flex-none font-mono text-[10px] text-muted">
          {expanded ? 'Hide ▴' : 'Open ▾'}
        </span>
      </button>

      {expanded && (
        <div className="border-t border-line">
          <div className="grid gap-4 p-4 sm:grid-cols-2 xl:grid-cols-4">
            <div>
              <SectionLabel className="mb-1.5">Model</SectionLabel>
              <div className="font-mono text-[10.5px] text-mist">
                {agent.config.model === 'default' ? 'default · inherited' : agent.config.model}
              </div>
            </div>
            <div>
              <SectionLabel className="mb-1.5">Reasoning</SectionLabel>
              <div className="font-mono text-[10.5px] text-mist">{agent.config.reasoning_level}</div>
            </div>
            <div>
              <SectionLabel className="mb-1.5">Allowed tools</SectionLabel>
              <ConfigItems value={agent.config.allowed_tools} />
            </div>
            <div>
              <SectionLabel className="mb-1.5">Allowed skills</SectionLabel>
              <ConfigItems value={agent.config.allowed_skills} />
            </div>
          </div>
          <div className="border-t border-line p-4">
            <div className="mb-3 flex items-center gap-2">
              <SectionLabel>Definition</SectionLabel>
              <span className="ml-auto font-mono text-[9px] text-faint">parsed for display · read only</span>
            </div>
            <div className="agent-definition max-h-[68vh] overflow-auto rounded-[4px] border border-line bg-deep px-4 py-3">
              <Markdown>{formatInstructions(agent.instructions)}</Markdown>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default function AgentsPage() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<AgentsTab>('current')
  const { data, isLoading } = useQuery({
    queryKey: ['agent-tasks'],
    queryFn: () => api.get<AgentTask[]>('/api/agents/tasks'),
    refetchInterval: 5000,
  })
  const definitions = useQuery({
    queryKey: ['agent-definitions'],
    queryFn: () => api.get<AgentDefinitionInfo[]>('/api/agents/definitions'),
    refetchInterval: 30_000,
  })
  const tasks = data ?? []
  const current = tasks.filter((task) => task.status === 'running').reverse()
  const history = tasks.filter((task) => task.status !== 'running').reverse()
  const agents = definitions.data ?? []
  const tabLabels = {
    current: current.length ? `Current Runs · ${current.length}` : 'Current Runs',
    history: history.length ? `Run History · ${history.length}` : 'Run History',
    agents: agents.length ? `Agents · ${agents.length}` : 'Agents',
  }
  const tabs = ['current', 'history', 'agents'] as const

  return (
    <Page
      title="Agents"
      actions={
        <Segmented
          options={tabs}
          value={tab}
          onChange={setTab}
          labels={tabLabels}
        />
      }
    >
      <div className="border-b border-line px-4 py-2.5 lg:hidden">
        <Segmented
          options={tabs}
          value={tab}
          onChange={setTab}
          labels={tabLabels}
        />
      </div>
      <div className="p-4 lg:p-6">
        {tab === 'current' && (isLoading ? (
          <div className="space-y-2"><Skeleton className="h-24" /><Skeleton className="h-24" /></div>
        ) : current.length === 0 ? (
          <EmptyState title="No sub-agents are running right now" />
        ) : (
          <div className="space-y-2">{current.map((task) => <TaskCard key={task.id} task={task} />)}</div>
        ))}

        {tab === 'history' && (
          <div>
            <div className="mb-4 flex items-start gap-2.5 rounded-[4px] border border-warn/20 bg-warn/[.05] px-3 py-2.5">
              <span className="mt-1 h-[5px] w-[5px] flex-none bg-warn" />
              <div>
                <div className="text-[11.5px] font-medium text-mist">Temporary run history</div>
                <div className="mt-0.5 text-[10.5px] leading-relaxed text-soft">
                  Runs are kept in gateway memory and are cleared when the gateway restarts. Dismiss removes an individual run.
                </div>
              </div>
            </div>
            {isLoading ? (
              <div className="space-y-2"><Skeleton className="h-24" /><Skeleton className="h-24" /></div>
            ) : history.length === 0 ? (
              <EmptyState title="No completed, stopped, or failed runs in this gateway session" />
            ) : (
              <div className="space-y-2">{history.map((task) => <TaskCard key={task.id} task={task} />)}</div>
            )}
          </div>
        )}

        {tab === 'agents' && (
          <div>
            <div className="mb-4 flex flex-wrap items-center gap-3">
              <span className="font-mono text-[10px] text-muted">
                {agents.length} available agent{agents.length === 1 ? '' : 's'} · definitions are read only
              </span>
              <Button variant="secondary" className="ml-auto" onClick={() => navigate('/')}>
                Ask ragnarbot to create one
              </Button>
            </div>
            {definitions.isLoading ? (
              <div className="space-y-2"><Skeleton className="h-24" /><Skeleton className="h-24" /></div>
            ) : definitions.isError ? (
              <EmptyState title="Agent definitions are unavailable" />
            ) : agents.length === 0 ? (
              <EmptyState
                title="No agent definitions available"
                action={<Button variant="primary" onClick={() => navigate('/')}>Ask ragnarbot to create one</Button>}
              />
            ) : (
              <div className="space-y-2">{agents.map((agent) => <AgentDefinitionCard key={agent.name} agent={agent} />)}</div>
            )}
          </div>
        )}
      </div>
    </Page>
  )
}
