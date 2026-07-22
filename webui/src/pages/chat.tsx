// Chat: streaming conversation with voice input, attachments, and conversation switching.

import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ChatMessage, MediaItem, SessionInfo, UploadResult, mediaUrl } from '../lib/api'
import { LiveTurn, MediaEvent, TextSegment, ToolEvent, TurnSegment, useChat } from '../lib/ws'
import { copyText } from '../lib/clipboard'
import { fmtBytes, fmtTokens } from '../lib/format'
import { Markdown } from '../components/markdown'
import {
  CHAT_ACTION_PX,
  ContextMeter,
  PixelIcon,
  PixelWordmark,
  StreamDots,
  Waveform,
} from '../components/pixel'
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
const CONTEXT_THRESHOLDS: Record<(typeof CTX_MODES)[number], number> = {
  eco: 0.4,
  normal: 0.6,
  full: 0.85,
}

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
      <div className="p-4">
        <Button
          variant="primary"
          className="min-h-11 w-full text-[13px]"
          onClick={() => newChat.mutate()}
          loading={newChat.isPending}
        >
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

function ConversationDrawer({ open, onClose }: { open: boolean; onClose: () => void }) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex bg-black/55" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Chats"
        className="h-full w-[86vw] max-w-[320px] animate-rb-slide-in-left border-r border-line bg-panel shadow-[12px_0_36px_rgba(0,0,0,.35)] motion-reduce:animate-none"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex h-12 items-center border-b border-line px-4">
          <span className="text-[14px] font-semibold text-ink">Chats</span>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto flex h-8 w-8 items-center justify-center rounded-[4px] bg-raised2 text-[17px] leading-none text-muted hover:text-ink"
            aria-label="Close chats"
          >
            ×
          </button>
        </div>
        <div className="h-[calc(100%-48px)]">
          <ConversationList onPick={onClose} />
        </div>
      </div>
    </div>
  )
}

// ── activity strip (tool timeline) ───────────────────────────

