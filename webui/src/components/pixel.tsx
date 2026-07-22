// Pixel-grid brand system: nav icons, wordmark, context meter, waveform, stream dots.
// Ported from the Claude Design prototype (console.dc.html).

import { useEffect, useRef } from 'react'

// 3×3 nav icon bitmaps from the design's Sidebar/TabBar components
export const NAV_PX: Record<string, number[]> = {
  Chat: [1, 1, 1, 1, 1, 1, 0, 1, 0],
  Cron: [0, 1, 0, 1, 1, 1, 0, 1, 0],
  Agents: [1, 0, 1, 1, 1, 1, 1, 0, 1],
  Files: [1, 1, 1, 1, 0, 1, 1, 1, 1],
  Skills: [0, 1, 0, 1, 1, 1, 1, 0, 1],
  Models: [1, 1, 0, 0, 1, 1, 1, 1, 0],
  Settings: [1, 0, 1, 0, 1, 0, 1, 0, 1],
  More: [0, 0, 0, 1, 1, 1, 0, 0, 0],
  Bell: [0, 1, 0, 1, 1, 1, 1, 1, 1],
}

export const CHAT_ACTION_PX = {
  add: [
    0, 0, 1, 0, 0,
    0, 0, 1, 0, 0,
    1, 1, 1, 1, 1,
    0, 0, 1, 0, 0,
    0, 0, 1, 0, 0,
  ],
  microphone: [
    0, 1, 1, 1, 0,
    0, 1, 0, 1, 0,
    0, 1, 0, 1, 0,
    0, 1, 0, 1, 0,
    1, 0, 0, 0, 1,
    0, 1, 1, 1, 0,
    0, 0, 1, 0, 0,
  ],
  send: [
    0, 0, 1, 0, 0,
    0, 0, 0, 1, 0,
    1, 1, 1, 1, 1,
    0, 0, 0, 1, 0,
    0, 0, 1, 0, 0,
  ],
  down: [
    0, 0, 1, 0, 0,
    0, 0, 1, 0, 0,
    1, 0, 1, 0, 1,
    0, 1, 1, 1, 0,
    0, 0, 1, 0, 0,
  ],
  stop: [
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
    1, 1, 1, 1, 1,
  ],
  followup: [
    1, 0, 0, 0, 0, 0, 0,
    1, 0, 0, 0, 0, 0, 0,
    1, 0, 0, 0, 1, 0, 0,
    1, 0, 0, 0, 1, 1, 0,
    1, 1, 1, 1, 1, 1, 1,
    0, 0, 0, 0, 1, 1, 0,
    0, 0, 0, 0, 1, 0, 0,
  ],
} as const

export function PixelIcon({
  px,
  cell = 3,
  gap = 1.5,
  cols = 3,
  on,
  off = 'transparent',
}: {
  px: readonly number[]
  cell?: number
  gap?: number
  cols?: number
  on: string
  off?: string
}) {
  return (
    <span
      style={{
        display: 'grid',
        gridTemplateColumns: `repeat(${cols}, ${cell}px)`,
        gridAutoRows: `${cell}px`,
        gap: `${gap}px`,
      }}
    >
      {px.map((v, i) => (
        <span key={i} style={{ width: cell, height: cell, background: v ? on : off }} />
      ))}
    </span>
  )
}

// 9-row × 4-col letter bitmaps from the design
const RB_GLYPHS: Record<string, string[]> = {
  r: ['....', '....', 'X.XX', 'XX..', 'X...', 'X...', 'X...', '....', '....'],
  a: ['....', '....', '.XX.', '...X', '.XXX', 'X..X', '.XXX', '....', '....'],
  g: ['....', '....', '.XXX', 'X..X', 'X..X', '.XXX', '...X', '...X', '.XX.'],
  n: ['....', '....', 'XXX.', 'X..X', 'X..X', 'X..X', 'X..X', '....', '....'],
  b: ['X...', 'X...', 'XXX.', 'X..X', 'X..X', 'X..X', 'XXX.', '....', '....'],
  o: ['....', '....', '.XX.', 'X..X', 'X..X', 'X..X', '.XX.', '....', '....'],
  t: ['.X..', '.X..', 'XXX.', '.X..', '.X..', '.X..', '.XX.', '....', '....'],
}

function cssVar(name: string, fallback: string): string {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    if (!v) return fallback
    return v.includes(' ') ? `rgb(${v})` : v
  } catch {
    return fallback
  }
}

