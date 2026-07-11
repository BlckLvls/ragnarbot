// Theme + accent preferences (client-side, localStorage).

export type Theme = 'dark' | 'light'
export const ACCENTS = ['cyan', 'violet', 'lime', 'ember', 'amber', 'bone'] as const
export type Accent = (typeof ACCENTS)[number]

export function applyTheme(theme: Theme, accent: Accent) {
  const root = document.documentElement
  root.classList.toggle('light', theme === 'light')
  root.style.colorScheme = theme
  document
    .querySelector<HTMLMetaElement>('meta[name="theme-color"]')
    ?.setAttribute('content', theme === 'light' ? '#ECE9E2' : '#101116')
  if (accent === 'cyan') root.removeAttribute('data-accent')
  else root.setAttribute('data-accent', accent)
  localStorage.setItem('rb-theme', theme)
  localStorage.setItem('rb-accent', accent)
}

export function loadTheme(): { theme: Theme; accent: Accent } {
  const theme = (localStorage.getItem('rb-theme') as Theme) || 'dark'
  const storedAccent = localStorage.getItem('rb-accent') as Accent | null
  const accent = storedAccent && ACCENTS.includes(storedAccent) ? storedAccent : 'cyan'
  return { theme, accent }
}

export function initTheme() {
  const { theme, accent } = loadTheme()
  applyTheme(theme, accent)
}
