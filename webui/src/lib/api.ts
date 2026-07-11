// REST client for the ragnarbot web console API.

export class ApiError extends Error {
  status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  })
  const isJson = res.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await res.json() : await res.text()
  if (!res.ok) {
    const message = isJson && data?.error ? data.error : `HTTP ${res.status}`
    throw new ApiError(message, res.status)
  }
  return data as T
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),

  async upload(files: File[]): Promise<UploadResult[]> {
    const fd = new FormData()
    for (const f of files) fd.append('file', f, f.name)
    const res = await fetch('/api/uploads', { method: 'POST', body: fd })
    if (!res.ok) throw new ApiError('upload failed', res.status)
    return res.json()
  },

  async transcribe(blob: Blob): Promise<string> {
    const fd = new FormData()
    const extension = blob.type.includes('mp4') || blob.type.includes('aac')
      ? 'm4a'
      : blob.type.includes('ogg')
        ? 'ogg'
        : 'webm'
    fd.append('file', blob, `voice.${extension}`)
    const res = await fetch('/api/transcribe', { method: 'POST', body: fd })
    const data = await res.json()
    if (!res.ok) throw new ApiError(data?.error || 'transcription failed', res.status)
    return data.text
  },
}

export const mediaUrl = (path: string) => `/api/media?path=${encodeURIComponent(path)}`

// ── shared types ─────────────────────────────────────────────

export interface UploadResult {
  id: string
  filename: string
  size: number
  kind: 'photo' | 'file'
  mime_type: string
}

export interface SessionInfo {
  session_id: string
  channel: string
  user_key: string
  created_at: string | null
  updated_at: string | null
  title: string
  active: boolean
}

export interface MediaItem {
  path: string
  kind: 'photo' | 'video' | 'audio' | 'file'
  filename: string
  size: number | null
  mime: string
}

export interface ChatMessage {
  index?: number
  role: 'user' | 'assistant'
  content: string
  metadata?: Record<string, unknown>
  media_refs?: { path: string; mime?: string; kind?: string; filename?: string }[]
  media_items?: MediaItem[]
  media?: string[]
  usage?: TurnUsage
  attachments?: { type: string; filename: string }[]
}

export interface TurnUsage {
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  model: string
  duration_ms: number
}

export interface ConfigField {
  path: string
  type: string
  default: unknown
  value: unknown
  reload: 'hot' | 'warm' | 'cold' | string
  label: string
  pattern?: string
  enum?: string[]
  ge?: number
  le?: number
  options?: ModelOption[]
  depends_on?: { value: string; match: string; creds_paths: string[]; hint: string }[]
}

export interface ModelOption {
  id: string
  name: string
  description: string
  provider: string
  provider_name: string
  vision: boolean
  oauth: boolean
}

export interface SecretEntry {
  path: string
  set: boolean
  api_key_url?: string
}

export interface CronJob {
  id: string
  name: string
  enabled: boolean
  schedule: { kind: 'at' | 'every' | 'cron'; at_ms?: number | null; every_ms?: number | null; expr?: string | null; tz?: string | null }
  payload: { kind: string; message: string; mode: 'isolated' | 'session'; deliver: boolean; channel?: string | null; to?: string | null; agent?: string | null }
  state: { next_run_at_ms?: number | null; last_run_at_ms?: number | null; last_status?: string | null; last_error?: string | null }
  created_at_ms: number
  updated_at_ms: number
  delete_after_run: boolean
}

export interface HookDef {
  id: string
  name: string
  instructions: string
  mode: 'alert' | 'silent'
  enabled: boolean
  channel?: string | null
  to?: string | null
  created_at_ms: number
  updated_at_ms: number
  trigger_count: number
}

export interface AgentTask {
  id: string
  label: string
  agent: string
  status: 'running' | 'completed' | 'stopped' | 'error'
  message_count: number
  created_at: string
}

export interface AgentDefinitionInfo {
  name: string
  description: string
  source: 'workspace' | 'builtin'
  path: string
  config: {
    model: string
    reasoning_level: string
    allowed_tools: string | string[]
    allowed_skills: string | string[]
  }
  instructions: string
}

export interface Notification {
  id: string
  ts: string
  kind: string
  title: string
  body: string
  status: string
  source_id?: string | null
  read?: boolean
}

export interface StatusFull {
  version: string
  profile: string
  workspace: string
  hooks: { enabled: boolean; port: number; count: number }
}

export interface WorkspaceEntry {
  path: string
  dir: boolean
  kind: 'directory' | 'text' | 'image' | 'video'
  size: number | null
  modified: number | null
  previewable: boolean
}

export interface SkillSummary {
  name: string
  description?: string
  source?: string
  always?: string | boolean
  [key: string]: unknown
}
