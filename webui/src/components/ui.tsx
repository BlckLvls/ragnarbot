// UI primitives ported from the design's component sheet (card 1z).

import { ReactNode, useEffect } from 'react'

// ── buttons ──────────────────────────────────────────────────

type ButtonVariant = 'primary' | 'secondary' | 'destructive' | 'ghost'

const BTN: Record<ButtonVariant, string> = {
  primary: 'bg-acc text-onacc font-semibold hover:opacity-90',
  secondary: 'bg-raised2 text-mist font-medium hover:bg-raised',
  destructive: 'bg-err/10 text-err border border-err/30 font-medium hover:bg-err/20',
  ghost: 'text-soft hover:text-ink',
}

export function Button({
  variant = 'secondary',
  className = '',
  loading,
  children,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
  loading?: boolean
}) {
  return (
    <button
      className={`inline-flex items-center justify-center gap-2 rounded-[3px] px-[15px] py-2 text-[12px] leading-none transition-colors disabled:opacity-40 disabled:pointer-events-none ${BTN[variant]} ${className}`}
      disabled={loading || props.disabled}
      {...props}
    >
      {loading && (
        <span className="inline-flex gap-[2px]">
          {[0, 1, 2].map((i) => (
            <span key={i} className="h-[4px] w-[4px] bg-current animate-rb-pulse" style={{ animationDelay: `${i * 0.2}s` }} />
          ))}
        </span>
      )}
      {children}
    </button>
  )
}

// ── badges & pills ───────────────────────────────────────────

export function ReloadBadge({ reload }: { reload: string }) {
  const map: Record<string, [string, string]> = {
    hot: ['applies instantly', 'text-ok bg-ok/10'],
    warm: ['needs restart', 'text-warn bg-warn/10'],
    cold: ['needs re-onboard', 'text-err bg-err/10'],
  }
  const [label, cls] = map[reload] ?? [reload, 'text-soft bg-raised2']
  return (
    <span className={`font-mono text-[9.5px] rounded-[2px] px-2 py-[2.5px] whitespace-nowrap ${cls}`}>
      {label}
    </span>
  )
}

export function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    running: 'text-acc bg-acc/10',
    ok: 'text-ok bg-ok/10',
    completed: 'text-ok bg-ok/10',
    error: 'text-err bg-err/10',
    stopped: 'text-soft bg-raised2',
    skipped: 'text-soft bg-raised2',
  }
  return (
    <span className={`font-mono text-[9px] uppercase rounded-[2px] px-2 py-[2.5px] ${map[status] ?? 'text-soft bg-raised2'}`}>
      {status}
    </span>
  )
}

export function SourceBadge({ source }: { source: string }) {
  const map: Record<string, string> = {
    builtin: 'text-soft bg-raised2',
    workspace: 'text-acc bg-acc/[.13]',
    override: 'text-warn bg-warn/10',
  }
  return (
    <span className={`font-mono text-[8.5px] rounded-[2px] px-[7px] py-[2px] ${map[source] ?? 'text-soft bg-raised2'}`}>
      {source}
    </span>
  )
}

export function Dot({ color = 'ok', pulse = false }: { color?: 'ok' | 'warn' | 'err' | 'acc' | 'muted'; pulse?: boolean }) {
  const map = { ok: 'bg-ok', warn: 'bg-warn', err: 'bg-err', acc: 'bg-acc', muted: 'bg-muted' }
  return <span className={`inline-block h-[5px] w-[5px] ${map[color]} ${pulse ? 'animate-rb-pulse' : ''}`} />
}

// ── section label ────────────────────────────────────────────

export function SectionLabel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`rb-label ${className}`}>{children}</div>
}

// ── inputs ───────────────────────────────────────────────────

export function TextInput({
  error,
  className = '',
  ...props
}: React.InputHTMLAttributes<HTMLInputElement> & { error?: boolean }) {
  return (
    <input
      className={`w-full rounded-[3px] bg-raised px-3 py-[9px] text-[12.5px] text-ink placeholder:text-muted outline-none border ${
        error ? 'border-err' : 'border-line2 focus:border-acc/50'
      } ${className}`}
      {...props}
    />
  )
}

export function TextArea({
  className = '',
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={`w-full rounded-[3px] bg-raised px-3 py-[9px] text-[12.5px] text-ink placeholder:text-muted outline-none border border-line2 focus:border-acc/50 ${className}`}
      {...props}
    />
  )
}

export function Select({
  className = '',
  children,
  ...props
}: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`w-full rounded-[3px] bg-raised px-3 py-[9px] text-[12.5px] text-ink outline-none border border-line2 focus:border-acc/50 ${className}`}
      {...props}
    >
      {children}
    </select>
  )
}

export function FieldError({ children }: { children: ReactNode }) {
  return (
    <div className="mt-1 flex items-center gap-1.5 text-[11px] text-err">
      <span className="h-[4px] w-[4px] bg-err" />
      {children}
    </div>
  )
}

// ── toggle / stepper / segmented ─────────────────────────────

