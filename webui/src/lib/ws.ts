// WebSocket client + live chat store (zustand).

import { create } from 'zustand'
import type { ChatMessage, MediaItem, Notification, TurnUsage } from './api'

export type ConnState = 'online' | 'connecting' | 'offline'

export interface ToolEvent {
  turn_id?: string
  tool: string
  args_preview?: string
  status?: 'ok' | 'error'
  duration_ms?: number
  done: boolean
}

export interface TextSegment {
  type: 'text'
  id: string
  content: string
  complete: boolean
}

export interface MediaSegment extends MediaEvent {
  type: 'media'
  id: string
}

export type TurnSegment = TextSegment | MediaSegment

export interface LiveTurn {
  turnId: string | null
  segments: TurnSegment[]
  currentText: TextSegment | null
  draftText: string
  tools: ToolEvent[]
  finalMessage?: ChatMessage
  finalizedAt?: number
  system?: boolean
}

export interface MediaEvent {
  content: string
  media_items: MediaItem[]
}

interface ChatState {
  conn: ConnState
  processing: boolean
  sessionId: string | null
  sessionTitle: string
  model: string
  reasoningLevel: string
  contextMode: string
  lightning: boolean
  trace: boolean
  steering: boolean
  contextUsed: number
  contextMax: number
  contextCompactions: number
  messages: ChatMessage[]
  liveTurn: LiveTurn | null
  unread: number
  toast: string | null
  // actions
  send: (text: string, attachmentIds?: string[]) => void
  stop: () => void
  command: (name: string, args?: Record<string, unknown>) => void
  setMessages: (msgs: ChatMessage[]) => void
  prependMessages: (msgs: ChatMessage[]) => void
  setUnread: (n: number) => void
  setToast: (t: string | null) => void
  commitLiveTurn: () => void
}

let socket: WebSocket | null = null
let reconnectDelay = 500
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let reconnectEnabled = true

function emptyTurn(turnId?: string | null, system = false): LiveTurn {
  return {
    turnId: turnId ?? null,
    segments: [],
    currentText: null,
    draftText: '',
    tools: [],
    system,
  }
}

function segmentId(turn: LiveTurn, type: TurnSegment['type']): string {
  return `${turn.turnId ?? 'turn'}-${type}-${turn.segments.length}`
}

function appendDelta(turn: LiveTurn, text: string): LiveTurn {
  if (!text) return turn
  if (turn.currentText && !turn.currentText.complete) {
    return {
      ...turn,
      currentText: { ...turn.currentText, content: turn.currentText.content + text },
    }
  }
  const draftText = turn.draftText + text
  const latestMedia = [...turn.segments]
    .reverse()
    .find((segment): segment is MediaSegment => segment.type === 'media')
  const caption = latestMedia?.content.trim() || ''
  if (caption && caption.startsWith(draftText)) {
    return { ...turn, draftText }
  }
  const content = caption && draftText.startsWith(caption)
    ? draftText.slice(caption.length).replace(/^\s+/, '')
    : draftText
  if (!content) return { ...turn, draftText: '' }
  return {
    ...turn,
    draftText: '',
    currentText: {
      type: 'text',
      id: segmentId(turn, 'text'),
      content,
      complete: false,
    },
  }
}

function stripMediaCaptionEcho(content: string, segments: TurnSegment[]): string {
  let cleaned = content
  const captions = segments
    .filter((segment): segment is MediaSegment => segment.type === 'media')
    .map((segment) => segment.content.trim())
    .filter(Boolean)
  while (cleaned) {
    const caption = captions.find((candidate) => cleaned.startsWith(candidate))
    if (!caption) break
    cleaned = cleaned.slice(caption.length).replace(/^\s+/, '')
  }
  return cleaned
}

function serializeSegments(segments: TurnSegment[]): TurnSegment[] {
  return segments.map((segment) => (
    segment.type === 'text' ? { ...segment, complete: true } : segment
  ))
}

function messageFromTurn(turn: LiveTurn, message: ChatMessage): ChatMessage {
  return {
    ...message,
    metadata: {
      ...(message.metadata || {}),
      turn_id: turn.turnId,
      segments: serializeSegments(turn.segments),
      tools: turn.tools,
    },
  }
}

function commitPendingTurn(state: ChatState): Pick<ChatState, 'messages' | 'liveTurn'> {
  if (!state.liveTurn?.finalMessage) {
    return { messages: state.messages, liveTurn: state.liveTurn }
  }
  return {
    messages: [...state.messages, state.liveTurn.finalMessage],
    liveTurn: null,
  }
}