export function PixelWordmark({
  w = 460,
  h = 84,
  cell = 6,
  gap = 2,
  text = 'ragnarbot',
  motion = true,
}: {
  w?: number
  h?: number
  cell?: number
  gap?: number
  text?: string
  motion?: boolean
}) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const pitch = cell + gap
    const glyphs = [...text].map((c) => RB_GLYPHS[c]).filter(Boolean)
    const cols = glyphs.length * 5 - 1
    const x0 = Math.max(0, (w - cols * pitch) / 2)
    const y0 = Math.max(0, (h - 9 * pitch) / 2)
    const cells: { x: number; y: number; t: number; f: number }[] = []
    let cx = 0
    for (const G of glyphs) {
      for (let r = 0; r < 9; r++)
        for (let c = 0; c < 4; c++)
          if (G[r][c] === 'X')
            cells.push({ x: x0 + (cx + c) * pitch, y: y0 + r * pitch, t: Math.random(), f: Math.random() })
      cx += 5
    }
    const t0 = performance.now()
    const draw = (now: number) => {
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      const acc = cssVar('--rb-acc', '#FFB248')
      const isLight = document.documentElement.classList.contains('light')
      const base = isLight ? '#26272B' : '#E9E6DF'
      const T = (now - t0) / 1000
      const cyc = motion ? T % 7 : 999
      const p = motion ? Math.min(1.01, cyc / 2.2) : 1.01
      let sweepX = -100
      if (motion && cyc > 4.8 && cyc < 5.9) sweepX = ((cyc - 4.8) / 1.1) * (w + 80) - 40
      for (const cl of cells) {
        if (cl.t > p) continue
        let col = base
        let a = 1
        if (p < 1 && cl.t > p - 0.14) col = acc
        if (motion && cl.f > 0.93 && Math.sin(T * 3 + cl.x * 0.7) > 0.92) a = 0.3
        if (Math.abs(cl.x - sweepX) < 24) {
          col = acc
          a = 1
        }
        ctx.globalAlpha = a
        ctx.fillStyle = col
        ctx.fillRect(cl.x * 2, cl.y * 2, cell * 2, cell * 2)
      }
      ctx.globalAlpha = 1
    }
    draw(performance.now())
    const iv = setInterval(() => draw(performance.now()), 50)
    return () => clearInterval(iv)
  }, [w, h, cell, gap, text, motion])

  return <canvas ref={ref} width={w * 2} height={h * 2} style={{ width: w, maxWidth: '100%', height: 'auto' }} />
}

// Pixel context meter: N blocks, filled by percent; color shifts at thresholds.
export function ContextMeter({
  percent,
  mode,
  detail,
  blocks = 12,
  className = '',
}: {
  percent: number
  mode?: string
  detail?: string
  blocks?: number
  className?: string
}) {
  const safePercent = Math.min(100, Math.max(0, percent))
  const filled = Math.round((safePercent / 100) * blocks)
  const color = percent >= 90 ? 'bg-err' : percent >= 65 ? 'bg-warn' : 'bg-acc'
  const label = `Context ${mode ? `${mode}, ` : ''}${safePercent.toFixed(0)} percent${detail ? `, ${detail}` : ''}`
  return (
    <span
      role="status"
      aria-label={label}
      data-testid="context-meter"
      className={`inline-flex min-h-9 items-center gap-2 rounded-[5px] border border-line bg-raised px-2.5 py-1 ${className}`}
    >
      <span className="flex gap-[2px]" aria-hidden="true">
        {Array.from({ length: blocks }, (_, i) => (
          <span
            key={i}
            className={`h-[10px] w-[4px] ${i < filled ? color : 'bg-raised2'}`}
          />
        ))}
      </span>
      <span className="min-w-[30px] text-right font-mono text-[11px] font-semibold text-mist">
        {safePercent.toFixed(0)}%
      </span>
    </span>
  )
}

// "Agent working" indicator — three pulsing pixel dots
export function StreamDots({ className = '' }: { className?: string }) {
  return (
    <span className={`inline-flex gap-[3px] ${className}`}>
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="h-[5px] w-[5px] bg-acc animate-rb-pulse"
          style={{ animationDelay: `${i * 0.2}s` }}
        />
      ))}
    </span>
  )
}

// Live voice-capture waveform (canvas, driven by an AnalyserNode when provided)
export function Waveform({
  analyser,
  w = 200,
  h = 28,
}: {
  analyser: AnalyserNode | null
  w?: number
  h?: number
}) {
  const ref = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = ref.current
    if (!canvas) return
    const bars = 32
    const data = analyser ? new Uint8Array(analyser.frequencyBinCount) : null
    let raf = 0
    const draw = () => {
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      const acc = cssVar('--rb-acc', '#FFB248')
      ctx.fillStyle = acc
      for (let i = 0; i < bars; i++) {
        let v = 0.15
        if (analyser && data) {
          analyser.getByteFrequencyData(data)
          v = Math.max(0.1, data[Math.floor((i / bars) * data.length)] / 255)
        } else {
          v = 0.15 + 0.5 * Math.abs(Math.sin(performance.now() / 300 + i))
        }
        const bh = Math.max(2, v * h)
        const bw = (w / bars) * 0.55
        ctx.fillRect(i * (w / bars) * 2, (h - bh) * 2, bw * 2, bh * 2)
      }
      raf = requestAnimationFrame(draw)
    }
    draw()
    return () => cancelAnimationFrame(raf)
  }, [analyser, w, h])
  return <canvas ref={ref} width={w * 2} height={h * 2} style={{ width: w, height: h }} />
}

// Blinking caret for streaming text
export function Caret() {
  return <span className="inline-block h-[14px] w-[6px] translate-y-[2px] bg-acc animate-rb-blink" />
}
