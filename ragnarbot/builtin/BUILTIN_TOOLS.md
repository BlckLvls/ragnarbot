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

## Web Tools

### web_search
Search the web via Brave Search. Returns titles, URLs, and snippets. Use when the user asks a question that needs current information, or when you need to look something up.

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
Schedule recurring tasks or reminders. Actions:
- `add` — create a job. Requires `message` and either `every_seconds` (interval) or `cron_expr` (cron syntax like "0 9 * * *" for daily at 9am).
- `list` — show all scheduled jobs.
- `remove` — delete a job by `job_id`.

Jobs run through the agent and deliver responses to the user's chat.

## Downloads

### download_file
Download a file that the user shared in chat. When a user sends a document, voice message, or other file, you'll see a `[file available: ...]` marker with a `file_id`. Pass that `file_id` to this tool to download and access the file locally.
