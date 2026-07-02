// App shell: desktop sidebar + mobile tab bar + notification center.
// Nav order is a hard product requirement: Chat, Sessions, Settings first —
// chat switching and settings are the most-used destinations.

import { ReactNode, useState } from 'react'
import { NavLink, useLocation, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api, Notification } from '../lib/api'
import { useChat } from '../lib/ws'
import { NAV_PX, PixelIcon } from '../components/pixel'
import { Dot, SectionLabel, StatusPill } from '../components/ui'

export const NAV_ITEMS = [
  { label: 'Chat', path: '/' },
  { label: 'Sessions', path: '/sessions' },
  { label: 'Settings', path: '/settings' },
  { label: 'Cron', path: '/cron' },
  { label: 'Hooks', path: '/hooks' },
  { label: 'Agents', path: '/agents' },
  { label: 'Memory', path: '/memory' },
  { label: 'Skills', path: '/skills' },
  { label: 'Status', path: '/status' },
] as const

const TAB_ITEMS = ['Chat', 'Sessions', 'Settings', 'More'] as const

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
      <DaemonFooter />
    </div>
  )
}

function DaemonFooter() {
  const { data } = useQuery({
    queryKey: ['status-full'],
    queryFn: () => api.get<{ daemon: { status: string } }>('/api/status/full'),
    refetchInterval: 60000,
  })
  const status = data?.daemon?.status ?? '…'
  return (
    <div className="mt-auto flex items-center gap-2 border-t border-line px-4 py-[13px]">
      <Dot color={status === 'running' ? 'ok' : 'muted'} />
      <span className="font-mono text-[10px] text-muted">daemon {status}</span>
    </div>
  )
}

// ── mobile tab bar ───────────────────────────────────────────

export function TabBar({ onMore }: { onMore: () => void }) {
  const location = useLocation()
  const navigate = useNavigate()
  const mainPaths: Record<string, string> = { Chat: '/', Sessions: '/sessions', Settings: '/settings' }
  const morePaths = ['/cron', '/hooks', '/agents', '/memory', '/skills', '/status']

  return (
    <div className="flex h-[74px] min-h-[74px] items-start border-t border-line bg-panel px-2 pt-2 pb-safe lg:hidden">
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
            className="flex flex-1 flex-col items-center gap-[5px] py-1.5"
          >
            <PixelIcon px={NAV_PX[label]} cell={4} gap={2} on={c.on} />
            <span className={`text-[10px] font-semibold tracking-[0.3px] ${active ? 'text-ink' : 'text-muted'}`}>
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
  const items = NAV_ITEMS.filter((i) => !['Chat', 'Sessions', 'Settings'].includes(i.label))
  return (
    <div className="fixed inset-0 z-40 bg-black/50 lg:hidden" onClick={onClose}>
      <div
        className="absolute bottom-0 w-full rounded-t-[10px] border-t border-line bg-panel p-3 pb-safe"
        onClick={(e) => e.stopPropagation()}
      >
        <SectionLabel className="px-2 pb-2">Modules</SectionLabel>
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

export function MobileHeader({ title, onBell }: { title: string; onBell: () => void }) {
  const unread = useChat((s) => s.unread)
  return (
    <div className="flex items-center gap-2 border-b border-line bg-panel px-4 py-3 pt-safe lg:hidden">
      <span className="text-[14px] font-bold text-ink">{title}</span>
      <ConnDot />
      <button onClick={onBell} className="relative ml-auto p-1">
        <PixelIcon px={NAV_PX.Bell} cell={4} gap={2} on="rgb(var(--rb-soft))" />
        {unread > 0 && (
          <span className="absolute -right-1 -top-1 rounded-[2px] bg-acc px-[4px] font-mono text-[8.5px] font-semibold text-onacc">
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
    <button onClick={onBell} className="relative hidden p-1.5 lg:block" title="Notifications">
      <PixelIcon px={NAV_PX.Bell} cell={4} gap={2} on="rgb(var(--rb-soft))" />
      {unread > 0 && (
        <span className="absolute -right-1 -top-1 rounded-[2px] bg-acc px-[4px] font-mono text-[8.5px] font-semibold text-onacc">
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

  if (!open) return null

  const markAll = async () => {
    await api.post('/api/notifications/read', { all: true })
    setUnread(0)
    qc.invalidateQueries({ queryKey: ['notifications'] })
  }

  return (
    <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose}>
      <div
        className="absolute inset-x-0 top-0 max-h-[85vh] overflow-y-auto border-b border-line bg-panel pb-3 pt-safe lg:inset-x-auto lg:right-4 lg:top-14 lg:w-[400px] lg:rounded-[6px] lg:border"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3">
          <span className="text-[13.5px] font-semibold text-ink">Notifications</span>
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
        <div className="divide-y divide-[var(--rb-line)]">
          {(data?.items ?? []).map((n) => (
            <button
              key={n.id}
              onClick={() => setExpanded(expanded === n.id ? null : n.id)}
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
    <div className="flex h-full min-h-0 flex-1 flex-col">
      <div className="hidden items-center gap-3 border-b border-line px-6 py-3.5 lg:flex">
        <span className="text-[14px] font-semibold text-ink">{title}</span>
        <div className="ml-auto flex items-center gap-2">{actions}</div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </div>
  )
}
