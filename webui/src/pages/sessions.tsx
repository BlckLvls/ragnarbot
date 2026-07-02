// Sessions: cross-channel list with a read-only transcript viewer.

import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { ChatMessage, SessionInfo, api } from '../lib/api'
import { Page } from '../app/shell'
import { Markdown } from '../components/markdown'
import { Button, ConfirmDialog, Dot, EmptyState, Skeleton } from '../components/ui'

const CHANNELS = ['all', 'web', 'telegram', 'cli'] as const
type ChannelFilter = (typeof CHANNELS)[number]

const CHANNEL_META: Record<string, { label: string; dot: string }> = {
  web: { label: 'web', dot: 'bg-acc' },
  telegram: { label: 'telegram', dot: 'bg-[#6FB7C4]' },
  cli: { label: 'cli', dot: 'bg-muted' },
}

function ChannelBadge({ channel }: { channel: string }) {
  const meta = CHANNEL_META[channel] ?? { label: channel, dot: 'bg-muted' }
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`h-[5px] w-[5px] ${meta.dot}`} />
      <span className="text-[12px] text-mist">{meta.label}</span>
    </span>
  )
}

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const today = new Date()
  if (d.toDateString() === today.toDateString())
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
}

// ── page ─────────────────────────────────────────────────────

