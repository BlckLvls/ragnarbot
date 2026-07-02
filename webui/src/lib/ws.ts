// WebSocket client + live chat store (zustand).

import { create } from 'zustand'
import type { ChatMessage, Notification, TurnUsage } from './api'

export type ConnState = 'online' | 'connecting' | 'offline'

export interface ToolEvent {
  turn_id?: string
  tool: string
  args_preview?: string
  status?: 'ok' | 'error'
  duration_ms?: number
  done: boolean
}

export interface LiveTurn {
  turnId: string | null
  text: string
  tools: ToolEvent[]
  system?: boolean
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
}

let socket: WebSocket | null = null
let reconnectDelay = 500

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
}))

function sendJson(data: Record<string, unknown>) {
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify(data))
  } else {
    useChat.getState().setToast('Not connected')
  }
}

export function connectWs(onNotification?: (n: Notification) => void) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws'
  const url = `${proto}://${location.host}/ws`
  useChat.setState({ conn: 'connecting' })
  socket = new WebSocket(url)

  socket.onopen = () => {
    reconnectDelay = 500
    useChat.setState({ conn: 'online' })
  }

  socket.onclose = () => {
    useChat.setState({ conn: 'offline', processing: false })
    setTimeout(() => connectWs(onNotification), reconnectDelay)
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
        useChat.setState({
          sessionId: ev.session_id,
          sessionTitle: ev.session_title || '',
          model: ev.model || '',
          reasoningLevel: ev.reasoning_level || 'medium',
          contextMode: ev.context_mode || 'normal',
          lightning: !!ev.lightning,
          trace: !!ev.trace,
          steering: !!ev.steering,
          processing: !!ev.processing,
        })
        break
      case 'processing':
        // value:false is the stop_typing signal — the turn produced no final
        // message, so clear any lingering live turn as well
        useChat.setState(ev.value ? { processing: true } : { processing: false, liveTurn: null })
        break
      case 'turn_started':
        useChat.setState({
          processing: true,
          liveTurn: { turnId: ev.turn_id ?? null, text: '', tools: [], system: !!ev.system },
        })
        break
      case 'delta': {
        // deltas ride one ordered WebSocket — no dedup needed (a seq counter
        // would restart every LLM iteration and drop post-tool text)
        const turn = s.liveTurn ?? { turnId: ev.turn_id ?? null, text: '', tools: [] }
        useChat.setState({
          processing: true,
          liveTurn: { ...turn, text: turn.text + (ev.text || '') },
        })
        break
      }
      case 'tool_start': {
        const turn = s.liveTurn ?? { turnId: ev.turn_id ?? null, text: '', tools: [] }
        useChat.setState({
          liveTurn: {
            ...turn,
            tools: [...turn.tools, { turn_id: ev.turn_id, tool: ev.tool, args_preview: ev.args_preview, done: false }],
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
        // Non-streaming providers: coarse text blocks during the turn
        const turn = s.liveTurn ?? { turnId: ev.turn_id ?? null, text: '', tools: [] }
        const text = ev.message?.content || ''
        useChat.setState({
          liveTurn: { ...turn, text: turn.text ? `${turn.text}\n\n${text}` : text },
        })
        break
      }
      case 'user_message':
        useChat.setState({
          messages: [...s.messages, ev.message as ChatMessage],
        })
        break
      case 'media':
        // media sent mid-turn (send_photo/video/file) — append a bubble
        // without touching processing/liveTurn state
        useChat.setState({
          messages: [...s.messages, ev.message as ChatMessage],
        })
        break
      case 'final': {
        const msg = ev.message as ChatMessage & { usage?: TurnUsage }
        const tools = s.liveTurn?.tools ?? []
        useChat.setState({
          messages: [
            ...s.messages,
            { ...msg, metadata: { ...(msg.metadata || {}), tools } },
          ],
          liveTurn: null,
          processing: false,
        })
        break
      }
      case 'turn_ended':
        if (ev.status === 'stopped' && s.liveTurn?.text) {
          // keep partial text as a message so the user sees what was generated;
          // the final event never comes for stopped turns, so usage attaches here
          useChat.setState({
            messages: [
              ...s.messages,
              {
                role: 'assistant',
                content: s.liveTurn.text,
                usage: ev.usage,
                metadata: { stopped: true, tools: s.liveTurn.tools },
              },
            ],
          })
        }
        // NOTE: the final message arrives AFTER turn_ended (it carries its own
        // usage), so keep liveTurn until final lands unless the turn was stopped.
        if (ev.status === 'stopped') {
          useChat.setState({ liveTurn: null, processing: false })
        }
        break
      case 'context_info':
        useChat.setState({ contextUsed: ev.used_tokens || 0, contextMax: ev.max_tokens || 200000 })
        break
      case 'session_changed':
        useChat.setState({ sessionId: ev.session_id, messages: [], liveTurn: null })
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
}
