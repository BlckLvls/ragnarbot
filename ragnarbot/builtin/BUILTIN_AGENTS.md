# Built-in Agents

## fast-researcher

Quick, focused research briefs. Targets 8-15 high-quality sources, delivers a concise actionable brief (500-1500 words) with key findings, citations, and honest gap assessment. No browser — uses `web_search` and `web_fetch` only.

**Use when:**
- The user needs a quick answer backed by sources — "what's the deal with X?"
- Quick competitive checks, technology overviews, fact verification
- Background research to inform a decision
- Current state summaries, rapid market scans
- The user explicitly asks for fast/quick research

## deep-researcher

Exhaustive, multi-source research reports. Builds a formal research plan, systematically investigates every angle (50-100+ sources), cross-references findings, and delivers polished markdown reports with full citations, confidence assessments, and knowledge gaps. Has browser access for JS-rendered pages.

**Use when:**
- The user explicitly asks for deep/thorough/comprehensive research
- The topic is complex with many dimensions that need systematic coverage
- Market research, competitive analysis, literature reviews, due diligence
- The user needs a deliverable-grade report they can share with others

## Choosing the right level of research

Not every question needs an agent. Match the effort to the task:

| Situation | Approach |
|---|---|
| Simple factual question, 1-2 sources enough | Answer it yourself with `web_search`/`web_fetch` |
| Quick overview, fact-check, "what's X?" | `fast-researcher` |
| Complex multi-angle investigation, formal report | `deep-researcher` |
| User says "research this" without specifics | Default to `fast-researcher` — suggest deep if the topic clearly needs it |
