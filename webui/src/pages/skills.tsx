// Skills: workspace-only catalog. Editing is delegated to the shared Files editor.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { api, ApiError, SkillSummary } from '../lib/api'
import { Page } from '../app/shell'
import { Button, Card, EmptyState, Sheet, Skeleton, TextInput, Toast } from '../components/ui'

const NAME_RE = /^[a-z0-9][a-z0-9-]*$/

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
          <div className="mt-1.5 text-[10.5px] text-muted">Lowercase, kebab-case (a–z, 0–9, hyphen).</div>
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
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={() => create.mutate()} disabled={!valid} loading={create.isPending}>
            Create
          </Button>
        </div>
      </div>
    </Sheet>
  )
}

function SkillCard({ skill, onClick }: { skill: SkillSummary; onClick: () => void }) {
  return (
    <button onClick={onClick} className="text-left">
      <Card className="flex h-full min-h-[112px] flex-col gap-2 transition-colors hover:border-line2">
        <span className="text-[13.5px] font-semibold text-ink">{skill.name}</span>
        {skill.description && (
          <span className="line-clamp-2 text-[11.5px] leading-relaxed text-soft">{skill.description}</span>
        )}
        <span className="mt-auto font-mono text-[9px] uppercase tracking-wider text-acc">workspace</span>
      </Card>
    </button>
  )
}

export default function SkillsPage() {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [showNew, setShowNew] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  const skills = useQuery({
    queryKey: ['skills'],
    queryFn: () => api.get<SkillSummary[]>('/api/skills'),
  })

  const openSkill = (name: string) => {
    navigate(`/files?path=${encodeURIComponent(`skills/${name}/SKILL.md`)}`)
  }

  return (
    <Page title="Skills">
      <div className="p-4 lg:p-6">
        <div className="mb-4 flex items-center gap-3">
          <span className="font-mono text-[10px] text-muted">
            {skills.data ? `${skills.data.length} workspace skill${skills.data.length === 1 ? '' : 's'}` : '…'}
          </span>
          <Button variant="primary" className="ml-auto" onClick={() => setShowNew(true)}>+ New skill</Button>
        </div>

        {skills.isLoading ? (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {[0, 1, 2].map((i) => (
              <Card key={i} className="space-y-2">
                <Skeleton className="w-1/2" />
                <Skeleton className="w-3/4" />
              </Card>
            ))}
          </div>
        ) : !skills.data || skills.data.length === 0 ? (
          <EmptyState
            title="No workspace skills yet"
            action={<Button onClick={() => setShowNew(true)}>+ New skill</Button>}
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
            {skills.data.map((skill) => (
              <SkillCard key={skill.name} skill={skill} onClick={() => openSkill(skill.name)} />
            ))}
          </div>
        )}
      </div>

      {showNew && (
        <NewSkillSheet
          onClose={() => setShowNew(false)}
          onCreated={(name) => {
            setShowNew(false)
            setToast('Skill created')
            qc.invalidateQueries({ queryKey: ['skills'] })
            openSkill(name)
          }}
        />
      )}
      {toast && <Toast text={toast} onDone={() => setToast(null)} />}
    </Page>
  )
}