export function Toggle({ value, onChange, disabled }: { value: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!value)}
      disabled={disabled}
      className={`relative h-[20px] w-[34px] rounded-[2px] border transition-colors disabled:opacity-40 ${
        value ? 'bg-acc/[.13] border-acc/50' : 'bg-surface border-line2'
      }`}
    >
      <span
        className={`absolute top-[2px] h-[14px] w-[14px] transition-all ${
          value ? 'right-[2px] bg-acc' : 'left-[2px] bg-faint'
        }`}
      />
    </button>
  )
}

export function Stepper({
  value,
  onChange,
  min,
  max,
  step = 1,
}: {
  value: number
  onChange: (v: number) => void
  min?: number
  max?: number
  step?: number
}) {
  const clamp = (v: number) => Math.min(max ?? Infinity, Math.max(min ?? -Infinity, v))
  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        onClick={() => onChange(clamp(value - step))}
        className="h-[26px] w-[26px] rounded-[2px] bg-raised border border-line2 text-soft hover:text-ink"
      >
        −
      </button>
      <span className="font-mono text-[12px] text-ink min-w-[48px] text-center">{value}</span>
      <button
        type="button"
        onClick={() => onChange(clamp(value + step))}
        className="h-[26px] w-[26px] rounded-[2px] bg-raised border border-line2 text-soft hover:text-ink"
      >
        +
      </button>
    </span>
  )
}

export function Segmented<T extends string>({
  options,
  value,
  onChange,
  labels,
}: {
  options: readonly T[]
  value: T
  onChange: (v: T) => void
  labels?: Partial<Record<T, string>>
}) {
  return (
    <span className="inline-flex rounded-[3px] bg-surface p-[2px] border border-line">
      {options.map((o) => (
        <button
          key={o}
          type="button"
          onClick={() => onChange(o)}
          className={`rounded-[2px] px-2.5 py-1 text-[11px] font-medium transition-colors ${
            o === value ? 'bg-acc text-onacc' : 'text-soft hover:text-ink'
          }`}
        >
          {labels?.[o] ?? o}
        </button>
      ))}
    </span>
  )
}

// ── cards / layout ───────────────────────────────────────────

export function Card({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-[4px] bg-raised border border-line px-[11px] py-[10px] ${className}`}>
      {children}
    </div>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`h-3 rounded-[2px] bg-raised2 animate-rb-skeleton ${className}`} />
}

export function EmptyState({
  title,
  action,
  className = '',
}: {
  title: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div className={`flex flex-col items-center justify-center gap-3 rounded-[4px] border border-dashed border-line2 py-10 px-4 text-center ${className}`}>
      <span className="grid grid-cols-3 gap-[2px]">
        {[1, 0, 1, 0, 1, 0, 1, 0, 1].map((v, i) => (
          <span key={i} className={`h-[4px] w-[4px] ${v ? 'bg-faint' : ''}`} />
        ))}
      </span>
      <div className="text-[12px] text-muted">{title}</div>
      {action}
    </div>
  )
}

// ── dialogs & toasts ─────────────────────────────────────────

export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'Confirm',
  destructive,
  onConfirm,
  onCancel,
}: {
  open: boolean
  title: string
  body?: ReactNode
  confirmLabel?: string
  destructive?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onCancel}>
      <div
        className="w-full max-w-sm rounded-[6px] bg-raised border border-line2 p-4 shadow-[0_8px_32px_rgba(0,0,0,.5)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="text-[13.5px] font-semibold text-ink">{title}</div>
        {body && <div className="mt-1.5 text-[11.5px] text-soft">{body}</div>}
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel}>
            Cancel
          </Button>
          <Button variant={destructive ? 'destructive' : 'primary'} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </div>
  )
}

export function Toast({ text, onDone }: { text: string; onDone: () => void }) {
  useEffect(() => {
    const t = setTimeout(onDone, 3500)
    return () => clearTimeout(t)
  }, [onDone])
  return (
    <div className="fixed bottom-20 left-1/2 z-50 -translate-x-1/2 lg:bottom-6">
      <div className="flex items-center gap-2 rounded-[4px] bg-raised2 border border-line2 px-3 py-2 text-[12px] text-ink shadow-[0_4px_16px_rgba(0,0,0,.4)]">
        <Dot color="acc" />
        {text}
      </div>
    </div>
  )
}

// Bottom sheet (mobile) / side panel (desktop)
export function Sheet({
  open,
  onClose,
  title,
  children,
  side = false,
}: {
  open: boolean
  onClose: () => void
  title?: ReactNode
  children: ReactNode
  side?: boolean
}) {
  if (!open) return null
  return (
    <div className="fixed inset-0 z-40 flex bg-black/50" onClick={onClose}>
      <div
        className={
          side
            ? 'ml-auto h-full w-full max-w-md overflow-y-auto bg-panel border-l border-line p-4'
            : 'mt-auto max-h-[85vh] w-full overflow-y-auto rounded-t-[10px] bg-panel border-t border-line p-4 pb-safe lg:m-auto lg:max-w-lg lg:rounded-[10px] lg:border'
        }
        onClick={(e) => e.stopPropagation()}
      >
        {title && (
          <div className="mb-3 flex items-center justify-between">
            <div className="text-[13.5px] font-semibold text-ink">{title}</div>
            <button onClick={onClose} className="text-muted hover:text-ink text-[16px] leading-none px-1">
              ×
            </button>
          </div>
        )}
        {children}
      </div>
    </div>
  )
}
