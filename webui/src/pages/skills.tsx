// Skills: card grid with source/always badges + detail/edit side panel + new-from-template.

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, ApiError, SkillSummary } from '../lib/api'
import { Page } from '../app/shell'
import { Markdown } from '../components/markdown'
import { Button, Card, EmptyState, Sheet, Skeleton, SourceBadge, TextInput, Toast } from '../components/ui'

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/
const VIEW_MODES = ['view', 'edit'] as const

function isAlways(v: SkillSummary['always']): boolean {
  return v === true || (typeof v === 'string' && v !== '' && v !== 'false')
}

function template(name: string): string {
  const title = name.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
  return `---
name: ${name}
description: Describe when to use this skill and what it does.
metadata: {"ragnarbot":{"emoji":"✨"}}
---

# ${title}

Describe the skill's purpose here.

## When to Use

- ...

## Steps

1. ...
`
}

// ── detail / edit panel ──────────────────────────────────────

function SkillDetail({ name, onClose, onSaved }: { name: string; onClose: () => void; onSaved: () => void }) {
  const [mode, setMode] = useState<(typeof VIEW_MODES)[number]>('view')
  const [content, setContent] = useState('')
  const [dirty, setDirty] = useState(false)

  const detail = useQuery({
    queryKey: ['skill', name],
    queryFn: () => api.get<{ name: string; content: string }>(`/api/skills/${encodeURIComponent(name)}`),
    retry: false,
  })

  useEffect(() => {
    if (detail.data) {
      setContent(detail.data.content)
      setDirty(false)
    }
  }, [detail.data])

  const save = useMutation({
    mutationFn: () => api.put(`/api/skills/${encodeURIComponent(name)}`, { content }),
    onSuccess: () => {
      setDirty(false)
      onSaved()
    },
  })

  return (
    <Sheet open onClose={onClose} title={name} side>
      <div className="mb-3 flex items-center gap-2">
        <span className="inline-flex rounded-[3px] bg-surface p-[2px] border border-line">
          {VIEW_MODES.map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`rounded-[2px] px-2.5 py-1 text-[11px] font-medium ${
                m === mode ? 'bg-acc text-onacc' : 'text-soft hover:text-ink'
              }`}
            >
              {m}
            </button>
          ))}
        </span>
        {dirty && <span className="h-[5px] w-[5px] bg-acc" title="unsaved" />}
        {mode === 'edit' && (
          <Button
            variant="primary"
            className="ml-auto"
            onClick={() => save.mutate()}
            loading={save.isPending}
            disabled={!dirty}
          >
            Save
          </Button>
        )}
      </div>

      {mode === 'edit' && (
        <div className="mb-2 text-[10.5px] text-muted">
          Saving a builtin skill creates a workspace override.
        </div>
      )}

      {detail.isLoading ? (
        <div className="space-y-2">
          <Skeleton className="w-3/4" />
          <Skeleton className="w-full" />
          <Skeleton className="w-1/2" />
        </div>
      ) : detail.isError ? (
        <div className="rounded-[4px] border border-err/30 bg-err/10 px-3 py-2 text-[11.5px] text-err">
          {(detail.error as ApiError)?.message ?? 'could not load skill'}
        </div>
      ) : mode === 'edit' ? (
        <textarea
          value={content}
          onChange={(e) => {
            setContent(e.target.value)
            setDirty(true)
          }}
          spellCheck={false}
          className="min-h-[420px] w-full resize-none rounded-[3px] border border-line2 bg-raised px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-ink outline-none focus:border-acc/50"
        />
      ) : (
        <Markdown>{content}</Markdown>
      )}
      {save.isError && (
        <div className="mt-2 text-[11px] text-err">{(save.error as ApiError)?.message ?? 'save failed'}</div>
      )}
    </Sheet>
  )
}

// ── new skill sheet ──────────────────────────────────────────