export const useChat = create<ChatState>((set, get) => ({
  conn: 'connecting',
  processing: false,
  sessionId: null,
  sessionTitle: '',
  model: '',
  reasoningLevel: 'medium',
  contextMode: 'normal',
  lightning: false,
  trace: false,
  steering: true,
  contextUsed: 0,
  contextMax: 200000,
  contextCompactions: 0,
  messages: [],
  liveTurn: null,
  unread: 0,
  toast: null,

  send: (text, attachmentIds = []) => {
    sendJson({ type: 'send', text, attachment_ids: attachmentIds })
  },
  stop: () => sendJson({ type: 'stop' }),
  command: (name, args) => sendJson({ type: 'command', name, args }),
  setMessages: (msgs) => set({ messages: msgs }),
  prependMessages: (msgs) => set((s) => ({ messages: [...msgs, ...s.messages] })),
  setUnread: (n) => set({ unread: n }),
  setToast: (t) => set({ toast: t }),
  commitLiveTurn: () => set((s) => {
    if (!s.liveTurn?.finalMessage) return s
    return {
      messages: [...s.messages, s.liveTurn.finalMessage],
      liveTurn: null,
      processing: false,
    }
  }),
}))

function sendJson(data: Record<string, unknown>) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(data))
  } else {
    useChat.getState().setToast('Not connected')
  }
}

