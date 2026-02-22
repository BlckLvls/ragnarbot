---
name: agent-creator
description: Create or update sub-agent definitions. Use when designing, structuring, or improving AGENT.md files that define specialized background agents with custom instructions, model preferences, and tool access.
---

# Agent Creator

This skill provides guidance for creating agent definitions — background sub-agents that run tasks autonomously and deliver results via `deliver_result`.

## About Agents

Agents are self-contained AGENT.md files that define specialized sub-agent types. When spawned, a sub-agent receives the AGENT.md body as its system prompt and runs independently with its own conversation and tool set.

### What Agents Provide

1. **Specialized behavior** — focused instructions for a specific domain
2. **Tool restrictions** — only the tools the agent actually needs
3. **Model overrides** — use a different model when appropriate
4. **Isolation** — runs in the background, delivers results when done

### How Agents Differ from Skills

| | Skills | Agents |
|---|---|---|
| **Runs as** | Instructions injected into the main agent's context | Independent sub-agent process in the background |
| **Interaction** | Main agent follows skill instructions directly | Sub-agent works autonomously, delivers result |
| **Tools** | Main agent's full tool set | Restricted to declared `allowedTools` |
| **Use when** | You need to guide the main agent's behavior | You need parallel/background work with focused scope |

## Anatomy of an Agent

```
agent-name/
└── AGENT.md (required)
    ├── YAML frontmatter (required)
    │   ├── name: (required)
    │   ├── description: (required)
    │   ├── model: (optional, default: "default")
    │   ├── allowedTools: (optional, default: "all")
    │   └── allowedSkills: (optional, default: "none")
    └── Markdown body — the agent's system prompt
```

### Frontmatter Fields

- **name** — Agent identifier. Must match the directory name. Kebab-case, lowercase.
- **description** — What the agent does. Shown in the agents summary so the main agent knows when to spawn it. Be specific about the agent's specialty and output format.
- **model** — `default` (inherits from config) or explicit like `anthropic/claude-sonnet-4.6`. Only override when a specific model is genuinely better for the task.
- **allowedTools** — `all` (gets all safe tools) or explicit list like `[web_search, web_fetch, browser]`. Available safe tools: `file_read`, `file_write`, `file_edit`, `list_dir`, `exec`, `web_search`, `web_fetch`, `browser`, `exec_bg`, `poll`, `output`, `kill`, `dismiss`. Use `[]` for agents that need no tools.
- **allowedSkills** — `none` (default, no skills), `all` (every available skill), or explicit list like `[agent-creator, prompt-engineering]`. When skills are allowed, the agent receives a skills summary in its prompt and can use `file_read` to load the full SKILL.md on demand. `file_read` is automatically added to the agent's tools when any skills are allowed, even if not listed in `allowedTools`.

### The Body (System Prompt)

The body becomes the agent's system prompt. Write it as direct instructions to the agent.

**Must include:**

1. **Role** — What the agent is and does (first sentence)
2. **Process** — Step-by-step workflow
3. **Output format** — What the deliverable looks like

Note: `deliver_result` instructions are automatically injected via `SUBAGENT.md` — no need to repeat them in the agent body.

**Keep it focused.** An agent should do one thing well. If you need multiple specialties, create multiple agents.

## Agent Creation Process

1. Understand what the agent should do with concrete examples
2. Decide which tools it needs (minimize — only what's required)
3. Initialize with `init_agent.py`
4. Write the AGENT.md body
5. Test by spawning the agent

### Agent Naming

- Lowercase letters, digits, and hyphens only
- Max 64 characters
- Prefer short nouns or noun phrases describing the role: `researcher`, `code-reviewer`, `data-analyst`
- Name the directory exactly after the agent name

### Step 1: Understanding the Agent

Clarify with the user:

- What task should this agent handle?
- What's the expected output format?
- Does it need web access, file access, shell access?
- Should it use a specific model?

### Step 2: Choosing Tools and Skills

Pick the minimum set of tools the agent needs. Common patterns:

- **Research agent**: `[web_search, web_fetch, browser, file_write]`
- **Code agent**: `[file_read, file_write, file_edit, list_dir, exec]`
- **Analysis agent**: `[file_read, exec, web_search]`
- **No-tool agent**: `[]` (pure reasoning, just deliver_result)

**Skills:** If the agent would benefit from existing skills, propose them to the user with a brief reason for each. The user decides which skills to include — do not silently add skills. Leave `allowedSkills` out of the template unless the user explicitly approves specific skills.

### Step 3: Initializing the Agent

Run `init_agent.py` to scaffold the directory:

```bash
{baseDir}/scripts/init_agent.py <agent-name> [--path <output-directory>]
```

Arguments:

- `name` (required): Agent name in kebab-case (max 64 chars)
- `--path` (optional): Parent directory. Defaults to `~/.ragnarbot/workspace/agents/`

Examples:

```bash
{baseDir}/scripts/init_agent.py code-reviewer
{baseDir}/scripts/init_agent.py data-analyst --path /tmp
```

### Step 4: Writing the Body

Write clear, direct instructions. Example structure:

```markdown
# Agent Name

You are a [role]. Your job is to [primary task].

## Process

1. Step one
2. Step two
3. Step three

## Output Format

Structure your result as:
- **Section** — what goes here
```

### Step 5: Testing

Spawn the agent and verify:

- It follows the instructions correctly
- It uses only the declared tools
- The description triggers the main agent to spawn it at the right times

## Examples

### Minimal agent (no tools)

```yaml
---
name: summarizer
description: Summarizes text input into concise bullet points.
allowedTools: []
---
```

### Research agent

```yaml
---
name: market-researcher
description: Researches market trends, competitors, and industry data. Produces structured reports with sources.
allowedTools: [web_search, web_fetch, browser, file_write]
---
```

### Code agent with model override

```yaml
---
name: code-reviewer
description: Reviews code for bugs, security issues, and style. Produces actionable feedback.
model: anthropic/claude-sonnet-4-20250514
allowedTools: [file_read, list_dir, exec]
---
```

### Agent with skills access

```yaml
---
name: content-writer
description: Writes and refines content using available skills for SEO, brand voice, and research.
allowedTools: [web_search, web_fetch, file_write]
allowedSkills: [seo-optimizer, brand-voice-extractor]
---
```

Note: `file_read` is automatically added when `allowedSkills` is set — no need to list it in `allowedTools`.
