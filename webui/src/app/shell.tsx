// App shell: focused navigation plus the shared Activity center.

import { ReactNode, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, Notification } from '../lib/api'
import { useChat } from '../lib/ws'
import { NAV_PX, PixelIcon } from '../components/pixel'
import { Dot, SectionLabel, StatusPill } from '../components/ui'

export const NAV_ITEMS = [
  { label: 'Chat', path: '/' },
  { label: 'Files', path: '/files' },
  { label: 'Skills', path: '/skills' },
  { label: 'Cron', path: '/cron' },
  { label: 'Agents', path: '/agents' },
  { label: 'Settings', path: '/settings' },
] as const

const TAB_ITEMS = ['Chat', 'Files', 'Skills', 'More'] as const

function ConnDot() {
  const conn = useChat((s) => s.conn)
  const color = conn === 'online' ? 'ok' : conn === 'connecting' ? 'warn' : 'err'
  return <Dot color={color} pulse={conn !== 'online'} />
}

function navColors(active: boolean) {
  return {
    on: active ? 'rgb(var(--rb-acc))' : 'rgb(var(--rb-soft) / .7)',
    fg: active ? 'text-ink' : 'text-soft',
    bg: active ? 'bg-raised2' : '',
  }
}

// ── desktop sidebar ──────────────────────────────────────────

export function Sidebar({ version }: { version: string }) {
  const location = useLocation()
  return (
    <div className="hidden lg:flex w-[212px] min-w-[212px] flex-col border-r border-line bg-panel">
      <div className="flex items-center gap-2 px-4 pb-3 pt-[18px]">
        <span className="text-[15px] font-bold tracking-[-0.2px] text-ink">ragnarbot</span>
        <ConnDot />
        <span className="ml-auto font-mono text-[10px] text-muted">v{version}</span>
      </div>
      <nav className="flex flex-col gap-[1px] p-2">
        {NAV_ITEMS.map((item) => {
          const active =
            item.path === '/' ? location.pathname === '/' : location.pathname.startsWith(item.path)
          const c = navColors(active)
          return (
            <NavLink
              key={item.label}
              to={item.path}
              className={`flex items-center gap-[11px] rounded-[3px] px-2.5 py-2 hover:bg-raised2/60 ${c.bg}`}
            >
              <PixelIcon px={NAV_PX[item.label]} on={c.on} />
              <span className={`text-[13px] font-medium tracking-[0.1px] ${c.fg}`}>{item.label}</span>
            </NavLink>
          )
        })}
      </nav>
    </div>
  )
}

// ── mobile tab bar ───────────────────────────────────────────

export function TabBar({ onMore }: { onMore: () => void }) {
  const location = useLocation()
  const navigate = useNavigate()
  const mainPaths: Record<string, string> = { Chat: '/', Files: '/files', Skills: '/skills' }
  const morePaths = ['/cron', '/agents', '/settings', '/hooks']

  return (
    <div className="flex h-[calc(64px+env(safe-area-inset-bottom))] min-h-[calc(64px+env(safe-area-inset-bottom))] items-start border-t border-line bg-panel px-2 pb-[calc(6px+env(safe-area-inset-bottom))] pt-3 lg:hidden">
      {TAB_ITEMS.map((label) => {
        const isMore = label === 'More'
        const active = isMore
          ? morePaths.some((p) => location.pathname.startsWith(p))
          : mainPaths[label] === '/'
            ? location.pathname === '/'
            : location.pathname.startsWith(mainPaths[label])
        const c = navColors(active)
        return (
          <button
            key={label}
            onClick={() => (isMore ? onMore() : navigate(mainPaths[label]))}
            className="flex flex-1 flex-col items-center gap-[6px] pb-0 pt-1"
          >
            <PixelIcon px={NAV_PX[label]} cell={5} gap={2} on={c.on} />
            <span className={`text-[12px] font-semibold leading-[14px] tracking-[0.3px] ${active ? 'text-ink' : 'text-muted'}`}>
              {label}
            </span>
          </button>
        )
      })}
    </div>
  )
}

