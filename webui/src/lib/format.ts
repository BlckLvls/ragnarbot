// Shared display formatters — one source of truth for tokens and times.

export function fmtTokens(n: number): string {
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}k`
  return String(n)
}

export function relPast(iso: string | null | undefined): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  const today = new Date()
  const yst = new Date(Date.now() - 86_400_000)
  if (d.toDateString() === today.toDateString()) return `today ${t}`
  if (d.toDateString() === yst.toDateString()) return `yesterday ${t}`
  return `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${t}`
}

export function relFuture(ms: number | null | undefined): string {
  if (ms == null) return '—'
  const d = ms - Date.now()
  if (d <= 0) return 'due'
  const m = Math.floor(d / 60_000)
  const h = Math.floor(m / 60)
  const days = Math.floor(h / 24)
  if (days > 0) return `in ${days}d ${h % 24}h`
  if (h > 0) return `in ${h}h ${m % 60}m`
  return `in ${m}m`
}