export function connectWs(onNotification?: (n: Notification) => void) {
  reconnectEnabled = true
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return disconnectWs
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}/ws`
  useChat.setState({ conn: 'connecting' })
  socket = new WebSocket(url)

  socket.onopen = () => {
    reconnectDelay = 500
    useChat.setState({ conn: 'online' })
  }

  socket.onclose = () => {
    socket = null
    useChat.setState({ conn: 'offline', processing: false })
    if (!reconnectEnabled) return
    reconnectTimer = setTimeout(() => connectWs(onNotification), reconnectDelay)
    reconnectDelay = Math.min(reconnectDelay * 2, 10000)
  }

  socket.onmessage = (raw) => {
    let ev: any
    try {
      ev = JSON.parse(raw.data)
    } catch {
      return
    }
    const s = useChat.getState()
    switch (ev.type) {
      case 'state':
        const sessionChanged = s.sessionId !== null && s.sessionId !== ev.session_id
        useChat.setState({
          sessionId: ev.session_id,
          sessionTitle: ev.session_title || '',
          model: ev.model || '',
          reasoningLevel: ev.reasoning_level || 'medium',
          contextMode: ev.context_mode || 'normal',
          contextUsed: ev.context_used ?? 0,
          contextMax: ev.context_max ?? 200000,
          contextCompactions: ev.context_compactions ?? 0,
          lightning: !!ev.lightning,
          trace: !!ev.trace,
          steering: !!ev.steering,
          processing: !!ev.processing,
          messages: sessionChanged ? [] : s.messages,
          liveTurn: sessionChanged ? null : s.liveTurn,
        })
        break
      case 'processing':
        // value:false is the stop_typing signal — the turn produced no final
        // message, so clear any lingering live turn as well
        useChat.setState(ev.value ? { processing: true } : { processing: false, liveTurn: null })
        break
      case 'turn_started': {
        const pending = commitPendingTurn(s)
        useChat.setState({
          messages: pending.messages,
          processing: true,
          liveTurn: emptyTurn(ev.turn_id, !!ev.system),
        })
        break
      }
      case 'delta': {
        // deltas ride one ordered WebSocket — no dedup needed (a seq counter
        // would restart every LLM iteration and drop post-tool text)
        const turn = s.liveTurn ?? emptyTurn(ev.turn_id)
        useChat.setState({
          processing: true,
          liveTurn: appendDelta(turn, ev.text || ''),
        })
        break
      }
      case 'tool_start': {
        const turn = s.liveTurn ?? emptyTurn(ev.turn_id)
        const segments = turn.currentText?.content.trim()
          ? [...turn.segments, { ...turn.currentText, complete: true }]
          : turn.segments
        const tool: ToolEvent = {
          turn_id: ev.turn_id,
          tool: ev.tool,
          args_preview: ev.args_preview,
          done: false,
        }
        useChat.setState({
          liveTurn: {
            ...turn,
            segments,
            currentText: null,
            draftText: '',
            tools: [...turn.tools, tool],
          },
        })
        break
      }
      case 'tool_end': {
        const turn = s.liveTurn
        if (!turn) break
        const tools = [...turn.tools]
        for (let i = tools.length - 1; i >= 0; i--) {
          if (tools[i].tool === ev.tool && !tools[i].done) {
            tools[i] = { ...tools[i], done: true, status: ev.status, duration_ms: ev.duration_ms }
            break
          }
        }
        useChat.setState({ liveTurn: { ...turn, tools } })
        break
      }
      case 'intermediate': {
        // Keep every coarse provider step as its own visible reply block.
        const turn = s.liveTurn ?? emptyTurn(ev.turn_id)
        const content = (ev.message?.content || '').trim()
        if (!content) break
        const segments = turn.currentText?.content.trim()
          ? [...turn.segments, { ...turn.currentText, complete: true }]
          : turn.segments
        useChat.setState({
          liveTurn: {
            ...turn,
            segments: [
              ...segments,
              { type: 'text', id: segmentId({ ...turn, segments }, 'text'), content, complete: true },
            ],
            currentText: null,
            draftText: '',
          },
        })
        break
      }
      case 'user_message': {
        const pending = commitPendingTurn(s)
        useChat.setState({
          messages: [...pending.messages, ev.message as ChatMessage],
          liveTurn: pending.liveTurn,
        })
        break
      }
      case 'media': {
        if (s.liveTurn) {
          const segments = s.liveTurn.currentText?.content.trim()
            ? [...s.liveTurn.segments, { ...s.liveTurn.currentText, complete: true }]
            : s.liveTurn.segments
          useChat.setState({
            liveTurn: {
              ...s.liveTurn,
              segments: [
                ...segments,
                {
                  type: 'media',
                  id: segmentId({ ...s.liveTurn, segments }, 'media'),
                  content: ev.message?.content || '',
                  media_items: ev.message?.media_items || [],
                },
              ],
              currentText: null,
              draftText: '',
            },
          })
        } else {
          useChat.setState({ messages: [...s.messages, ev.message as ChatMessage] })
        }
        break
      }
      case 'final': {
        const msg = ev.message as ChatMessage & { usage?: TurnUsage }
        const turn = s.liveTurn ?? emptyTurn(ev.turn_id)
        const finalText = stripMediaCaptionEcho(
          (msg.content || turn.currentText?.content || turn.draftText || '').trim(),
          turn.segments,
        )
        const currentText = turn.currentText
          ? { ...turn.currentText, content: finalText || turn.currentText.content, complete: true }
          : finalText
            ? {
                type: 'text' as const,
                id: segmentId(turn, 'text'),
                content: finalText,
                complete: true,
              }
            : null
        const completedTurn = { ...turn, currentText, draftText: '' }
        useChat.setState({
          liveTurn: {
            ...completedTurn,
            finalMessage: messageFromTurn(completedTurn, msg),
            finalizedAt: Date.now(),
          },
          processing: false,
        })
        break
      }
      case 'turn_ended':
        if (
          ev.status === 'stopped' &&
          s.liveTurn &&
          (s.liveTurn.segments.length || s.liveTurn.currentText || s.liveTurn.draftText || s.liveTurn.tools.length)
        ) {
          const draft = stripMediaCaptionEcho(
            (s.liveTurn.currentText?.content || s.liveTurn.draftText).trim(),
            s.liveTurn.segments,
          )
          const turn = {
            ...s.liveTurn,
            currentText: draft
              ? {
                  type: 'text' as const,
                  id: segmentId(s.liveTurn, 'text'),
                  content: draft,
                  complete: true,
                }
              : null,
            draftText: '',
          }
          const message = messageFromTurn(turn, {
            role: 'assistant',
            content: turn.currentText?.content || '',
            usage: ev.usage,
            metadata: { stopped: true },
          })
          useChat.setState({
            messages: [...s.messages, message],
          })
        }
        // NOTE: the final message arrives AFTER turn_ended (it carries its own
        // usage), so keep liveTurn until final lands unless the turn was stopped.
        if (ev.status === 'stopped') {
          useChat.setState({ liveTurn: null, processing: false })
        } else {
          // Some providers emit final before turn_ended and others after it.
          // In both orders the turn is no longer processing; final may still
          // consume the preserved liveTurn text/tools immediately afterwards.
          useChat.setState({ processing: false })
        }
        break
      case 'context_info':
        useChat.setState({
          contextUsed: ev.used_tokens || 0,
          contextMax: ev.max_tokens || 200000,
          contextCompactions: ev.compactions ?? s.contextCompactions,
        })
        break
      case 'session_changed':
        useChat.setState({
          sessionId: ev.session_id,
          messages: [],
          liveTurn: null,
          contextUsed: ev.used_tokens ?? 0,
          contextMax: ev.max_tokens ?? s.contextMax,
          contextCompactions: ev.compactions ?? 0,
        })
        // page components reload history via react-query invalidation on sessionId change
        break
      case 'notification':
        useChat.setState({ unread: s.unread + 1 })
        onNotification?.(ev as Notification)
        break
      case 'error':
        useChat.setState({ toast: ev.text || 'Error', processing: false })
        break
      default:
        break
    }
  }
  return disconnectWs
}

export function disconnectWs() {
  reconnectEnabled = false
  if (reconnectTimer) {
    clearTimeout(reconnectTimer)
    reconnectTimer = null
  }
  if (socket) {
    socket.onclose = null
    socket.close()
    socket = null
  }
}
