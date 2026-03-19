# Built-in Tools

## File Tools

### file_read
Read a file's contents. Always read a file before editing it — you need to see the current content to construct an accurate edit.

Also works with image files (jpg, png, gif, webp) — the image is returned as visual content you can see and describe. Use this to inspect screenshots, generated images, local photos, or any visual file on disk.

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
- Destructive commands (rm -rf, format, dd, etc.) are blocked by safety guards (can be disabled via `tools.exec.safetyGuard: false` in config).
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

## Browser

### browser
Control a Chromium browser — open pages, interact with elements, take screenshots, run JavaScript, manage tabs. A single tool with an `action` parameter. The browser maintains its own persistent profile at `{data_root}/browser-profile/`. Logins and cookies persist across sessions. Chromium is auto-installed on first use.

**Session lifecycle:**
- `open` — launch a new browser session. Optional: `url`, `headless` (default: `true`). Reuses existing session if one is already open.
- `connect` — attach to an already-running browser via CDP. Requires `cdp_url`.
- `close` — close a session by `session_id`.
- `close_all` — close all sessions.
- `list_sessions` — show active sessions with age and URL.

**Navigation:**
- `navigate` — go to a URL. Requires `url`.
- `back` / `forward` — browser history navigation.

**Content & DOM:**
- `content` — get page text and a numbered interactive element map. Call this before clicking or typing. Optional `selector` to scope.
- `screenshot` — take a screenshot. Optional `selector` for element-only, `full_page` for full page.

**Interaction:**
- `click` — click an element by `index` (from content map), `selector`, or `x`/`y` coordinates.
- `type` — type text into an element by `index` or `selector`. Set `clear` to replace existing content.
- `scroll` — scroll the page. `direction` (up/down), `amount` (pixels, default 500).
- `wait` — wait for a `selector` to appear. Optional `timeout` (ms, default 10000).
- `js` — execute JavaScript `code` on the page.

**Tabs:**
- `tabs` — list open tabs.
- `tab_open` — open a new tab. Optional `url`.
- `tab_switch` — switch to tab by `tab_id`.
- `tab_close` — close tab by `tab_id`.

**Workflow:**
1. `browser(action="open", url="...", headless=True)` — open a session
2. `browser(action="content")` — read the page and get element indices
3. `browser(action="click", index=5)` — click element #5
4. `browser(action="screenshot")` — verify visually
5. `browser(action="close")` — clean up

## Subagents

### spawn
Spawn a background subagent to handle a task independently. Good for:
- Tasks that take many steps and can run without user interaction
- Research or data-gathering that would take multiple tool calls
- Work that doesn't need back-and-forth with the user

The subagent gets its own tool access and reports back when done. Give it a clear, self-contained task description.

## Scheduling

### cron
Schedule and manage tasks. Actions:
- `add` — create a job. Requires `message` and one of `at`, `after`, `every_seconds`, or `cron_expr`. Optional: `name`, `mode`.
- `list` — show all scheduled jobs with mode, schedule, and status.
- `update` — modify a job. Requires `job_id`. Supports: `name`, `message`, `mode`, `enabled`, `every_seconds`, `cron_expr`.
- `remove` — delete a job by `job_id`.

**Schedule types:**
- `at` — ISO datetime (e.g. `"2026-02-12T15:00:00"`). One-shot: runs once and **auto-deletes**. Logs persist. Rejects past times with an error.
- `after` — seconds from now (e.g. `300` = in 5 minutes). One-shot: runs once and **auto-deletes**. Minimum 10 seconds. Simpler than `at` for relative delays.
- `every_seconds` — interval in seconds (recurring).
- `cron_expr` — cron expression like `"0 9 * * *"` (recurring). Uses the user's local timezone automatically.

**Execution modes** (`mode` parameter):

| | Isolated (default) | Session |
|---|---|---|
| Context | Fresh — no session history | Full conversation history |
| Output | Must call `deliver_result` | Responds naturally in chat |
| Interaction | None — one turn, no questions | Fully interactive |
| Concurrency | Parallel — multiple jobs run simultaneously | Sequential — queued into session |
| Best for | Data fetching, reports, monitoring, automated checks | Reminders, conversation-aware tasks, follow-ups |

**Choosing a mode:**
- Default to `isolated` for any task that fetches data, runs commands, or produces a report.
- Use `session` when the task is a reminder, needs conversation context, or should feel like a natural message in the chat.
- When in doubt and the user hasn't specified, use `isolated`.

### deliver_result
Capture the final output of an isolated cron job or heartbeat check. Available during isolated cron execution and heartbeat execution. This is the ONLY way the user sees the result — if the agent doesn't call `deliver_result`, the job runs silently with no output delivered.

### Time expression reference

| User says | Parameters |
|---|---|
| at 3pm today | `at="2026-02-12T15:00:00"` |
| in 2 minutes | `after=120` |
| in 5 minutes | `after=300` |
| in 1 hour | `after=3600` |
| in 2 hours | `after=7200` |
| every 20 minutes | `every_seconds=1200` |
| every hour | `every_seconds=3600` |
| every day at 8am | `cron_expr="0 8 * * *"` |
| weekdays at 5pm | `cron_expr="0 17 * * 1-5"` |
| every Sunday at noon | `cron_expr="0 12 * * 0"` |

### Cron logs

Execution history is stored at `{data_root}/cron/logs/{{job_id}}.jsonl`. Each entry contains timestamp, status, duration, input, and output. Logs persist even after one-shot jobs auto-delete. Use `file_read` to inspect them.

## Heartbeat

### heartbeat
Manage periodic heartbeat tasks in HEARTBEAT.md. Actions:
- `add` — create a task. Requires `message`. Returns the generated task ID.
- `remove` — delete a task by `id`.
- `edit` — update a task's message. Requires `id` and `message`.
- `list` — show all current heartbeat tasks with their IDs.

### heartbeat_done
Signal that the heartbeat check is complete with nothing to report. Only available during heartbeat execution. Call this instead of `deliver_result` when all tasks have been checked and there is nothing noteworthy to tell the user.

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

For storing arbitrary API keys not covered by built-in fields, use `secrets.extra.<name>` paths (e.g. `config set secrets.extra.notion_token ntn_xxx`).

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