function ActivityStrip({ tools, live }: { tools: ToolEvent[]; live?: boolean }) {
  const [open, setOpen] = useState(false)
  if (!tools.length) return null
  const running = tools.filter((t) => !t.done).length
  const last = tools[tools.length - 1]
  const lastColor = !last.done ? 'acc' : last.status === 'error' ? 'err' : 'ok'
  return (
    <div
      data-turn-segment="tools"
      className="overflow-hidden rounded-[6px] border border-line bg-panel shadow-[0_4px_14px_rgba(0,0,0,.12)]"
    >
      <button
        onClick={() => setOpen(!open)}
        className="flex min-h-11 w-full items-center gap-2.5 px-3 py-2 text-left transition-colors hover:bg-raised/50"
        aria-expanded={open}
      >
        <span className="flex h-7 w-7 flex-none items-center justify-center rounded-[4px] border border-line2 bg-raised">
          <Dot color={lastColor} pulse={!last.done} />
        </span>
        <span className="min-w-0 flex-1">
          <span className="block font-mono text-[8.5px] uppercase tracking-[0.1em] text-faint">
            {running ? 'Current action' : 'Last action'}
          </span>
          <span className="mt-0.5 flex min-w-0 items-center gap-2">
            <span className="flex-none text-[11.5px] font-semibold text-mist">{last.tool}</span>
            {last.args_preview && (
              <span className="min-w-0 flex-1 truncate font-mono text-[9.5px] text-muted">
                {last.args_preview}
              </span>
            )}
          </span>
        </span>
        <span className="flex flex-none items-center gap-2">
          <span className="rounded-[3px] bg-raised2 px-1.5 py-0.5 font-mono text-[8.5px] text-soft">
            {tools.length}
          </span>
          <span className="font-mono text-[10px] text-faint">{open ? '−' : '+'}</span>
        </span>
      </button>
      {open && (
        <div className="border-t border-line bg-deep/40 px-3 py-2">
          {tools.map((t, i) => (
            <div key={`${t.turn_id ?? 'tool'}-${i}`} className="relative flex gap-2.5 py-1.5">
              {i < tools.length - 1 && (
                <span className="absolute left-[6px] top-[18px] h-[calc(100%-4px)] w-px bg-[var(--rb-line2)]" />
              )}
              <span className="relative mt-1 flex h-[13px] w-[13px] flex-none items-center justify-center rounded-full border border-line2 bg-panel">
                <Dot color={t.done ? (t.status === 'error' ? 'err' : 'ok') : 'acc'} pulse={!t.done && !!live} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="text-[11px] font-semibold text-mist">{t.tool}</span>
                  {t.duration_ms != null && (
                    <span className="font-mono text-[8.5px] text-faint">
                      {(t.duration_ms / 1000).toFixed(1)}s
                    </span>
                  )}
                </span>
                {t.args_preview && (
                  <span className="mt-0.5 block break-words font-mono text-[9.5px] leading-relaxed text-muted">
                    {t.args_preview}
                  </span>
                )}
              </span>
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

function useSmoothText(
  target: string,
  smooth: boolean,
  complete: boolean,
  onSettled?: () => void,
) {
  const [visible, setVisible] = useState(smooth ? '' : target)
  const [settled, setSettled] = useState(!smooth)
  const cursorRef = useRef(smooth ? 0 : Array.from(target).length)
  const visibleLengthRef = useRef(cursorRef.current)
  const velocityRef = useRef(28)
  const previousTargetRef = useRef(cursorRef.current)
  const latestChunkRef = useRef(1)
  const settledRef = useRef(false)
  const onSettledRef = useRef(onSettled)

  useEffect(() => {
    onSettledRef.current = onSettled
  }, [onSettled])

  useEffect(() => {
    const chars = Array.from(target)
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (!smooth || reducedMotion) {
      cursorRef.current = chars.length
      visibleLengthRef.current = chars.length
      previousTargetRef.current = chars.length
      setVisible(target)
      setSettled(true)
      if (complete && !settledRef.current) {
        settledRef.current = true
        onSettledRef.current?.()
      }
      return
    }

    if (chars.length < cursorRef.current) {
      cursorRef.current = 0
      visibleLengthRef.current = 0
      velocityRef.current = 28
      setVisible('')
    }
    const growth = Math.max(0, chars.length - previousTargetRef.current)
    if (growth > 0) latestChunkRef.current = growth
    previousTargetRef.current = chars.length
    settledRef.current = false
    setSettled(false)

    let frame = 0
    let previousFrame = performance.now()
    const tick = (now: number) => {
      const elapsed = Math.min(50, Math.max(1, now - previousFrame))
      previousFrame = now

      // Keep the newest provider chunk in reserve until another chunk or the
      // final boundary arrives. This maintains a real lead instead of repeatedly
      // catching the stream and restarting from rest.
      const reserve = complete
        ? 0
        : Math.min(18, Math.max(1, latestChunkRef.current), chars.length)
      const revealLimit = Math.max(0, chars.length - reserve)
      const queued = Math.max(0, chars.length - cursorRef.current)
      // A single low-pass controller: it accelerates once as pressure builds,
      // then decelerates once as the queue drains. No reserve/release sawtooth.
      const pressure = 1 - Math.exp(-queued / 72)
      // Large provider bursts must not leave the UI typing for tens of seconds
      // after the answer is already complete. Backlog adds continuous catch-up
      // pressure while the low-pass below keeps acceleration visually smooth.
      const catchUpVelocity = Math.min(520, queued * 0.22)
      const desiredVelocity = 28 + 108 * pressure + catchUpVelocity
      const easing = 1 - Math.exp(-elapsed / 260)
      velocityRef.current += (desiredVelocity - velocityRef.current) * easing
      cursorRef.current = Math.min(
        revealLimit,
        cursorRef.current + (velocityRef.current * elapsed) / 1000,
      )

      const nextLength = Math.floor(cursorRef.current)
      if (nextLength > visibleLengthRef.current) {
        visibleLengthRef.current = nextLength
        setVisible(chars.slice(0, nextLength).join(''))
      }

      if (complete && cursorRef.current >= chars.length) {
        if (visibleLengthRef.current !== chars.length) {
          visibleLengthRef.current = chars.length
          setVisible(target)
        }
        if (!settledRef.current) {
          settledRef.current = true
          setSettled(true)
          onSettledRef.current?.()
        }
        return
      }
      if (cursorRef.current < revealLimit) {
        frame = requestAnimationFrame(tick)
      }
    }

    frame = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(frame)
  }, [target, smooth, complete])

  return { visible, settled }
}

// Media rendering, telegram-style: photos inline (compressed view, click for
// original), video/audio get players, everything else — a file card.
function MediaItemView({ item, flush = false }: { item: MediaItem; flush?: boolean }) {
  const url = mediaUrl(item.path)
  const spacing = flush ? '' : 'mt-2'
  if (item.kind === 'photo') {
    return (
      <a href={url} target="_blank" rel="noreferrer" className="block w-fit">
        <img src={url} alt={item.filename} className={`${spacing} max-h-80 max-w-full rounded-[4px] cursor-zoom-in`} />
      </a>
    )
  }
  if (item.kind === 'video') {
    return <video controls preload="metadata" src={url} className={`${spacing} max-h-80 max-w-full rounded-[4px]`} />
  }
  if (item.kind === 'audio') {
    return (
      <div className={`${spacing} w-fit max-w-full rounded-[4px] border border-line bg-raised px-3 py-2`}>
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
      className={`${spacing} flex w-fit max-w-full items-center gap-2.5 rounded-[4px] border border-line bg-raised px-3 py-2.5 hover:bg-raised2`}
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

function MediaEventView({ event, live = false }: { event: MediaEvent; live?: boolean }) {
  return (
    <div
      data-turn-segment="media"
      className={`w-fit max-w-full rounded-[6px] border border-line bg-inset p-2.5 shadow-[0_3px_12px_rgba(0,0,0,.1)] ${
        live ? 'animate-rb-message-in motion-reduce:animate-none' : ''
      }`}
    >
      <div className="space-y-2">
        {event.media_items.map((item, i) => (
          <MediaItemView key={`${item.path}-${i}`} item={item} flush />
        ))}
      </div>
      {event.content && (
        <div className="mt-2 text-soft">
          <Markdown>{event.content}</Markdown>
        </div>
      )}
    </div>
  )
}

function AssistantTextCard({
  segment,
  live = false,
  onSettled,
}: {
  segment: TextSegment
  live?: boolean
  onSettled?: () => void
}) {
  const { visible, settled } = useSmoothText(
    segment.content,
    live,
    segment.complete,
    onSettled,
  )
  return (
    <div
      data-turn-segment="text"
      data-segment-id={segment.id}
      data-streaming={live ? 'true' : 'false'}
      data-target-length={Array.from(segment.content).length}
      data-visible-length={Array.from(visible).length}
      data-complete={segment.complete ? 'true' : 'false'}
      className={`w-fit max-w-full rounded-[6px] border border-line bg-inset px-3.5 py-2.5 shadow-[0_3px_12px_rgba(0,0,0,.08)] ${
        live ? 'animate-rb-message-in motion-reduce:animate-none' : ''
      }`}
    >
      {visible ? <Markdown streaming={live && !settled}>{visible}</Markdown> : (
        <div className="flex min-h-5 items-center gap-2">
          <StreamDots />
          <span className="font-mono text-[9.5px] text-muted">writing…</span>
        </div>
      )}
    </div>
  )
}

function storedSegments(msg: ChatMessage): TurnSegment[] {
  const meta = (msg.metadata ?? {}) as Record<string, any>
  const raw = Array.isArray(meta.segments) ? meta.segments : null
  if (raw) {
    const segments = raw.flatMap((value: any, index: number): TurnSegment[] => {
      const id = value.id || `stored-${msg.index ?? 'message'}-${index}`
      if (value.type === 'text' && typeof value.content === 'string') {
        return [{ type: 'text', id, content: value.content, complete: true }]
      }
      if (value.type === 'media' && Array.isArray(value.media_items)) {
        return [{
          type: 'media',
          id,
          content: value.content || '',
          media_items: value.media_items,
        }]
      }
      return []
    })
    const knownPaths = new Set(
      segments.flatMap((segment) => segment.type === 'media'
        ? segment.media_items.map((item) => item.path)
        : []),
    )
    const extras: MediaItem[] = [
      ...(msg.media_items ?? []),
      ...(msg.media ?? []).map((path) => ({
        path,
        kind: 'photo' as const,
        filename: path.split('/').pop() || 'image',
        size: null,
        mime: 'image/*',
      })),
      ...(msg.media_refs ?? []).map((ref) => ({
        path: ref.path,
        kind: (ref.kind === 'video' || ref.kind === 'audio' || ref.kind === 'file'
          ? ref.kind
          : 'photo') as MediaItem['kind'],
        filename: ref.filename || ref.path.split('/').pop() || 'media',
        size: null,
        mime: ref.mime || 'application/octet-stream',
      })),
    ].filter((item) => !knownPaths.has(item.path))
    if (extras.length) {
      segments.push({
        type: 'media',
        id: `stored-${msg.index ?? 'message'}-extra-media`,
        content: '',
        media_items: extras,
      })
    }
    return segments
  }

  // Compatibility with transcripts created before ordered segments existed.
  const segments: TurnSegment[] = []
  for (const content of ((meta.intermediate as string[]) ?? [])) {
    segments.push({
      type: 'text',
      id: `stored-${msg.index ?? 'message'}-text-${segments.length}`,
      content,
      complete: true,
    })
  }
  for (const event of ((meta.media_events as MediaEvent[]) ?? [])) {
    segments.push({
      type: 'media',
      id: `stored-${msg.index ?? 'message'}-media-${segments.length}`,
      ...event,
    })
  }
  if (msg.media_items?.length) {
    segments.push({
      type: 'media',
      id: `stored-${msg.index ?? 'message'}-direct-media`,
      content: msg.content || '',
      media_items: msg.media_items,
    })
  }
  const legacyPaths: MediaItem[] = [
    ...(msg.media ?? []).map((path) => ({
      path,
      kind: 'photo' as const,
      filename: path.split('/').pop() || 'image',
      size: null,
      mime: 'image/*',
    })),
    ...(msg.media_refs ?? []).map((ref) => ({
      path: ref.path,
      kind: (ref.kind === 'video' || ref.kind === 'audio' || ref.kind === 'file'
        ? ref.kind
        : 'photo') as MediaItem['kind'],
      filename: ref.filename || ref.path.split('/').pop() || 'media',
      size: null,
      mime: ref.mime || 'application/octet-stream',
    })),
  ]
  if (legacyPaths.length) {
    segments.push({
      type: 'media',
      id: `stored-${msg.index ?? 'message'}-legacy-media`,
      content: '',
      media_items: legacyPaths,
    })
  }
  return segments
}

function TurnContent({
  segments,
  tools,
  finalText,
  live = false,
  finalized = false,
  onTextSettled,
}: {
  segments: TurnSegment[]
  tools: ToolEvent[]
  finalText: TextSegment | null
  live?: boolean
  finalized?: boolean
  onTextSettled?: (id: string) => void
}) {
  const toolRef = useRef<HTMLDivElement>(null)
  const finalRef = useRef<HTMLDivElement>(null)
  const wasFinalizedRef = useRef(finalized)

  useLayoutEffect(() => {
    const wasFinalized = wasFinalizedRef.current
    wasFinalizedRef.current = finalized
    if (!live || !finalized || wasFinalized) return
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    const tool = toolRef.current
    const text = finalRef.current
    if (!tool || !text) return
    const gap = 10
    tool.style.setProperty('--rb-flip-y', `${text.offsetHeight + gap}px`)
    text.style.setProperty('--rb-flip-y', `-${tool.offsetHeight + gap}px`)
    tool.classList.add('rb-flip-swap')
    text.classList.add('rb-flip-swap')
    tool.dataset.flipPhase = 'animating'
    text.dataset.flipPhase = 'animating'
    const clearFlip = () => {
      tool.classList.remove('rb-flip-swap')
      text.classList.remove('rb-flip-swap')
      tool.style.removeProperty('--rb-flip-y')
      text.style.removeProperty('--rb-flip-y')
      delete tool.dataset.flipPhase
      delete text.dataset.flipPhase
    }
    text.addEventListener('animationend', clearFlip, { once: true })
    return () => {
      text.removeEventListener('animationend', clearFlip)
      clearFlip()
    }
  }, [finalized, live])

  const textItem = (segment: TextSegment, final = false): ReactNode => (
    <div
      key={segment.id}
      ref={final ? finalRef : undefined}
      data-swap-item={final ? 'text' : undefined}
      className="w-full"
    >
      <AssistantTextCard
        segment={segment}
        live={live}
        onSettled={() => onTextSettled?.(segment.id)}
      />
    </div>
  )
  const items: ReactNode[] = segments.map((segment) => segment.type === 'text'
    ? textItem(segment)
    : <MediaEventView key={segment.id} event={segment} live={live} />)
  const toolItem = tools.length > 0 ? (
    <div key="tools" ref={toolRef} data-swap-item="tools" className="w-full">
      <ActivityStrip tools={tools} live={live} />
    </div>
  ) : null
  const finalItem = finalText ? textItem(finalText, true) : null

  // While generating, the current reply is always above activity. Once the
  // provider confirms final, only these two stable wrappers swap via FLIP.
  if (live && !finalized) {
    if (finalItem) items.push(finalItem)
    if (toolItem) items.push(toolItem)
  } else {
    if (toolItem) items.push(toolItem)
    if (finalItem) items.push(finalItem)
  }
  return (
    <div className="space-y-2.5" data-final-swap={finalized ? 'done' : 'pending'}>
      {items}
    </div>
  )
}

function messageCopyText(msg: ChatMessage): string {
  if (msg.content) return msg.content
  const meta = (msg.metadata ?? {}) as Record<string, any>
  const segments = Array.isArray(meta.segments) ? meta.segments : []
  return segments
    .filter((s: any) => s.type === 'text' && typeof s.content === 'string')
    .map((s: any) => s.content)
    .join('\n\n')
}

function MessageActions({
  msg,
  onRegenerate,
  onFork,
}: {
  msg: ChatMessage
  onRegenerate?: (msg: ChatMessage) => void
  onFork?: (msg: ChatMessage) => void
}) {
  const processing = useChat((s) => s.processing)
  const [copyState, setCopyState] = useState<'idle' | 'copied' | 'failed'>('idle')
  const meta = (msg.metadata ?? {}) as Record<string, any>
  const anchored = typeof meta.raw_index === 'number'
  const text = messageCopyText(msg)

  const copy = async () => {
    setCopyState((await copyText(text)) ? 'copied' : 'failed')
    setTimeout(() => setCopyState('idle'), 1500)
  }

  return (
    <div className="mt-1 flex items-center gap-3 font-mono text-[9.5px] text-faint">
      {text && (
        <button onClick={copy} className="hover:text-ink" title="Copy reply">
          {copyState === 'copied' ? 'copied ✓' : copyState === 'failed' ? 'copy failed' : 'copy'}
        </button>
      )}
      {anchored && onRegenerate && (
        <button
          onClick={() => onRegenerate(msg)}
          disabled={processing}
          className="hover:text-ink disabled:opacity-40"
          title="Re-ask with the current model (switch model first to compare)"
        >
          regen
        </button>
      )}
      {anchored && onFork && (
        <button
          onClick={() => onFork(msg)}
          disabled={processing}
          className="hover:text-ink disabled:opacity-40"
          title="Copy the chat up to this reply into a new chat"
        >
          fork
        </button>
      )}
    </div>
  )
}

const MessageRow = memo(function MessageRow({
  msg,
  onRegenerate,
  onFork,
}: {
  msg: ChatMessage
  onRegenerate?: (msg: ChatMessage) => void
  onFork?: (msg: ChatMessage) => void
}) {
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
          {(msg.media_refs ?? []).map((m, i) => (
            <img
              key={`${m.path}-${i}`}
              src={mediaUrl(m.path)}
              alt={m.filename || 'Attached image'}
              className="mt-2 max-h-80 max-w-full rounded-[4px]"
            />
          ))}
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
  const segments = storedSegments(msg)
  const contentIsDirectMediaCaption = !meta.segments && !!msg.media_items?.length
  const finalText = msg.content && !contentIsDirectMediaCaption
    ? {
        type: 'text' as const,
        id: `stored-${msg.index ?? 'message'}-final`,
        content: msg.content,
        complete: true,
      }
    : null
  return (
    <div className="my-3">
      <TurnContent segments={segments} tools={tools} finalText={finalText} />
      {meta.stopped && <div className="mt-1 font-mono text-[9.5px] text-warn">stopped by user</div>}
      {msg.usage && <UsageLine usage={msg.usage} />}
      <MessageActions msg={msg} onRegenerate={onRegenerate} onFork={onFork} />
    </div>
  )
})

function LiveTurnView({ turn }: { turn: LiveTurn }) {
  const commitLiveTurn = useChat((s) => s.commitLiveTurn)
  const [settled, setSettled] = useState<Record<string, boolean>>({})
  const markSettled = useCallback((id: string) => {
    setSettled((current) => current[id] ? current : { ...current, [id]: true })
  }, [])

  useEffect(() => {
    setSettled({})
  }, [turn.turnId])

  useEffect(() => {
    if (!turn.finalMessage) return
    const textIds = [
      ...turn.segments
        .filter((segment): segment is TextSegment => segment.type === 'text')
        .map((segment) => segment.id),
      ...(turn.currentText ? [turn.currentText.id] : []),
    ]
    if (textIds.every((id) => settled[id])) {
      const finalLength = Array.from(turn.currentText?.content || '').length
      const minimumDwell = Math.min(1600, Math.max(420, 260 + finalLength * 4))
      const remaining = Math.max(0, minimumDwell - (Date.now() - (turn.finalizedAt || 0)))
      // Committing swaps the live DOM for a stored message row, which would
      // wipe an in-progress text selection. Wait until the user is done.
      const selectionInsideLiveTurn = () => {
        const sel = window.getSelection()
        if (!sel || sel.isCollapsed) return false
        const host = document.querySelector('[data-live-turn]')
        return !!host && (host.contains(sel.anchorNode) || host.contains(sel.focusNode))
      }
      let timer = window.setTimeout(function attempt() {
        if (selectionInsideLiveTurn()) {
          timer = window.setTimeout(attempt, 1000)
          return
        }
        commitLiveTurn()
      }, remaining)
      return () => window.clearTimeout(timer)
    }
  }, [
    turn.finalMessage,
    turn.finalizedAt,
    turn.segments,
    turn.currentText,
    settled,
    commitLiveTurn,
  ])

  return (
    <div className="my-3" data-live-turn={turn.turnId || 'active'}>
      <TurnContent
        segments={turn.segments}
        tools={turn.tools}
        finalText={turn.currentText}
        live
        finalized={!!turn.finalMessage}
        onTextSettled={markSettled}
      />
      {turn.draftText && !turn.currentText && (
        <div
          data-stream-buffering="true"
          className="mt-2.5 flex items-center gap-2 py-1 text-muted"
        >
          <StreamDots />
          <span className="font-mono text-[10px]">composing…</span>
        </div>
      )}
      {!turn.segments.length && !turn.tools.length && !turn.currentText && !turn.draftText && (
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
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      onError('Microphone requires HTTPS or localhost')
      return
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      const ctx = new AudioContext()
      const src = ctx.createMediaStreamSource(stream)
      const an = ctx.createAnalyser()
      an.fftSize = 128
      src.connect(an)
      ctxRef.current = ctx
      setAnalyser(an)
      const preferredMime = 'audio/webm;codecs=opus'
      const rec = new MediaRecorder(
        stream,
        MediaRecorder.isTypeSupported(preferredMime) ? { mimeType: preferredMime } : undefined,
      )
      chunksRef.current = []
      rec.ondataavailable = (e) => e.data.size && chunksRef.current.push(e.data)
      rec.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop())
        ctx.close()
        setAnalyser(null)
        setBusy(true)
        try {
          const blob = new Blob(chunksRef.current, { type: rec.mimeType || 'audio/webm' })
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
    } catch (error) {
      const name = error instanceof DOMException ? error.name : ''
      onError(name === 'NotAllowedError' ? 'Microphone permission was denied' : 'Microphone unavailable')
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
  const cmd = (name: string, args?: Record<string, unknown>, optimistic?: Partial<Record<string, unknown>>) => {
    s.command(name, args)
    if (optimistic) useChat.setState(optimistic as any)
  }
  return (
    <Sheet open={open} onClose={onClose} title="Chat settings">
      <div className="space-y-5 pb-1">
        <div>
          <SectionLabel className="mb-1.5">Reasoning</SectionLabel>
          <Segmented
            options={REASONING}
            value={s.reasoningLevel as (typeof REASONING)[number]}
            onChange={(v) => cmd('set_reasoning_level', { reasoning_level: v }, { reasoningLevel: v })}
            size="large"
          />
        </div>
        <div>
          <SectionLabel className="mb-1.5">Context mode</SectionLabel>
          <Segmented
            options={CTX_MODES}
            value={s.contextMode as (typeof CTX_MODES)[number]}
            onChange={(v) => cmd('set_context_mode', { context_mode: v }, { contextMode: v })}
            size="large"
          />
        </div>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[12.5px] text-ink">Lightning mode</div>
            <div className="text-[10.5px] text-muted">OpenAI only · 2× token price</div>
          </div>
          <Toggle
            label="Lightning mode"
            size="large"
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
            label="Steering"
            size="large"
            value={s.steering}
            onChange={(v) => cmd('set_steering_mode', { steering_mode: v }, { steering: v })}
          />
        </div>
        <div className="flex items-center justify-between">
          <div>
            <div className="text-[12.5px] text-ink">Trace mode</div>
            <div className="text-[10.5px] text-muted">tool calls as chat messages (telegram-style)</div>
          </div>
          <Toggle
            label="Trace mode"
            size="large"
            value={s.trace}
            onChange={(v) => cmd('set_trace_mode', { trace_mode: v }, { trace: v })}
          />
        </div>
      </div>
    </Sheet>
  )
}

function ContextDetailsDialog({
  open,
  onClose,
  percent,
  used,
  activeLimit,
  modelLimit,
  mode,
  compactions,
}: {
  open: boolean
  onClose: () => void
  percent: number
  used: number
  activeLimit: number
  modelLimit: number
  mode: string
  compactions: number
}) {
  if (!open) return null
  const rows = [
    ['Used', `${fmtTokens(used)} tokens`],
    ['Active limit', `${fmtTokens(activeLimit)} tokens`],
    ['Model window', `${fmtTokens(modelLimit)} tokens`],
    ['Mode', mode],
    ['Compactions', String(compactions)],
  ]
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="context-details-title"
        className="w-full max-w-[330px] rounded-[8px] border border-line2 bg-panel p-4 shadow-[0_12px_36px_rgba(0,0,0,.5)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3">
          <div>
            <div id="context-details-title" className="text-[14px] font-semibold text-ink">Context</div>
            <div className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.08em] text-acc">
              {Math.min(100, Math.max(0, percent)).toFixed(0)}% used
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto flex h-8 w-8 items-center justify-center rounded-[4px] bg-raised2 text-[17px] leading-none text-muted hover:text-ink"
            aria-label="Close context details"
          >
            ×
          </button>
        </div>
        <div className="mt-4 divide-y divide-[var(--rb-line)] rounded-[5px] border border-line bg-raised px-3">
          {rows.map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-4 py-2.5">
              <span className="text-[11.5px] text-soft">{label}</span>
              <span className="text-right font-mono text-[10.5px] text-mist">{value}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ── composer ─────────────────────────────────────────────────

type ComposerActionTone = 'neutral' | 'primary' | 'destructive'

function ComposerAction({
  icon,
  label,
  tone = 'neutral',
  pulse,
  disabled,
  onClick,
}: {
  icon: keyof typeof CHAT_ACTION_PX
  label: string
  tone?: ComposerActionTone
  pulse?: boolean
  disabled?: boolean
  onClick: () => void
}) {
  const toneClass = {
    neutral: 'border-line2 bg-raised text-mist hover:border-acc/40 hover:bg-raised2',
    primary: 'border-acc bg-acc text-onacc hover:opacity-90',
    destructive: 'border-err/50 bg-err/10 text-err hover:bg-err/20',
  }[tone]
  const pixelColor = {
    neutral: 'rgb(var(--rb-mist))',
    primary: 'rgb(var(--rb-onacc))',
    destructive: 'rgb(var(--rb-err))',
  }[tone]
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className={`flex h-10 w-10 flex-none items-center justify-center rounded-[5px] border transition-colors disabled:pointer-events-none disabled:opacity-35 ${toneClass}`}
    >
      <span className={pulse ? 'animate-rb-pulse' : ''}>
        {icon === 'microphone' ? (
          <svg
            viewBox="0 0 20 20"
            className="h-[26px] w-[26px]"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <rect x="7" y="2.5" width="6" height="10" rx="3" />
            <path d="M4.8 9.7a5.2 5.2 0 0 0 10.4 0M10 15v2.5M7.2 17.5h5.6" />
          </svg>
        ) : (
          <PixelIcon
            px={CHAT_ACTION_PX[icon]}
            cols={icon === 'followup' ? 7 : 5}
            cell={icon === 'followup' ? 3 : 4}
            gap={icon === 'followup' ? 1 : 1.5}
            on={pixelColor}
          />
        )}
      </span>
    </button>
  )
}

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
    if (taRef.current) taRef.current.style.height = '40px'
  }

  const hasDraft = !!text.trim() || attachments.length > 0

  const onPaste = (e: React.ClipboardEvent) => {
    const files = Array.from(e.clipboardData.files)
    if (files.length) {
      e.preventDefault()
      doUpload(files)
    }
  }

  const action = s.processing && !hasDraft
    ? { icon: 'stop' as const, label: 'Stop the agent', tone: 'destructive' as const, onClick: s.stop, pulse: true }
    : s.processing
      ? { icon: 'followup' as const, label: 'Send follow-up', tone: 'primary' as const, onClick: submit, pulse: false }
      : { icon: 'send' as const, label: 'Send message', tone: 'primary' as const, onClick: submit, pulse: false }

  return (
    <div
      data-testid="chat-composer"
      className="border-t border-line bg-panel px-3 pt-2 pb-[calc(10px+env(safe-area-inset-bottom))] sm:px-4"
    >
      <div className="mx-auto max-w-[980px]">
        {attachments.length > 0 && (
          <div className="mb-2.5 flex flex-wrap gap-2">
          {attachments.map((a) => (
            <span key={a.id} className="flex min-h-8 items-center gap-2 rounded-[4px] border border-line bg-raised px-2.5 py-1.5">
              <span className="flex h-5 w-5 items-center justify-center rounded-[2px] bg-raised2 font-mono text-[7px] uppercase text-soft">
                {a.kind === 'photo' ? 'img' : (a.filename.split('.').pop() ?? 'f').slice(0, 3)}
              </span>
              <span className="max-w-44 truncate font-mono text-[10.5px] text-mist">{a.filename}</span>
              <button
                type="button"
                aria-label={`Remove ${a.filename}`}
                className="text-muted hover:text-err"
                onClick={() => setAttachments((prev) => prev.filter((x) => x.id !== a.id))}
              >
                ×
              </button>
            </span>
          ))}
          </div>
        )}
        <div className="flex items-end gap-1.5 sm:gap-2">
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
          <ComposerAction
            icon="add"
            label="Attach files"
            onClick={() => fileRef.current?.click()}
            disabled={uploading}
          />
          {voice.recording ? (
            <div className="flex h-10 min-w-0 flex-1 items-center gap-2 overflow-hidden rounded-[5px] border border-err/30 bg-raised px-3">
              <Dot color="err" pulse />
              <span className="font-mono text-[11px] text-ink">
                {Math.floor(voice.elapsed / 60)}:{String(voice.elapsed % 60).padStart(2, '0')}
              </span>
              <div className="min-w-0 flex-1 overflow-hidden">
                <Waveform analyser={voice.analyser} w={120} h={28} />
              </div>
            </div>
          ) : (
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
              className="max-h-40 min-h-10 min-w-0 flex-1 resize-none rounded-[5px] border border-line2 bg-raised px-3 py-2 text-[16px] leading-[22px] text-ink outline-none placeholder:text-muted focus:border-acc/50 sm:text-[14px]"
            />
          )}
          <ComposerAction
            icon="microphone"
            label={voice.recording ? 'Stop recording' : 'Voice input'}
            tone={voice.recording ? 'destructive' : 'neutral'}
            pulse={voice.recording}
            onClick={voice.recording ? voice.stop : voice.start}
            disabled={voice.busy}
          />
          <ComposerAction
            {...action}
            disabled={voice.recording || (!s.processing && !hasDraft)}
          />
        </div>
      </div>
    </div>
  )
}

// ── main page ────────────────────────────────────────────────

export default function ChatPage({
  conversationsOpen,
  onConversationsOpen,
  onConversationsClose,
}: {
  conversationsOpen: boolean
  onConversationsOpen: () => void
  onConversationsClose: () => void
}) {
  const s = useChat()
  const [showSettings, setShowSettings] = useState(false)
  const [showContext, setShowContext] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const [showScrollDown, setShowScrollDown] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const stickToBottomRef = useRef(true)
  const qc = useQueryClient()
  const setToast = useChat((st) => st.setToast)

  const { data: history, dataUpdatedAt } = useQuery({
    queryKey: ['messages', s.sessionId],
    queryFn: () => api.get<{ session_id: string; messages: ChatMessage[]; title: string }>('/api/sessions/active/messages?limit=200'),
    enabled: s.sessionId !== null,
    // Live WS events own the message list after the initial load — a focus
    // refetch would wipe tool timelines/usage and race in-flight messages.
    staleTime: 0,
    refetchOnWindowFocus: false,
  })

  const appliedHistoryRef = useRef(0)

  useEffect(() => {
    stickToBottomRef.current = true
    setShowScrollDown(false)
  }, [s.sessionId])

  useEffect(() => {
    if (
      history &&
      s.sessionId &&
      history.session_id === s.sessionId &&
      appliedHistoryRef.current !== dataUpdatedAt &&
      !useChat.getState().processing
    ) {
      appliedHistoryRef.current = dataUpdatedAt
      useChat.setState({ messages: history.messages, sessionTitle: history.title })
    }
  }, [history, dataUpdatedAt, s.sessionId])

  useEffect(() => {
    qc.invalidateQueries({ queryKey: ['sessions', 'user'] })
  }, [s.sessionId, qc])

  const scrollToBottom = useCallback((behavior: ScrollBehavior = 'auto') => {
    const el = scrollRef.current
    if (!el) return
    stickToBottomRef.current = true
    setShowScrollDown(false)
    el.scrollTo({ top: el.scrollHeight, behavior })
  }, [])

  const handleScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const distanceFromBottom = Math.max(0, el.scrollHeight - el.scrollTop - el.clientHeight)
    stickToBottomRef.current = distanceFromBottom < 120
    setShowScrollDown(distanceFromBottom > el.clientHeight / 2)
  }, [])

  // Stay pinned while new messages stream only if the user has not scrolled away.
  useLayoutEffect(() => {
    if (stickToBottomRef.current) scrollToBottom()
  }, [
    s.messages,
    s.liveTurn?.currentText?.content,
    s.liveTurn?.segments.length,
    s.liveTurn?.tools.length,
    scrollToBottom,
  ])

  // Media and animated text can change height after render; keep the bottom
  // anchored through those late layout shifts too.
  useEffect(() => {
    const content = contentRef.current
    if (!content || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      if (stickToBottomRef.current) scrollToBottom()
    })
    observer.observe(content)
    return () => observer.disconnect()
  }, [scrollToBottom])

  const onRegenerate = useCallback(async (msg: ChatMessage) => {
    const state = useChat.getState()
    const rawIndex = (msg.metadata as Record<string, any> | undefined)?.raw_index
    if (typeof rawIndex !== 'number' || state.processing) return
    // Trim the visible transcript back to before the user message that
    // produced this reply — the server truncates and re-dispatches it.
    const pos = state.messages.indexOf(msg)
    if (pos >= 0) {
      let userPos = -1
      for (let i = pos - 1; i >= 0; i--) {
        const m = state.messages[i]
        if (m.role === 'user' && (m.metadata as Record<string, any> | undefined)?.type !== 'compaction') {
          userPos = i
          break
        }
      }
      if (userPos >= 0) useChat.setState({ messages: state.messages.slice(0, userPos), processing: true })
    }
    try {
      await api.post('/api/sessions/active/regenerate', { raw_index: rawIndex })
    } catch (e: any) {
      setToast(e?.message || 'regenerate failed')
      qc.invalidateQueries({ queryKey: ['messages'] })
      useChat.setState({ processing: false })
    }
  }, [qc, setToast])

  const onFork = useCallback(async (msg: ChatMessage) => {
    const rawIndex = (msg.metadata as Record<string, any> | undefined)?.raw_index
    if (typeof rawIndex !== 'number') return
    try {
      await api.post<{ session_id: string }>('/api/sessions/active/fork', { raw_index: rawIndex })
      qc.invalidateQueries({ queryKey: ['sessions', 'user'] })
      setToast('Forked into a new chat')
    } catch (e: any) {
      setToast(e?.message || 'fork failed')
    }
  }, [qc, setToast])

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

  const contextThreshold = CONTEXT_THRESHOLDS[s.contextMode as (typeof CTX_MODES)[number]] ?? CONTEXT_THRESHOLDS.normal
  const effectiveContextMax = Math.max(1, Math.round(s.contextMax * contextThreshold))
  const percent = (s.contextUsed / effectiveContextMax) * 100
  const modelShort = s.model.split('/').pop() ?? s.model
  const empty = s.messages.length === 0 && !s.liveTurn
  const contextLabel = `Context ${s.contextMode}, ${percent.toFixed(0)} percent, ${fmtTokens(s.contextUsed)} / ${fmtTokens(effectiveContextMax)}`

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
      {/* chat column */}
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="flex min-h-[48px] items-center gap-2 border-b border-line px-3 py-1.5 lg:min-h-[52px] lg:px-4 lg:pr-[68px]">
          <div className="hidden min-w-0 items-center gap-2 lg:flex">
            <button
              type="button"
              className="flex h-9 w-9 flex-none items-center justify-center rounded-[5px] border border-line bg-raised text-mist hover:border-line2 hover:text-ink"
              onClick={onConversationsOpen}
              aria-label="Open chats"
            >
              <span className="flex w-[15px] flex-col gap-[3px]" aria-hidden="true">
                <span className="h-[1.5px] w-full bg-current" />
                <span className="h-[1.5px] w-full bg-current" />
                <span className="h-[1.5px] w-full bg-current" />
              </span>
            </button>
            <span className="hidden max-w-[220px] truncate text-[13px] font-semibold text-ink xl:block">
              {s.sessionTitle || 'New chat'}
            </span>
          </div>
          <span className="min-w-0 flex-1 truncate font-mono text-[9.5px] text-soft sm:text-[10.5px] lg:flex-none">
            {modelShort} · {s.reasoningLevel}
            {s.lightning ? ' · ⚡' : ''}
          </span>
          <div className="group relative ml-auto flex-none">
            <button
              type="button"
              onClick={() => setShowContext(true)}
              className="rounded-[5px] outline-none hover:border-acc/30 focus-visible:ring-1 focus-visible:ring-acc"
              aria-label={contextLabel}
            >
              <ContextMeter
                percent={percent}
                mode={s.contextMode}
                detail={`${fmtTokens(s.contextUsed)} / ${fmtTokens(effectiveContextMax)}`}
                blocks={10}
              />
            </button>
            {!showContext && (
              <div
                role="tooltip"
                className="pointer-events-none invisible absolute right-0 top-full z-30 mt-1.5 w-max max-w-[230px] rounded-[4px] border border-line2 bg-deep px-2.5 py-2 opacity-0 shadow-[0_6px_18px_rgba(0,0,0,.35)] transition-none group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100"
              >
                <div className="font-mono text-[9px] uppercase tracking-[0.08em] text-acc">{s.contextMode}</div>
                <div className="mt-1 font-mono text-[10px] text-mist">
                  {fmtTokens(s.contextUsed)} / {fmtTokens(effectiveContextMax)} tokens
                </div>
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={() => setShowSettings(true)}
            className="flex h-9 w-9 flex-none items-center justify-center rounded-[5px] border border-line bg-raised text-mist hover:border-acc/30 hover:text-ink"
            aria-label="Chat settings"
          >
            <svg
              viewBox="0 0 20 20"
              className="h-[17px] w-[17px]"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <circle cx="10" cy="10" r="2.5" />
              <path d="M10 2.8v1.5M10 15.7v1.5M17.2 10h-1.5M4.3 10H2.8M15.1 4.9 14 6M6 14l-1.1 1.1M15.1 15.1 14 14M6 6 4.9 4.9" />
              <circle cx="10" cy="10" r="6" />
            </svg>
          </button>
        </div>

        <div className="relative min-h-0 flex-1">
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className="h-full min-h-0 overflow-y-auto px-4 lg:px-8"
          >
            <div ref={contentRef} className={empty ? 'h-full' : ''}>
              {empty ? (
                <div className="flex h-full flex-col items-center justify-center gap-7 py-8">
                  <div className="sm:hidden">
                    <PixelWordmark w={320} h={76} cell={5} gap={1.5} />
                  </div>
                  <div className="hidden sm:block">
                    <PixelWordmark w={480} h={104} cell={7} gap={2} />
                  </div>
                  <div className="text-[15px] font-medium tracking-[0.01em] text-soft">Your Personal AI Assistant</div>
                  <div className="grid w-full max-w-[720px] grid-cols-1 gap-3 px-6 sm:grid-cols-3">
                    {['What can you do?', 'Schedule a daily digest', 'Search my memory'].map((p) => (
                      <button
                        key={p}
                        onClick={() => s.send(p)}
                        className="min-h-12 rounded-[6px] border border-line2 bg-raised px-4 py-2.5 text-[13px] font-medium text-soft transition-colors hover:border-acc/30 hover:bg-raised2 hover:text-ink"
                      >
                        {p}
                      </button>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="mx-auto max-w-[720px] py-4">
                  {s.messages.map((m, i) => (
                    <MessageRow key={i} msg={m} onRegenerate={onRegenerate} onFork={onFork} />
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
          </div>

          {showScrollDown && (
            <button
              type="button"
              onClick={() => scrollToBottom(
                window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
              )}
              className="absolute bottom-4 left-1/2 z-10 flex h-10 -translate-x-1/2 items-center justify-center gap-2 rounded-[3px] border border-acc/35 bg-deep/95 px-3.5 text-acc shadow-[0_7px_20px_rgba(0,0,0,.42)] transition-colors hover:border-acc hover:bg-raised2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-acc"
              aria-label="Scroll to latest message"
              title="Scroll to latest message"
            >
              <PixelIcon
                px={CHAT_ACTION_PX.down}
                cols={5}
                cell={2}
                gap={1}
                on="rgb(var(--rb-acc))"
              />
              <span className="font-mono text-[10px] font-semibold uppercase tracking-[0.08em] text-mist">
                Down
              </span>
            </button>
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

      <ConversationDrawer open={conversationsOpen} onClose={onConversationsClose} />
      <ContextDetailsDialog
        open={showContext}
        onClose={() => setShowContext(false)}
        percent={percent}
        used={s.contextUsed}
        activeLimit={effectiveContextMax}
        modelLimit={s.contextMax}
        mode={s.contextMode}
        compactions={s.contextCompactions}
      />
      <ChatSettingsSheet open={showSettings} onClose={() => setShowSettings(false)} />
    </div>
  )
}
