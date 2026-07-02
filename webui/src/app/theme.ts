// Theme + accent preferences (client-side, localStorage).

export type Theme = 'dark' | 'light'
export type Accent = 'amber' | 'cyan' | 'bone' | 'ember'

export function applyTheme(theme: Theme, accent: Accent) {
  const root = document.documentElement
  root.classList.toggle('light', theme === 'light')
  if (accent === 'amber') root.removeAttribute('data-accent')
  else root.setAttribute('data-accent', accent)
  localStorage.setItem('rb-theme', theme)
  localStorage.setItem('rb-accent', accent)
}

export function loadTheme(): { theme: Theme; accent: Accent } {
  const theme = (localStorage.getItem('rb-theme') as Theme) || 'dark'
  const accent = (localStorage.getItem('rb-accent') as Accent) || 'amber'
  return { theme, accent }
}

export function initTheme() {
  const { theme, accent } = loadTheme()
  applyTheme(theme, accent)
}
