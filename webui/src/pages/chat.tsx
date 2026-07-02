// Chat: streaming conversation with voice input, attachments, and conversation switching.

import { memo, useCallback, useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ChatMessage, MediaItem, SessionInfo, UploadResult, mediaUrl } from '../lib/api'
import { LiveTurn, ToolEvent, useChat } from '../lib/ws'
import { fmtBytes, fmtTokens } from '../lib/format'
import { Markdown } from '../components/markdown'
import { Caret, ContextMeter, PixelWordmark, StreamDots, Waveform } from '../components/pixel'
import {
  Button,
  ConfirmDialog,
  Dot,
  SectionLabel,
  Segmented,
  Sheet,
  TextInput,
  Toggle,
} from '../components/ui'

const REASONING = ['off', 'low', 'medium', 'high', 'ultra', 'max'] as const
const CTX_MODES = ['eco', 'normal', 'full'] as const

// ── conversation list ────────────────────────────────────────

function ConversationList({ onPick }: { onPick?: () => void }) {
  const qc = useQueryClient()
  const sessionId = useChat((s) => s.sessionId)
  // Unified conversation list: real user chats from every channel (web + telegram),
  // heartbeat/cli plumbing and empty sessions are filtered out server-side.
  const { data: sessions } = useQuery({
    queryKey: ['sessions', 'user'],
    queryFn: () => api.get<SessionInfo[]>('/api/sessions?user=1'),
  })
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [renameVal, setRenameVal] = useState('')

  const invalidate = () => qc.invalidateQueries({ queryKey: ['sessions', 'user'] })

  const newChat = useMutation({
    mutationFn: () => api.post<{ session_id: string }>('/api/sessions/new'),
    onSuccess: () => {
      invalidate()
      onPick?.()
    },
  })
  const activate = useMutation({
    mutationFn: (id: string) => api.post(`/api/sessions/${id}/activate`),
    onSuccess: () => {
      invalidate()
      onPick?.()
    },
  })
  const del = useMutation({
    mutationFn: (id: string) => api.delete(`/api/sessions/${id}`),
    onSuccess: invalidate,
  })
  const rename = useMutation({
    mutationFn: ({ id, title }: { id: string; title: string }) =>
      api.patch(`/api/sessions/${id}`, { title }),
    onSuccess: () => {
      setRenaming(null)
      invalidate()
    },
  })

  return (
    <div className="flex h-full flex-col">
      <div className="p-3">
        <Button variant="primary" className="w-full" onClick={() => newChat.mutate()} loading={newChat.isPending}>
          + New chat
        </Button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {(sessions ?? []).map((s) => {
          const active = s.session_id === sessionId || s.active
          return (
            <div
              key={s.session_id}
              className={`group flex cursor-pointer items-center gap-2 border-b border-line px-3 py-2.5 ${
                active ? 'bg-raised2' : 'hover:bg-raised/60'
              }`}
              onClick={() => !active && activate.mutate(s.session_id)}
            >
              <div className="min-w-0 flex-1">
                {renaming === s.session_id ? (
                  <TextInput
                    autoFocus
                    value={renameVal}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) => setRenameVal(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') rename.mutate({ id: s.session_id, title: renameVal })
                      if (e.key === 'Escape') setRenaming(null)
                    }}
                    className="py-1 text-[12px]"
                  />
                ) : (
                  <>
                    <div className={`truncate text-[12.5px] font-medium ${active ? 'text-ink' : 'text-mist'}`}>
                      {s.title}
                    </div>
                    <div className="flex items-center gap-1.5 font-mono text-[9.5px] text-faint">
                      {s.channel !== 'web' && (
                        <span className="rounded-[2px] bg-raised2 px-[5px] py-[1px] text-[8.5px] uppercase text-soft">
                          {s.channel === 'telegram' ? 'tg' : s.channel}
                        </span>
                      )}
                      {s.updated_at ? new Date(s.updated_at).toLocaleDateString() : ''}
                    </div>
                  </>
                )}
              </div>
              <div className="hidden gap-1 group-hover:flex" onClick={(e) => e.stopPropagation()}>
                <button
                  className="font-mono text-[9.5px] text-muted hover:text-ink"
                  onClick={() => {
                    setRenaming(s.session_id)
                    setRenameVal(s.title)
                  }}
                >
                  ren
                </button>
                <button
                  className="font-mono text-[9.5px] text-muted hover:text-err"
                  onClick={() => setConfirmDelete(s.session_id)}
                >
                  del
                </button>
              </div>
              {active && <Dot color="acc" />}
            </div>
          )
        })}
      </div>
      <ConfirmDialog
        open={!!confirmDelete}
        title="Delete this chat?"
        body="The conversation history will be permanently removed."
        confirmLabel="Delete"
        destructive
        onConfirm={() => {
          if (confirmDelete) del.mutate(confirmDelete)
          setConfirmDelete(null)
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  )
}