export default function SessionsPage() {
  const [channel, setChannel] = useState<ChannelFilter>('all')
  const [selected, setSelected] = useState<SessionInfo | null>(null)

  const { data: sessions, isLoading } = useQuery({
    queryKey: ['sessions', 'all'],
    queryFn: () => api.get<SessionInfo[]>('/api/sessions'),
  })

  const filtered = (sessions ?? []).filter((s) => channel === 'all' || s.channel === channel)

  const chips = (
    <div className="flex gap-1.5">
      {CHANNELS.map((c) => (
        <button
          key={c}
          onClick={() => setChannel(c)}
          className={`rounded-[3px] px-2.5 py-1 text-[11px] capitalize ${
            channel === c ? 'bg-acc font-semibold text-onacc' : 'bg-raised2 text-soft hover:text-ink'
          }`}
        >
          {c}
        </button>
      ))}
    </div>
  )

  const list = (
    <div className="flex h-full flex-col">
      <div className="flex gap-1.5 border-b border-line px-4 py-2.5 lg:hidden">{chips}</div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {isLoading ? (
          <div className="space-y-3 p-4">
            <Skeleton className="w-2/3" />
            <Skeleton className="w-1/2" />
            <Skeleton className="w-3/4" />
          </div>
        ) : filtered.length === 0 ? (
          <div className="p-4">
            <EmptyState title="No sessions in this channel" />
          </div>
        ) : (
          filtered.map((s) => {
            const isSel = selected?.session_id === s.session_id
            return (
              <button
                key={s.session_id}
                onClick={() => setSelected(s)}
                className={`flex w-full items-center gap-3 border-b border-line px-4 py-3 text-left ${
                  isSel ? 'bg-raised2' : 'hover:bg-raised/60'
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-[12.5px] font-medium text-mist">{s.title}</span>
                    {s.active && <Dot color="acc" />}
                  </div>
                  <div className="mt-1 flex items-center gap-2">
                    <ChannelBadge channel={s.channel} />
                    <span className="font-mono text-[9.5px] text-faint">{fmtDate(s.updated_at)}</span>
                  </div>
                </div>
                <span className="font-mono text-[9.5px] text-faint">
                  {s.session_id.slice(0, 8)}
                </span>
              </button>
            )
          })
        )}
      </div>
    </div>
  )

  return (
    <Page title="Sessions" actions={<div className="hidden lg:block">{chips}</div>}>
      <div className="flex h-full min-h-0">
        {/* list column */}
        <div className="w-full lg:w-[320px] lg:min-w-[320px] lg:border-r lg:border-line">{list}</div>

        {/* desktop transcript */}
        <div className="hidden min-h-0 flex-1 lg:flex">
          {selected ? (
            <Transcript
              key={selected.session_id}
              session={selected}
              onClose={() => setSelected(null)}
              onDeleted={() => setSelected(null)}
            />
          ) : (
            <div className="flex flex-1 items-center justify-center">
              <EmptyState title="Select a session to view its transcript" />
            </div>
          )}
        </div>
      </div>

      {/* mobile full-screen transcript */}
      {selected && (
        <div className="fixed inset-0 z-40 bg-page lg:hidden">
          <Transcript
            key={selected.session_id}
            session={selected}
            onClose={() => setSelected(null)}
            onDeleted={() => setSelected(null)}
            mobile
          />
        </div>
      )}
    </Page>
  )
}

// ── transcript viewer ────────────────────────────────────────

function Transcript({
  session,
  onClose,
  onDeleted,
  mobile,
}: {
  session: SessionInfo
  onClose: () => void
  onDeleted: () => void
  mobile?: boolean
}) {
  const qc = useQueryClient()
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [confirmActivate, setConfirmActivate] = useState(false)
  const [busy, setBusy] = useState(false)

  const { data, isLoading } = useQuery({
    queryKey: ['session-messages', session.session_id],
    queryFn: () =>
      api.get<{ messages: ChatMessage[]; title: string; total: number }>(
        `/api/sessions/${session.session_id}/messages?limit=200`,
      ),
  })

  const isWeb = session.channel === 'web'

  const activate = async () => {
    setConfirmActivate(false)
    setBusy(true)
    try {
      await api.post(`/api/sessions/${session.session_id}/activate`)
      qc.invalidateQueries({ queryKey: ['sessions'] })
    } finally {
      setBusy(false)
    }
  }

  const del = async () => {
    setConfirmDelete(false)
    setBusy(true)
    try {
      await api.delete(`/api/sessions/${session.session_id}`)
      qc.invalidateQueries({ queryKey: ['sessions'] })
      onDeleted()
    } finally {
      setBusy(false)
    }
  }

  const exportJson = () => {
    const blob = new Blob([JSON.stringify(data?.messages ?? [], null, 2)], {
      type: 'application/json',
    })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${session.session_id}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col bg-panel">
      <div className="flex items-center gap-2 border-b border-line px-4 py-3 pt-safe lg:pt-3">
        {mobile && (
          <button onClick={onClose} className="text-[13px] text-soft">
            <span className="text-[15px]">‹</span> Sessions
          </button>
        )}
        <span className="truncate text-[13px] font-semibold text-ink">
          {data?.title ?? session.title}
        </span>
        <span className="font-mono text-[9.5px] text-faint">read-only</span>
        {!mobile && (
          <button onClick={onClose} className="ml-auto text-[15px] text-muted hover:text-ink">
            ×
          </button>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4 lg:px-5">
        {isLoading ? (
          <div className="space-y-3">
            <Skeleton className="w-1/2" />
            <Skeleton className="w-2/3" />
          </div>
        ) : (data?.messages ?? []).length === 0 ? (
          <EmptyState title="No messages in this session" />
        ) : (
          <div className="mx-auto flex max-w-[640px] flex-col gap-3">
            {(data?.messages ?? []).map((m, i) => (
              <TranscriptMessage key={m.index ?? i} msg={m} />
            ))}
          </div>
        )}
      </div>

      <div className="flex gap-2 border-t border-line px-4 py-3 pb-safe">
        {isWeb && (
          <Button
            variant="primary"
            className="flex-1"
            disabled={session.active || busy}
            onClick={() => setConfirmActivate(true)}
          >
            {session.active ? 'Active' : 'Make active'}
          </Button>
        )}
        <Button variant="secondary" className="flex-1" onClick={exportJson}>
          Export JSON
        </Button>
        <Button variant="destructive" onClick={() => setConfirmDelete(true)} loading={busy}>
          ×
        </Button>
      </div>

      <ConfirmDialog
        open={confirmActivate}
        title="Make this the active chat?"
        body="The web console will switch to this conversation."
        confirmLabel="Activate"
        onConfirm={activate}
        onCancel={() => setConfirmActivate(false)}
      />
      <ConfirmDialog
        open={confirmDelete}
        title="Delete this session?"
        body="The conversation history will be permanently removed."
        confirmLabel="Delete"
        destructive
        onConfirm={del}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  )
}

function TranscriptMessage({ msg }: { msg: ChatMessage }) {
  const meta = (msg.metadata ?? {}) as Record<string, unknown>
  if (meta.type === 'compaction') {
    return (
      <div className="my-2 flex items-center gap-3">
        <div className="h-px flex-1 bg-line2" />
        <span className="font-mono text-[9.5px] uppercase tracking-wider text-faint">
          context compacted
        </span>
        <div className="h-px flex-1 bg-line2" />
      </div>
    )
  }
  if (msg.role === 'user') {
    return (
      <div className="self-end max-w-[88%] whitespace-pre-wrap rounded-[5px] rounded-br-[2px] border border-acc/[.22] bg-acc/[.13] px-3 py-2 text-[12px] leading-[1.5] text-ink">
        {msg.content}
      </div>
    )
  }
  return (
    <div className="max-w-[92%] self-start text-[12px] leading-[1.6] text-mist">
      <Markdown>{msg.content}</Markdown>
    </div>
  )
}