function NewSkillSheet({
  onClose,
  onCreated,
}: {
  onClose: () => void
  onCreated: (name: string) => void
}) {
  const [name, setName] = useState('')
  const valid = NAME_RE.test(name)

  const create = useMutation({
    mutationFn: () => api.put(`/api/skills/${encodeURIComponent(name)}`, { content: template(name) }),
    onSuccess: () => onCreated(name),
  })

  return (
    <Sheet open onClose={onClose} title="New skill">
      <div className="space-y-3">
        <div>
          <TextInput
            autoFocus
            value={name}
            placeholder="skill-name"
            error={name !== '' && !valid}
            onChange={(e) => setName(e.target.value)}
            className="font-mono"
          />
          <div className="mt-1.5 text-[10.5px] text-muted">
            Lowercase, kebab-case (a–z, 0–9, hyphen).
          </div>
        </div>
        <div>
          <div className="rb-label mb-1">Template</div>
          <pre className="max-h-52 overflow-auto rounded-[3px] bg-deep p-3 font-mono text-[10.5px] leading-relaxed text-mist">
            {valid ? template(name) : template('skill-name')}
          </pre>
        </div>
        {create.isError && (
          <div className="text-[11px] text-err">{(create.error as ApiError)?.message ?? 'create failed'}</div>
        )}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={() => create.mutate()} disabled={!valid} loading={create.isPending}>
            Create
          </Button>
        </div>
      </div>
    </Sheet>
  )
}

// ── card ─────────────────────────────────────────────────────

function SkillCard({ s, onClick }: { s: SkillSummary; onClick: () => void }) {
  return (
    <button onClick={onClick} className="text-left">
      <Card className="flex h-full flex-col gap-2 transition-colors hover:border-line2">
        <div className="flex items-center gap-2">
          <span className="text-[13.5px] font-semibold text-ink">{s.name}</span>
          {isAlways(s.always) && (
            <span className="ml-auto rounded-[2px] bg-acc/[.13] px-[7px] py-[2px] font-mono text-[8.5px] text-acc">
              always
            </span>
          )}
        </div>
        {s.description && (
          <span className="line-clamp-2 text-[11.5px] leading-relaxed text-soft">{s.description}</span>
        )}
        <div className="mt-auto pt-1">{s.source && <SourceBadge source={s.source} />}</div>
      </Card>
    </button>
  )
}

// ── page ─────────────────────────────────────────────────────

export default function SkillsPage() {
  const qc = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [showNew, setShowNew] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  const skills = useQuery({
    queryKey: ['skills'],
    queryFn: () => api.get<SkillSummary[]>('/api/skills'),
  })

  const refresh = () => qc.invalidateQueries({ queryKey: ['skills'] })

  return (
    <Page title="Skills">
      <div className="p-4 lg:p-6">
        <div className="mb-4 flex items-center gap-3">
          <span className="font-mono text-[10px] text-muted">
            {skills.data ? `${skills.data.length} loaded` : '…'}
          </span>
          <Button variant="primary" className="ml-auto" onClick={() => setShowNew(true)}>
            + New skill
          </Button>
        </div>

        {skills.isLoading ? (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {[0, 1, 2, 3, 4, 5].map((i) => (
              <Card key={i} className="space-y-2">
                <Skeleton className="w-1/2" />
                <Skeleton className="w-3/4" />
              </Card>
            ))}
          </div>
        ) : !skills.data || skills.data.length === 0 ? (
          <EmptyState title="No skills yet" action={<Button onClick={() => setShowNew(true)}>+ New skill</Button>} />
        ) : (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {skills.data.map((s) => (
              <SkillCard key={s.name} s={s} onClick={() => setSelected(s.name)} />
            ))}
          </div>
        )}
      </div>

      {selected && (
        <SkillDetail
          key={selected}
          name={selected}
          onClose={() => setSelected(null)}
          onSaved={() => {
            setToast('Saved')
            refresh()
          }}
        />
      )}

      {showNew && (
        <NewSkillSheet
          onClose={() => setShowNew(false)}
          onCreated={(name) => {
            setShowNew(false)
            setToast('Skill created')
            refresh()
            setSelected(name)
          }}
        />
      )}

      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </Page>
  )
}