export function MoreSheet({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  if (!open) return null
  const items = NAV_ITEMS.filter((i) => !['Chat', 'Files', 'Skills'].includes(i.label))
  return (
    <div className="fixed inset-0 z-40 bg-black/50 lg:hidden" onClick={onClose}>
      <div
        className="absolute bottom-0 w-full rounded-t-[12px] border-t border-line bg-panel p-4 pb-[calc(18px+env(safe-area-inset-bottom))]"
        onClick={(e) => e.stopPropagation()}
      >
        <SectionLabel className="px-2 pb-2">More</SectionLabel>
        {items.map((item) => (
          <button
            key={item.label}
            onClick={() => {
              navigate(item.path)
              onClose()
            }}
            className="flex w-full items-center gap-3 rounded-[3px] px-3 py-3 text-left hover:bg-raised2"
          >
            <PixelIcon px={NAV_PX[item.label]} on="rgb(var(--rb-soft))" />
            <span className="text-[13px] font-medium text-mist">{item.label}</span>
          </button>
        ))}
      </div>
    </div>
  )
}

// ── header (mobile top bar + bell) ───────────────────────────

export function MobileHeader({
  title,
  onBell,
  onMenu,
}: {
  title: string
  onBell: () => void
  onMenu?: () => void
}) {
  const unread = useChat((s) => s.unread)
  return (
    <div className="flex min-h-12 items-center gap-2 border-b border-line bg-panel px-3 pb-2 pt-[calc(8px+env(safe-area-inset-top))] lg:hidden">
      {onMenu && (
        <button
          type="button"
          onClick={onMenu}
          className="flex h-9 w-9 flex-none items-center justify-center rounded-[5px] border border-line bg-raised text-mist hover:border-line2 hover:text-ink"
          aria-label="Open chats"
        >
          <span className="flex w-[15px] flex-col gap-[3px]" aria-hidden="true">
            <span className="h-[1.5px] w-full bg-current" />
            <span className="h-[1.5px] w-full bg-current" />
            <span className="h-[1.5px] w-full bg-current" />
          </span>
        </button>
      )}
      <span className="text-[15px] font-bold text-ink">{title}</span>
      <ConnDot />
      <button
        onClick={onBell}
        className="relative ml-auto flex h-9 w-9 items-center justify-center rounded-[5px] border border-line bg-raised hover:border-line2"
        aria-label="Open activity"
      >
        <PixelIcon px={NAV_PX.Bell} cell={4} gap={1.5} on="rgb(var(--rb-mist))" />
        {unread > 0 && (
          <span className="absolute -right-1.5 -top-1.5 rounded-[3px] bg-acc px-[5px] py-px font-mono text-[9px] font-semibold text-onacc">
            {unread > 99 ? '99+' : unread}
          </span>
        )}
      </button>
    </div>
  )
}

export function DesktopBell({ onBell }: { onBell: () => void }) {
  const unread = useChat((s) => s.unread)
  return (
    <button
      onClick={onBell}
      className="relative hidden h-9 w-9 items-center justify-center rounded-[5px] border border-line bg-raised hover:border-line2 lg:flex"
      title="Activity"
      aria-label="Open activity"
    >
      <PixelIcon px={NAV_PX.Bell} cell={4} gap={1.5} on="rgb(var(--rb-mist))" />
      {unread > 0 && (
        <span className="absolute -right-1.5 -top-1.5 rounded-[3px] bg-acc px-[5px] py-px font-mono text-[9px] font-semibold text-onacc">
          {unread > 99 ? '99+' : unread}
        </span>
      )}
    </button>
  )
}

// ── notification center ──────────────────────────────────────

const KIND_FILTERS = ['all', 'cron', 'hook', 'heartbeat', 'agent', 'job', 'system'] as const

export function NotificationPanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [filter, setFilter] = useState<(typeof KIND_FILTERS)[number]>('all')
  const [expanded, setExpanded] = useState<string | null>(null)
  const qc = useQueryClient()
  const setUnread = useChat((s) => s.setUnread)

  const { data } = useQuery({
    queryKey: ['notifications', filter],
    queryFn: () =>
      api.get<{ items: Notification[]; unread: number }>(
        `/api/notifications?limit=50${filter !== 'all' ? `&kind=${filter}` : ''}`,
      ),
    enabled: open,
    refetchInterval: open ? 15000 : false,
  })
  const jobs = useQuery({
    queryKey: ['jobs'],
    queryFn: () => api.get<{ summary: string }>('/api/jobs'),
    enabled: open,
    refetchInterval: open ? 5000 : false,
  })

  if (!open) return null

  const markAll = async () => {
    await api.post('/api/notifications/read', { all: true })
    setUnread(0)
    qc.invalidateQueries({ queryKey: ['notifications'] })
  }

  const openItem = async (notification: Notification) => {
    setExpanded(expanded === notification.id ? null : notification.id)
    if (notification.read) return
    await api.post('/api/notifications/read', { ids: [notification.id] })
    setUnread(Math.max(0, useChat.getState().unread - 1))
    qc.invalidateQueries({ queryKey: ['notifications'] })
  }

  const jobsSummary = jobs.data?.summary?.trim()
  const showJobs = !!jobsSummary && jobsSummary !== 'No background jobs.'

  return (
    <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose}>
      <div
        className="absolute inset-x-0 top-0 max-h-[85vh] overflow-y-auto border-b border-line bg-panel pb-3 pt-safe lg:inset-x-auto lg:right-3 lg:top-[62px] lg:w-[400px] lg:rounded-[6px] lg:border"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3">
          <span className="text-[13.5px] font-semibold text-ink">Activity</span>
          <button onClick={markAll} className="font-mono text-[10px] text-acc hover:opacity-80">
            mark all read
          </button>
        </div>
        <div className="flex flex-wrap gap-1 px-4 pb-2">
          {KIND_FILTERS.map((k) => (
            <button
              key={k}
              onClick={() => setFilter(k)}
              className={`rounded-[2px] px-2 py-1 font-mono text-[9.5px] uppercase tracking-wider ${
                filter === k ? 'bg-acc text-onacc' : 'bg-raised2 text-soft'
              }`}
            >
              {k}
            </button>
          ))}
        </div>
        {showJobs && (
          <div className="mx-4 mb-3 rounded-[4px] border border-line bg-deep p-3">
            <div className="mb-1.5 font-mono text-[9px] uppercase tracking-wider text-acc">Background jobs</div>
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap font-mono text-[10px] leading-relaxed text-mist">
              {jobsSummary}
            </pre>
          </div>
        )}
        <div className="divide-y divide-[var(--rb-line)]">
          {(data?.items ?? []).map((n) => (
            <button
              key={n.id}
              onClick={() => openItem(n)}
              className="block w-full px-4 py-2.5 text-left hover:bg-raised/50"
            >
              <div className="flex items-center gap-2">
                {!n.read && <Dot color="acc" />}
                <span className="truncate text-[12px] font-medium text-mist">{n.title}</span>
                <span className="ml-auto flex items-center gap-2">
                  <StatusPill status={n.status} />
                  <span className="font-mono text-[9.5px] text-faint">
                    {new Date(n.ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                  </span>
                </span>
              </div>
              <div className="mt-0.5 font-mono text-[9.5px] uppercase text-faint">{n.kind}</div>
              {expanded === n.id && n.body && (
                <pre className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-[3px] bg-deep p-2 font-mono text-[10.5px] text-mist">
                  {n.body}
                </pre>
              )}
            </button>
          ))}
          {data && data.items.length === 0 && (
            <div className="px-4 py-8 text-center text-[12px] text-muted">Nothing here yet</div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── page container ───────────────────────────────────────────

export function Page({ title, children, actions }: { title: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex h-full min-h-0 min-w-0 max-w-full flex-1 flex-col overflow-hidden">
      <div className="hidden items-center gap-3 border-b border-line px-6 py-3.5 lg:flex lg:pr-[68px]">
        <span className="text-[14px] font-semibold text-ink">{title}</span>
        <div className="ml-auto flex items-center gap-2">{actions}</div>
      </div>
      <div className="min-h-0 min-w-0 max-w-full flex-1 overflow-x-hidden overflow-y-auto">{children}</div>
    </div>
  )
}