// ── activity strip (tool timeline) ───────────────────────────

function ActivityStrip({ tools, live }: { tools: ToolEvent[]; live?: boolean }) {
  const [open, setOpen] = useState(live ?? false)
  if (!tools.length) return null
  const running = tools.filter((t) => !t.done).length
  return (
    <div className="my-2 rounded-[4px] border border-line bg-panel">
      <button
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
      >
        <Dot color={running ? 'acc' : 'ok'} pulse={!!running} />
        <span className="font-mono text-[10px] text-soft">
          {running ? `running ${tools[tools.length - 1].tool}…` : `${tools.length} tool call${tools.length > 1 ? 's' : ''}`}
        </span>
        <span className="ml-auto font-mono text-[9px] text-faint">{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="border-t border-line px-3 py-1.5">
          {tools.map((t, i) => (
            <div key={i} className="flex items-center gap-2 py-[3px]">
              <Dot color={t.done ? (t.status === 'error' ? 'err' : 'ok') : 'acc'} pulse={!t.done} />
              <span className="font-mono text-[10.5px] text-mist">{t.tool}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted">
                {t.args_preview}
              </span>
              {t.duration_ms != null && (
                <span className="font-mono text-[9.5px] text-faint">
                  {(t.duration_ms / 1000).toFixed(1)}s
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

// ── message rendering ────────────────────────────────────────

function UsageLine({ usage }: { usage: NonNullable<ChatMessage['usage']> }) {
  return (
    <div className="mt-1 font-mono text-[9.5px] text-faint">
      {fmtTokens(usage.input_tokens)} → {fmtTokens(usage.output_tokens)} tok
      {usage.cache_read_tokens > 0 && ` · cache ${fmtTokens(usage.cache_read_tokens)}`}
      {' · '}
      {(usage.duration_ms / 1000).toFixed(0)}s
    </div>
  )
}

// Media rendering, telegram-style: photos inline (compressed view, click for
// original), video/audio get players, everything else — a file card.
function MediaItemView({ item }: { item: MediaItem }) {
  const url = mediaUrl(item.path)
  if (item.kind === 'photo') {
    return (
      <a href={url} target="_blank" rel="noreferrer" className="block w-fit">
        <img src={url} alt={item.filename} className="mt-2 max-h-80 max-w-full rounded-[4px] cursor-zoom-in" />
      </a>
    )
  }
  if (item.kind === 'video') {
    return <video controls preload="metadata" src={url} className="mt-2 max-h-80 max-w-full rounded-[4px]" />
  }
  if (item.kind === 'audio') {
    return (
      <div className="mt-2 w-fit max-w-full rounded-[4px] border border-line bg-raised px-3 py-2">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="font-mono text-[10px] text-mist">{item.filename}</span>
          <span className="font-mono text-[9px] text-faint">{fmtBytes(item.size)}</span>
        </div>
        <audio controls preload="metadata" src={url} className="h-9 max-w-full" />
      </div>
    )
  }
  const ext = (item.filename.split('.').pop() ?? 'f').slice(0, 4)
  return (
    <a
      href={url}
      download={item.filename}
      className="mt-2 flex w-fit max-w-full items-center gap-2.5 rounded-[4px] border border-line bg-raised px-3 py-2.5 hover:bg-raised2"
    >
      <span className="flex h-[30px] w-[30px] items-center justify-center rounded-[3px] bg-raised2 font-mono text-[8.5px] uppercase text-soft">
        {ext}
      </span>
      <span className="min-w-0">
        <span className="block truncate text-[12px] text-ink">{item.filename}</span>
        <span className="font-mono text-[9.5px] text-faint">
          {fmtBytes(item.size)}{item.size != null ? ' · ' : ''}download
        </span>
      </span>
    </a>
  )
}

const MessageRow = memo(function MessageRow({ msg }: { msg: ChatMessage }) {
  const meta = (msg.metadata ?? {}) as Record<string, any>
  if (meta.type === 'compaction') {
    return (
      <div className="my-4 flex items-center gap-3">
        <div className="h-px flex-1 bg-[var(--rb-line2)]" />
        <span className="font-mono text-[9.5px] uppercase tracking-wider text-faint">context compacted</span>
        <div className="h-px flex-1 bg-[var(--rb-line2)]" />
      </div>
    )
  }
  const isSystemNote =
    typeof msg.content === 'string' &&
    (msg.content.startsWith('[Cron result') ||
      msg.content.startsWith('[Hook triggered') ||
      msg.content.startsWith('[Heartbeat check'))
  if (isSystemNote) {
    return (
      <div className="my-2 rounded-[4px] border border-line bg-inset px-3 py-2 font-mono text-[10.5px] text-muted">
        {msg.content}
      </div>
    )
  }
  if (msg.role === 'user') {
    return (
      <div className="my-2 flex flex-col items-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-[6px] rounded-br-[2px] border border-acc/[.22] bg-acc/[.13] px-3.5 py-2.5 text-[13px] text-ink">
          {msg.content}
          {msg.attachments && msg.attachments.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {msg.attachments.map((a, i) => (
                <span key={i} className="rounded-[2px] bg-raised2 px-1.5 py-0.5 font-mono text-[9.5px] text-soft">
                  {a.type === 'photo' ? '▦ ' : '≡ '}
                  {a.filename || a.type}
                </span>
              ))}
            </div>
          )}
        </div>
        {meta.timestamp && (
          <span className="mt-1 font-mono text-[9.5px] text-faint">
            {new Date(meta.timestamp as string).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          </span>
        )}
      </div>
    )
  }
  const tools = (meta.tools as ToolEvent[]) ?? []
  return (
    <div className="my-3">
      {tools.length > 0 && <ActivityStrip tools={tools} />}
      <Markdown>{msg.content}</Markdown>
      {(msg.media_items ?? []).map((m, i) => (
        <MediaItemView key={i} item={m} />
      ))}
      {(msg.media ?? []).map((m, i) => (
        <img key={i} src={mediaUrl(m)} className="mt-2 max-h-96 rounded-[4px]" />
      ))}
      {(msg.media_refs ?? []).map((m, i) => (
        <img key={i} src={mediaUrl(m.path)} className="mt-2 max-h-96 rounded-[4px]" />
      ))}
      {meta.stopped && <div className="mt-1 font-mono text-[9.5px] text-warn">stopped by user</div>}
      {msg.usage && <UsageLine usage={msg.usage} />}
    </div>
  )
})

function LiveTurnView({ turn }: { turn: LiveTurn }) {
  return (
    <div className="my-3">
      {turn.tools.length > 0 && <ActivityStrip tools={turn.tools} live />}
      {turn.text ? (
        <>
          <Markdown>{turn.text}</Markdown>
          <Caret />
        </>
      ) : (
        <div className="flex items-center gap-2 py-1">
          <StreamDots />
          <span className="font-mono text-[10px] text-muted">thinking…</span>
        </div>
      )}
    </div>
  )
}

// ── voice recording ──────────────────────────────────────────

function useVoiceRecorder(onText: (text: string) => void, onError: (e: string) => void) {
  const [recording, setRecording] = useState(false)
  const [busy, setBusy] = useState(false)
  const [elapsed, setElapsed] = useState(0)
  const [analyser, setAnalyser] = useState<AnalyserNode | null>(null)
  const recRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const ctxRef = useRef<AudioContext | null>(null)

  const start = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new AudioContext()
      const src = ctx.createMediaStreamSource(stream)
      const an = ctx.createAnalyser()
      an.fftSize = 128
      src.connect(an)
      ctxRef.current = ctx
      setAnalyser(an)
      const rec = new MediaRecorder(stream, { mimeType: 'audio/webm;codecs=opus' })
      chunksRef.current = []
      rec.ondataavailable = (e) => e.data.size && chunksRef.current.push(e.data)
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop())
        ctx.close()
        setAnalyser(null)
        setBusy(true)
        try {
          const blob = new Blob(chunksRef.current, { type: 'audio/webm' })
          const text = await api.transcribe(blob)
          onText(text)
        } catch (e: any) {
          onError(e?.message || 'transcription failed')
        } finally {
          setBusy(false)
        }
      }
      rec.start()
      recRef.current = rec
      setElapsed(0)
      setRecording(true)
    } catch {
      onError('microphone unavailable')
    }
  }

  const stop = () => {
    recRef.current?.stop()
    setRecording(false)
  }

  useEffect(() => {
    if (!recording) return
    const iv = setInterval(() => setElapsed((e) => e + 1), 1000)
    return () => clearInterval(iv)
  }, [recording])

  return { recording, busy, elapsed, analyser, start, stop }
}

// ── chat settings sheet ──────────────────────────────────────

function ChatSettingsSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const s = useChat()
  const [advanced, setAdvanced] = useState(false)
  const cmd = (name: string, args?: Record<string, unknown>, optimistic?: Partial<Record<string, unknown>>) => {
    s.command(name, args)
    if (optimistic) useChat.setState(optimistic as any)
  }
  return (
    <Sheet open={open} onClose={onClose} title="Chat settings">
      <div className="space-y-4">
        <div>
          <SectionLabel className="mb-1.5">Reasoning</SectionLabel>
          <Segmented
            options={REASONING}
            value={s.reasoningLevel as (typeof REASONING)[number]}
            onChange={(v) => cmd('set_reasoning_level', { reasoning_level: v }, { reasoningLevel: v })}
          />
        </div>
        <div>
          <SectionLabel className="mb-1.5">Context mode</SectionLabel>
          <Segmented
            options={CTX_MODES}
            value={s.contextMode as (typeof CTX_MODES)[number]}
            onChange={(v) => cmd('set_context_mode', { context_mode: v }, { contextMode: v })}
          />
        </div>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[12.5px] text-ink">Lightning mode</div>
            <div className="text-[10.5px] text-muted">OpenAI only · 2× token price</div>
          </div>
          <Toggle
            value={s.lightning}
            onChange={(v) => cmd('set_lightning_mode', { lightning_mode: v }, { lightning: v })}
          />
        </div>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[12.5px] text-ink">Steering</div>
            <div className="text-[10.5px] text-muted">inject messages into a running turn</div>
          </div>
          <Toggle
            value={s.steering}
            onChange={(v) => cmd('set_steering_mode', { steering_mode: v }, { steering: v })}
          />
        </div>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[12.5px] text-ink">Trace mode</div>
            <div className="text-[10.5px] text-muted">tool calls as chat messages (telegram-style)</div>
          </div>
          <Toggle value={s.trace} onChange={(v) => cmd('set_trace_mode', { trace_mode: v }, { trace: v })} />
        </div>
        <button className="font-mono text-[10px] text-muted hover:text-ink" onClick={() => setAdvanced(!advanced)}>
          {advanced ? '▾' : '▸'} advanced
        </button>
        {advanced && (
          <div className="flex items-center justify-between">
            <div className="text-[12.5px] text-ink">Experimental soul</div>
            <SoulToggle />
          </div>
        )}
      </div>
    </Sheet>
  )
}

function SoulToggle() {
  const command = useChat((s) => s.command)
  const [on, setOn] = useState(false)
  return (
    <Toggle
      value={on}
      onChange={(v) => {
        setOn(v)
        command('set_soul_mode', { soul_mode: v })
      }}
    />
  )
}

// ── composer ─────────────────────────────────────────────────

function Composer() {
  const s = useChat()
  const [text, setText] = useState('')
  const [attachments, setAttachments] = useState<UploadResult[]>([])
  const [uploading, setUploading] = useState(false)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const fileRef = useRef<HTMLInputElement>(null)
  const setToast = useChat((st) => st.setToast)
  const voice = useVoiceRecorder(
    (t) => {
      const autoSend = localStorage.getItem('rb-voice-autosend') === '1'
      if (autoSend && t.trim()) {
        s.send(t.trim())
      } else {
        setText((prev) => (prev ? `${prev} ${t}` : t))
        taRef.current?.focus()
      }
    },
    (e) => setToast(e),
  )

  const doUpload = useCallback(
    async (files: File[]) => {
      if (!files.length) return
      setUploading(true)
      try {
        const res = await api.upload(files)
        setAttachments((prev) => [...prev, ...res])
      } catch (e: any) {
        setToast(e?.message || 'upload failed')
      } finally {
        setUploading(false)
      }
    },
    [setToast],
  )

  // uploads coming from the page-level drag-and-drop overlay
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<UploadResult[]>).detail
      if (detail?.length) setAttachments((prev) => [...prev, ...detail])
    }
    window.addEventListener('rb-uploads', handler)
    return () => window.removeEventListener('rb-uploads', handler)
  }, [])

  const submit = () => {
    const t = text.trim()
    if (!t && attachments.length === 0) return
    s.send(t, attachments.map((a) => a.id))
    setText('')
    setAttachments([])
  }

  const onPaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files)
    if (files.length) {
      e.preventDefault()
      doUpload(files)
    }
  }

  return (
    <div className="border-t border-line bg-panel px-3 py-2.5 pb-safe">
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {attachments.map((a) => (
            <span key={a.id} className="flex items-center gap-1.5 rounded-[3px] border border-line bg-raised px-2 py-1">
              <span className="flex h-[18px] w-[18px] items-center justify-center rounded-[2px] bg-raised2 font-mono text-[7px] uppercase text-soft">
                {a.kind === 'photo' ? 'img' : (a.filename.split('.').pop() ?? 'f').slice(0, 3)}
              </span>
              <span className="max-w-40 truncate font-mono text-[10px] text-mist">{a.filename}</span>
              <button
                className="text-muted hover:text-err"
                onClick={() => setAttachments((prev) => prev.filter((x) => x.id !== a.id))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {voice.recording ? (
        <div className="flex items-center gap-3 py-1">
          <Dot color="err" pulse />
          <span className="font-mono text-[11px] text-ink">
            {Math.floor(voice.elapsed / 60)}:{String(voice.elapsed % 60).padStart(2, '0')}
          </span>
          <div className="flex-1">
            <Waveform analyser={voice.analyser} w={220} h={26} />
          </div>
          <Button variant="primary" onClick={voice.stop}>
            Done
          </Button>
        </div>
      ) : (
        <div className="flex items-end gap-2">
          <input
            ref={fileRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              doUpload(Array.from(e.target.files ?? []))
              e.target.value = ''
            }}
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
            className="pb-2 text-[18px] leading-none text-muted hover:text-ink disabled:opacity-40"
            title="Attach files"
          >
            +
          </button>
          <button
            onClick={voice.start}
            disabled={voice.busy}
            className="pb-2 font-mono text-[13px] leading-none text-muted hover:text-acc disabled:opacity-40"
            title="Voice input"
          >
            {voice.busy ? '…' : '●'}
          </button>
          <textarea
            ref={taRef}
            rows={1}
            value={text}
            placeholder={s.processing ? 'Steer the agent mid-run…' : 'Message ragnarbot…'}
            onChange={(e) => {
              setText(e.target.value)
              const ta = e.target
              ta.style.height = 'auto'
              ta.style.height = `${Math.min(160, ta.scrollHeight)}px`
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault()
                submit()
              }
            }}
            onPaste={onPaste}
            className="max-h-40 min-h-[38px] flex-1 resize-none rounded-[3px] border border-line2 bg-raised px-3 py-2 text-[13px] text-ink outline-none placeholder:text-muted focus:border-acc/50"
          />
          {s.processing ? (
            <Button variant="destructive" onClick={s.stop} title="Stop the agent">
              ■
            </Button>
          ) : (
            <Button variant="primary" onClick={submit} disabled={!text.trim() && !attachments.length}>
              ➤
            </Button>
          )}
        </div>
      )}
    </div>
  )
}

// ── main page ────────────────────────────────────────────────

export default function ChatPage() {
  const s = useChat()
  const [showConvs, setShowConvs] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const qc = useQueryClient()
  const setToast = useChat((st) => st.setToast)

  const { data: history } = useQuery({
    queryKey: ['messages', s.sessionId],
    queryFn: () => api.get<{ messages: ChatMessage[]; title: string }>('/api/sessions/active/messages?limit=200'),
    enabled: s.sessionId !== null,
    // Live WS events own the message list after the initial load — a focus
    // refetch would wipe tool timelines/usage and race in-flight messages.
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })

  const appliedSessionRef = useRef<string | null>(null)
  useEffect(() => {
    if (history && s.sessionId && appliedSessionRef.current !== s.sessionId) {
      appliedSessionRef.current = s.sessionId
      useChat.setState({ messages: history.messages, sessionTitle: history.title })
    }
  }, [history, s.sessionId])

  useEffect(() => {
    qc.invalidateQueries({ queryKey: ['sessions', 'user'] })
  }, [s.sessionId, qc])

  // autoscroll on new content
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 300
    if (nearBottom) el.scrollTop = el.scrollHeight
  }, [s.messages, s.liveTurn?.text, s.liveTurn?.tools.length])

  const onDrop = async (e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const files = Array.from(e.dataTransfer.files)
    if (!files.length) return
    try {
      const res = await api.upload(files)
      // stash uploads into the composer via a custom event
      window.dispatchEvent(new CustomEvent('rb-uploads', { detail: res }))
    } catch (err: any) {
      setToast(err?.message || 'upload failed')
    }
  }

  const percent = s.contextMax ? (s.contextUsed / s.contextMax) * 100 : 0
  const modelShort = s.model.split('/').pop() ?? s.model
  const empty = s.messages.length === 0 && !s.liveTurn

  return (
    <div
      className="relative flex h-full min-h-0 flex-1"
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={(e) => {
        if (e.currentTarget === e.target) setDragOver(false)
      }}
      onDrop={onDrop}
    >
      {/* conversation list — desktop column */}
      <div className="hidden w-[256px] min-w-[256px] border-r border-line bg-panel lg:block">
        <div className="border-b border-line px-3 py-3">
          <SectionLabel>Conversations</SectionLabel>
        </div>
        <ConversationList />
      </div>

      {/* chat column */}
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-line px-3 py-2 lg:px-5">
          <button className="text-[13px] text-muted hover:text-ink lg:hidden" onClick={() => setShowConvs(true)}>
            ‹ Chats
          </button>
          <span className="hidden truncate text-[13px] font-semibold text-ink lg:block">
            {s.sessionTitle || 'New chat'}
          </span>
          <button
            onClick={() => setShowSettings(true)}
            className="rounded-[2px] bg-raised2 px-2 py-1 font-mono text-[9.5px] text-mist hover:text-ink"
            title="Chat settings"
          >
            {modelShort} · {s.reasoningLevel}
            {s.lightning ? ' · ⚡' : ''}
          </button>
          <ContextMeter percent={percent} className="ml-auto" />
        </div>

        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 lg:px-8">
          {empty ? (
            <div className="flex h-full flex-col items-center justify-center gap-5">
              <PixelWordmark w={300} h={64} cell={4} gap={1.5} />
              <div className="text-[12px] text-muted">Your personal AI assistant. Nothing else.</div>
              <div className="flex flex-wrap justify-center gap-2 px-6">
                {['What can you do?', 'Schedule a daily digest', 'Search my memory'].map((p) => (
                  <button
                    key={p}
                    onClick={() => s.send(p)}
                    className="rounded-[3px] border border-line bg-raised px-3 py-1.5 text-[11.5px] text-soft hover:text-ink"
                  >
                    {p}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto max-w-[720px] py-4">
              {s.messages.map((m, i) => (
                <MessageRow key={i} msg={m} />
              ))}
              {s.liveTurn && <LiveTurnView turn={s.liveTurn} />}
              {s.processing && !s.liveTurn && (
                <div className="flex items-center gap-2 py-2">
                  <StreamDots />
                </div>
              )}
            </div>
          )}
        </div>

        <Composer />
      </div>

      {/* drag overlay */}
      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-30 flex items-center justify-center border-2 border-dashed border-[var(--rb-line2)] bg-page/80">
          <div className="rounded-[4px] border border-acc/40 bg-raised px-6 py-4 text-[13px] text-ink">
            Drop files to attach
          </div>
        </div>
      )}

      {/* mobile conversations sheet */}
      {showConvs && (
        <div className="fixed inset-0 z-40 bg-page lg:hidden">
          <div className="flex items-center gap-2 border-b border-line px-4 py-3 pt-safe">
            <button className="text-[13px] text-muted" onClick={() => setShowConvs(false)}>
              ‹ Back
            </button>
            <span className="text-[14px] font-semibold text-ink">Chats</span>
          </div>
          <div className="h-[calc(100%-52px)]">
            <ConversationList onPick={() => setShowConvs(false)} />
          </div>
        </div>
      )}

      <ChatSettingsSheet open={showSettings} onClose={() => setShowSettings(false)} />
    </div>
  )
}
