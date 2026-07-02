import { Suspense, lazy, useEffect, useState } from 'react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { connectWs, useChat } from './lib/ws'
import { initTheme } from './app/theme'
import {
  DesktopBell,
  MobileHeader,
  MoreSheet,
  NAV_ITEMS,
  NotificationPanel,
  Sidebar,
  TabBar,
} from './app/shell'
import { Skeleton, Toast } from './components/ui'
import { api } from './lib/api'

const ChatPage = lazy(() => import('./pages/chat'))
const SessionsPage = lazy(() => import('./pages/sessions'))
const SettingsPage = lazy(() => import('./pages/settings'))
const CronPage = lazy(() => import('./pages/cron'))
const HooksPage = lazy(() => import('./pages/hooks'))
const AgentsPage = lazy(() => import('./pages/agents'))
const MemoryPage = lazy(() => import('./pages/memory'))
const SkillsPage = lazy(() => import('./pages/skills'))
const StatusPage = lazy(() => import('./pages/status'))

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 5000 } },
})

function Fallback() {
  return (
    <div className="space-y-3 p-6">
      <Skeleton className="w-2/3" />
      <Skeleton className="w-1/2" />
      <Skeleton className="w-3/4" />
    </div>
  )
}

export default function App() {
  const [more, setMore] = useState(false)
  const [bell, setBell] = useState(false)
  const location = useLocation()
  const toast = useChat((s) => s.toast)
  const setToast = useChat((s) => s.setToast)
  const setUnread = useChat((s) => s.setUnread)
  const [version, setVersion] = useState('')

  useEffect(() => {
    initTheme()
    connectWs()
    api
      .get<{ version: string }>('/api/status')
      .then((d) => setVersion(d.version))
      .catch(() => {})
    api
      .get<{ unread: number }>('/api/notifications?limit=1')
      .then((d) => setUnread(d.unread))
      .catch(() => {})
  }, [setUnread])

  const current =
    NAV_ITEMS.find((i) =>
      i.path === '/' ? location.pathname === '/' : location.pathname.startsWith(i.path),
    )?.label ?? 'ragnarbot'

  return (
    <QueryClientProvider client={queryClient}>
      <div className="flex h-dvh flex-col lg:flex-row">
        <Sidebar version={version} />
        <MobileHeader title={current} onBell={() => setBell(true)} />
        <div className="relative flex min-h-0 flex-1 flex-col">
          <div className="absolute right-4 top-3 z-10 hidden lg:block">
            <DesktopBell onBell={() => setBell(true)} />
          </div>
          <Suspense fallback={<Fallback />}>
            <Routes>
              <Route path="/" element={<ChatPage />} />
              <Route path="/sessions" element={<SessionsPage />} />
              <Route path="/settings/*" element={<SettingsPage />} />
              <Route path="/cron" element={<CronPage />} />
              <Route path="/hooks" element={<HooksPage />} />
              <Route path="/agents" element={<AgentsPage />} />
              <Route path="/memory" element={<MemoryPage />} />
              <Route path="/skills" element={<SkillsPage />} />
              <Route path="/status" element={<StatusPage />} />
            </Routes>
          </Suspense>
        </div>
        <TabBar onMore={() => setMore(true)} />
        <MoreSheet open={more} onClose={() => setMore(false)} />
        <NotificationPanel open={bell} onClose={() => setBell(false)} />
        {toast && <Toast text={toast} onDone={() => setToast(null)} />}
      </div>
    </QueryClientProvider>
  )
}
