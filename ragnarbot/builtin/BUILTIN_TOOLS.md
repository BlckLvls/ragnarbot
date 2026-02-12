# Built-in Tools

## File Tools

### file_read
Read a file's contents. Always read a file before editing it — you need to see the current content to construct an accurate edit.

### write_file
Write content to a file (creates parent directories automatically). Use for creating new files. For modifying existing files, prefer `edit_file` instead.

### edit_file
Replace a specific text block in an existing file. Provide the exact `old_text` to match — it must appear exactly once. Include enough surrounding context to make the match unique. Always `file_read` the file first.

### list_dir
List directory contents. Use to explore project structure or check what files exist before acting.

## Shell

### exec
Execute a shell command. Returns stdout, stderr, and exit code.
- Has a timeout (commands that run too long are killed).
- Destructive commands (rm -rf, format, dd, etc.) are blocked by safety guards.
- Provide `working_dir` when the command must run in a specific directory.
- For long-running processes, warn the user about potential timeout.

## Background Execution

For tasks that take more than a few seconds — image generation, data processing, long scripts, batch operations. Do NOT use these for quick commands; use `exec` instead.

### exec_bg
Launch a shell command in the background. Returns a `job_id` immediately. The system notifies you automatically when the job finishes — no need to poll or check manually in most cases.
- Use `label` to give the job a human-readable name.
- `working_dir` sets where the command runs.
- Same safety guards as `exec`.

### output
Read the current stdout/stderr of a running or completed background job. Pass `job_id` and optionally `lines` (default 20). Use when you need to check progress mid-run.

### poll
Schedule a status check for all background jobs after N seconds. Use ONLY when the task produces periodic progress output you need to monitor (build logs, training progress, incremental results). In most cases you don't need this — the automatic completion notification is enough.

### kill
Terminate a running background job or cancel a scheduled poll. Pass the `job_id`.

### dismiss
Remove a completed/errored/killed job from the status summary. Cannot dismiss running jobs.

## Web Tools

### web_search
Search the web using the configured search engine (Brave Search or DuckDuckGo). Returns titles, URLs, and snippets. Use when the user asks a question that needs current information, or when you need to look something up.

### web_fetch
Fetch a URL and extract its content as markdown or plain text. Use when you have a specific URL to read (from search results, user-provided links, documentation). Set `extractMode` to "text" for simpler output or "markdown" (default) for structured content.

## Message

### message
Send a message to a specific channel and chat. In normal conversation, you do NOT need this tool — just respond with text directly. Use `message` only when:
- You need to send an intermediate status update during a multi-step tool operation
- You need to deliver a message to a different channel or chat than the current one

## Subagents

### spawn
Spawn a background subagent to handle a task independently. Good for:
- Tasks that take many steps and can run without user interaction
- Research or data-gathering that would take multiple tool calls
- Work that doesn't need back-and-forth with the user

The subagent gets its own tool access and reports back when done. Give it a clear, self-contained task description.

## Scheduling

### cron
Schedule and manage recurring tasks. Actions:
- `add` — create a job. Requires `message` and either `every_seconds` or `cron_expr`. Optional: `name`, `mode`.
- `list` — show all scheduled jobs with mode and status.
- `update` — modify a job. Requires `job_id`. Supports: `name`, `message`, `mode`, `enabled`, `every_seconds`, `cron_expr`.
- `remove` — delete a job by `job_id`.

**Execution modes** (`mode` parameter):
- `isolated` (default) — fresh context per run, no session history. The agent gets a cron-specific prompt and must use `deliver_result` to send output. Jobs run fully parallel.
- `session` — injected into the user's active chat session as a message. Fully interactive — the agent can ask follow-up questions and use conversation history.

### deliver_result
Capture the final output of an isolated cron job. Only available during isolated cron execution. This is the ONLY way the user sees the result of an isolated job — call it with the final output content.

**Cron logs** are stored at `~/.ragnarbot/cron/logs/{{job_id}}.jsonl`. Use `file_read` to inspect execution history.

## Configuration

### config
View and modify bot configuration at runtime. Actions:
- `schema` — discover available config fields with types, defaults, and reload levels. Pass `path` to filter by prefix (e.g. `agents.defaults`).
- `get` — read the current value of a config field. Requires `path`.
- `set` — change a config value. Requires `path` and `value`. Values are auto-coerced to the target type (e.g. "0.5" becomes float).
- `list` — show all current config values as a flat list.
- `diff` — show only values that differ from defaults.

Fields have reload levels:
- **hot** — applied immediately (e.g. temperature, max_tokens, stream_steps, search settings).
- **warm** — saved to disk, requires `restart` tool to apply (e.g. model, telegram settings, gateway port).
- **cold** — saved to disk, requires full re-onboard to apply (e.g. workspace path).

### restart
Schedule a graceful gateway restart. The restart happens after the current response is fully sent. Use after changing "warm" config values that need a restart to apply.

### update
Check for new ragnarbot versions, view release notes, and self-update. Actions:
- `check` — compare current version against the latest GitHub release. Returns `current_version`, `latest_version`, and `update_available`.
- `changelog` — fetch release notes for a specific version. Pass `version` (e.g. `"0.4.0"`) or omit to get the latest. Returns the release body from GitHub.
- `update` — upgrade ragnarbot to the latest version and restart. Tries `uv tool upgrade` first, falls back to `pip install --upgrade`. After upgrade, the gateway restarts automatically and sends a notification with the changelog URL.

## Downloads

### download_file
Download a file that the user shared in chat. When a user sends a document, voice message, or other file, you'll see a `[file available: ...]` marker with a `file_id`. Pass that `file_id` to this tool to download and access the file locally.
