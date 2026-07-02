/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: ['class'],
  theme: {
    extend: {
      colors: {
        // ragnarbot design tokens; values live in CSS vars (dark default, light override)
        page: 'rgb(var(--rb-page) / <alpha-value>)',
        surface: 'rgb(var(--rb-surface) / <alpha-value>)',
        panel: 'rgb(var(--rb-panel) / <alpha-value>)',
        inset: 'rgb(var(--rb-inset) / <alpha-value>)',
        deep: 'rgb(var(--rb-deep) / <alpha-value>)',
        raised: 'rgb(var(--rb-raised) / <alpha-value>)',
        raised2: 'rgb(var(--rb-raised2) / <alpha-value>)',
        ink: 'rgb(var(--rb-ink) / <alpha-value>)',
        body: 'rgb(var(--rb-body) / <alpha-value>)',
        mist: 'rgb(var(--rb-mist) / <alpha-value>)',
        soft: 'rgb(var(--rb-soft) / <alpha-value>)',
        muted: 'rgb(var(--rb-muted) / <alpha-value>)',
        faint: 'rgb(var(--rb-faint) / <alpha-value>)',
        acc: 'rgb(var(--rb-acc) / <alpha-value>)',
        onacc: 'rgb(var(--rb-onacc) / <alpha-value>)',
        ok: 'rgb(var(--rb-ok) / <alpha-value>)',
        warn: 'rgb(var(--rb-warn) / <alpha-value>)',
        err: 'rgb(var(--rb-err) / <alpha-value>)',
      },
      borderColor: {
        line: 'var(--rb-line)',
        line2: 'var(--rb-line2)',
      },
      fontFamily: {
        sans: ['"Schibsted Grotesk"', 'ui-sans-serif', 'sans-serif'],
        mono: ['"IBM Plex Mono"', 'ui-monospace', 'monospace'],
      },
      keyframes: {
        'rb-blink': { '0%,55%': { opacity: '1' }, '56%,100%': { opacity: '.12' } },
        'rb-pulse': { '0%,100%': { opacity: '1' }, '50%': { opacity: '.3' } },
      },
      animation: {
        'rb-blink': 'rb-blink 1s steps(1) infinite',
        'rb-pulse': 'rb-pulse 1s infinite',
        'rb-skeleton': 'rb-pulse 1.6s infinite',
      },
    },
  },
  plugins: [],
}
