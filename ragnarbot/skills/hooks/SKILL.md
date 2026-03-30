---
name: hooks
description: Guide for creating and managing webhook hooks — event-driven HTTP endpoints that let external scripts trigger the agent autonomously. Use when the user wants to set up hooks, write trigger scripts, design hook-based automation, connect external systems to the agent, build alert pipelines, or create any reactive "when X happens, do Y" workflow. Also use when discussing the relationship between hooks, cron jobs, and heartbeat.
---

# Hooks

Hooks are event-driven HTTP endpoints that turn external events into autonomous agent actions. The core separation of concerns: **the script decides WHEN to trigger, the agent decides WHAT to do.**

Hooks are NOT just alerters. Each trigger spawns a full autonomous agent session with access to every tool — exec, browser, web_search, web_fetch, file operations, cron, other hooks. The handler can research, compare, compute, run commands, write files, and take corrective action before deciding whether to notify the user.

## How Hooks Work

```
External script ──POST──> Hook Server ──spawn──> Isolated Agent Session
                                                    │
                                                    ├── reads instructions
                                                    ├── parses payload
                                                    ├── uses tools (exec, web, files, etc.)
                                                    ├── optionally calls deliver_result
                                                    │        │
                                                    │        └──> message appears in user's chat
                                                    └── session ends, trigger logged
```

Step by step:

1. Create a hook: `hook(action="create", name="...", instructions="...")`
2. The tool returns a **hook ID** (`hk_...`) — this is the URL path AND the auth secret
3. External script sends `POST http://HOST:PORT/hooks/{hook_id}` with a payload body
4. Hook server validates the ID, checks rate limits, reads the payload
5. An isolated agent session starts with HOOK_ISOLATED.md as system prompt containing: hook name, mode, instructions, payload, workspace path
6. The session has full tool access and processes the payload per the instructions
7. If the handler calls `deliver_result(content="...")`, the message appears in the user's chat
8. A session marker is injected into the user's conversation history: `[Hook triggered: {name} | id: {id} | {ts} | status: ok]`
9. The trigger is logged to `~/.ragnarbot/hooks/logs/{hook_id}.jsonl`

The isolated session has NO conversation history, NO knowledge of previous messages. It is a fresh agent that sees only: the instructions, the payload, the workspace files, and its tools.

## Hook Tool API

```
hook(action="create", name="...", instructions="...", mode="alert"|"silent")
hook(action="list")
hook(action="update", id="...", name="...", instructions="...", mode="...")
hook(action="delete", id="...")
hook(action="history", id="...", limit=10)
```

- `create` — name and instructions required. Mode defaults to "alert".
- `list` — shows all hooks with truncated IDs, mode, trigger count, status.
- `update` — id required. Provide any combination of name, instructions, mode.
- `delete` — id required. Permanent.
- `history` — id required. Returns the last N trigger log entries.

## Writing Instructions

Instructions are the single most important part of a hook. They become the system prompt for the isolated handler. The handler sees ONLY the instructions and the payload — no conversation context, no prior messages, no user preferences beyond what the workspace contains.

### Principles

1. **Self-contained** — include everything the handler needs to know. It cannot ask follow-up questions.
2. **Describe the expected payload** — the handler needs to know what fields to look for.
3. **Specify delivery conditions** — when to call `deliver_result` and when to stay silent.
4. **Define the output format** — the handler will follow whatever format the instructions specify.
5. **Include error handling** — what to do if the payload is malformed or unexpected.

### Bad vs Good Instructions

Bad — vague, assumes context:

```
Handle the CI notification.
```

The handler has no idea what "CI notification" looks like, what fields matter, or what to deliver.

Bad — too rigid, wastes the agent's intelligence:

```
Extract the "status" field and send it to the user.
```

This is a string extraction, not an agent task. A curl + jq pipeline would be better.

Good — structured, specific, leverages agent intelligence:

```
You receive a JSON payload from GitHub Actions with these fields:
- action: "completed"
- workflow_run.conclusion: "success" | "failure" | "cancelled"
- workflow_run.name: workflow name
- workflow_run.html_url: link to the run
- repository.full_name: "owner/repo"
- workflow_run.head_branch: branch name

Decision logic:
- If conclusion is "failure": deliver an alert with the repo, workflow name,
  branch, and a direct link. Use web_fetch on the html_url to extract the
  specific failing step name if possible.
- If conclusion is "cancelled": deliver only if the branch is "main" or "master".
- If conclusion is "success": do NOT deliver.

Format the alert as:
  [REPO] Workflow "NAME" failed on BRANCH
  Failing step: STEP (if found)
  Link: URL
```

Good — multi-step processing:

```
You receive a JSON payload with a "url" field pointing to a web page.

1. Use web_fetch to retrieve the page content.
2. Extract the main article text.
3. Summarize it in 3-5 bullet points.
4. Save the full text to {workspace}/hook-data/articles/YYYY-MM-DD-title.md
5. Deliver the summary to the user with the file path.
```

Good — history-aware with trend detection:

```
You receive server metrics as JSON: {cpu, memory_pct, disk_pct, load_avg, timestamp}.

Read the hook's trigger log at ~/.ragnarbot/hooks/logs/{hook_id}.jsonl using
file_read. Parse the last 10 entries.

Alerting rules:
- If cpu > 90 for 3+ consecutive triggers: CRITICAL alert
- If memory_pct increased by 20+ points compared to 1 hour ago: WARNING
- If disk_pct > 85: WARNING with projected time to full (extrapolate from history)
- Otherwise: do NOT deliver

When alerting, include the current values, the trend direction (rising/falling/stable),
and a suggested action.
```

### Describing Payload Format

Always tell the handler what to expect. Two approaches:

**Enumerated fields** (when the payload structure is known):

```
The payload is JSON with fields:
- event_type: string ("push", "pr", "issue")
- repo: string (e.g. "owner/repo")
- author: string
- message: string
- timestamp: ISO 8601 string
```

**Open-ended** (when payloads vary):

```
The payload is a JSON object. The exact structure varies.
Look for these key fields if present: status, error, message, severity.
If the payload is not valid JSON, treat it as plain text and summarize it.
```

### Delivery Guidance by Mode

**Mode "alert"** (default): the handler SHOULD call `deliver_result` unless the instructions explicitly say otherwise for certain conditions. The system prompt tells it: "If mode is alert, you SHOULD deliver unless the instructions say otherwise."

**Mode "silent"**: the handler should NOT deliver unless the instructions explicitly say to. The system prompt tells it: "If mode is silent, only deliver if the instructions explicitly say to."

Use "alert" mode with conditional delivery logic in instructions for most cases. Use "silent" mode for data collection, logging, and background processing where delivery is the exception.

## Payload Design

When generating trigger scripts, structure payloads for maximum handler effectiveness.

### Best Practices

1. **Use JSON** — the handler parses it naturally
2. **Include metadata** — timestamp, source, severity help the handler make decisions
3. **Keep it focused** — send what matters, not raw dumps
4. **Use consistent field names** — across hooks that process similar events

### Recommended Payload Structure

```json
{
  "event": "descriptive_event_name",
  "source": "system-or-service-name",
  "timestamp": "2025-01-15T14:30:00Z",
  "severity": "info|warning|error|critical",
  "data": {
    // event-specific fields
  }
}
```

### What NOT to Send

- Full log files (use a file path or URL instead)
- Binary data (save to disk, send the path)
- Secrets or credentials
- Payloads over 64KB (default limit) — summarize or reference external data

## Trigger Scripts

When the user asks to set up a hook end-to-end, create BOTH the hook AND the trigger script. Store the hook ID as an environment variable, never hardcoded.

### Shell (curl)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Load hook ID from environment or .env file
HOOK_ID="${RAGNARBOT_HOOK_CI:-}"
if [[ -z "$HOOK_ID" ]]; then
  echo "Error: RAGNARBOT_HOOK_CI not set" >&2
  exit 1
fi

HOOK_URL="http://localhost:18791/hooks/${HOOK_ID}"

payload=$(cat <<EOF
{
  "event": "build_complete",
  "source": "ci",
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "data": {
    "repo": "${REPO_NAME:-unknown}",
    "branch": "${BRANCH:-unknown}",
    "status": "${BUILD_STATUS:-unknown}",
    "commit": "${COMMIT_SHA:-unknown}"
  }
}
EOF
)

response=$(curl -s -w "\n%{http_code}" -X POST "$HOOK_URL" \
  -H "Content-Type: application/json" \
  -d "$payload")

http_code=$(echo "$response" | tail -1)
body=$(echo "$response" | head -1)

if [[ "$http_code" != "202" ]]; then
  echo "Hook trigger failed (HTTP $http_code): $body" >&2
  exit 1
fi
```

### Python

```python
#!/usr/bin/env python3
import json
import os
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

HOOK_ID = os.environ.get("RAGNARBOT_HOOK_MONITOR")
if not HOOK_ID:
    print("Error: RAGNARBOT_HOOK_MONITOR not set", file=sys.stderr)
    sys.exit(1)

HOOK_URL = f"http://localhost:18791/hooks/{HOOK_ID}"

payload = {
    "event": "health_check",
    "source": "monitor",
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "data": {
        "cpu": get_cpu_percent(),
        "memory": get_memory_percent(),
        "disk": get_disk_percent(),
    },
}

req = Request(
    HOOK_URL,
    data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urlopen(req, timeout=10) as resp:
        print(f"Triggered: {resp.status}")
except HTTPError as e:
    print(f"Hook error: {e.code} {e.read().decode()}", file=sys.stderr)
except URLError as e:
    print(f"Connection error: {e.reason}", file=sys.stderr)
```

The Python example uses only stdlib (`urllib`) so no dependencies are needed. For more complex scripts, `requests` or `httpx` are fine.

### GitHub Actions

```yaml
- name: Notify Ragnarbot
  if: failure()
  run: |
    curl -s -X POST "http://${{ secrets.RAGNARBOT_HOST }}/hooks/${{ secrets.RAGNARBOT_HOOK_CI }}" \
      -H "Content-Type: application/json" \
      -d '{
        "event": "ci_failure",
        "source": "github_actions",
        "timestamp": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
        "data": {
          "repo": "${{ github.repository }}",
          "branch": "${{ github.ref_name }}",
          "workflow": "${{ github.workflow }}",
          "run_url": "${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}",
          "actor": "${{ github.actor }}"
        }
      }'
```

Note: GitHub Actions requires an external-facing URL. The user needs a tunnel (ngrok, cloudflared) or a publicly accessible host.

### File Watcher (fswatch)

```bash
#!/usr/bin/env bash
HOOK_ID="${RAGNARBOT_HOOK_FILEWATCHER}"
WATCH_DIR="/path/to/watched/directory"

fswatch -0 --event Created --event Updated "$WATCH_DIR" | while read -d "" file; do
  curl -s -X POST "http://localhost:18791/hooks/${HOOK_ID}" \
    -H "Content-Type: application/json" \
    -d "{\"event\": \"file_changed\", \"data\": {\"path\": \"$file\", \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}}"
done
```

### Securing Hook IDs

- Store in environment variables: `export RAGNARBOT_HOOK_CI="hk_abc123..."`
- Use `.env` files (gitignored): `RAGNARBOT_HOOK_CI=hk_abc123...`
- CI/CD secrets: GitHub Secrets, GitLab CI Variables, etc.
- Never commit hook IDs to version control
- Never log the full hook ID (the system truncates it in logs for this reason)

## The Self-Authoring Pattern

The ideal user experience: the user describes what they want in natural language, and the agent handles everything — creating the hook, writing the trigger script, deploying it, and confirming it works. The user never needs to see a hook ID, a URL, or a payload format.

### Full Cycle

1. **User describes the goal**: "Notify me when my CI pipeline fails"
2. **Agent creates the hook**: `hook(action="create", name="ci-failure-alert", instructions="...", mode="alert")`
3. **Agent writes the trigger script**: creates a shell/Python script in the workspace, injects the hook ID via environment variable
4. **Agent deploys the script**: adds a crontab entry, creates a launchd plist, or sets up fswatch — whatever the platform needs
5. **Agent confirms**: tells the user what was set up, how it works, and how to test it
6. **User starts receiving results**: hook fires, handler processes, user sees the alert

### Important: Manage the Complexity

The user should NOT need to understand hook IDs, URLs, payload formats, or HTTP. The agent abstracts all of this. When explaining what was set up, focus on what happens ("when your CI fails, you'll get a notification with the error details") not how ("the script sends a POST request to the hooks endpoint with a JSON payload").

When the user asks to modify or delete a hook, use `hook(action="list")` to find it by name, then operate on it. Do not ask the user for the hook ID.

## Hook Patterns

### Pattern 1: Alert Hook

Every trigger delivers a message. The simplest pattern.

Mode: `alert`

Example — deploy notification:

```
hook(action="create", name="deploy-notify", mode="alert", instructions="""
You receive a JSON payload about a deployment:
- app: application name
- environment: "staging" | "production"
- version: deployed version
- deployer: who triggered it
- timestamp: when it happened

Format and deliver:
  Deploy: APP vVERSION -> ENVIRONMENT
  By: DEPLOYER at TIME
""")
```

### Pattern 2: Silent Hook

Triggers are logged and data is collected, but nothing is delivered. The handler processes data for later analysis.

Mode: `silent`

Example — metrics collector:

```
hook(action="create", name="metrics-collector", mode="silent", instructions="""
You receive a JSON payload with server metrics: cpu, memory, disk, latency.

Append a formatted line to {workspace}/data/metrics.csv:
  timestamp,cpu,memory,disk,latency

Create the file with headers if it doesn't exist.
Do NOT deliver a result — this is a data collection hook.
""")
```

### Pattern 3: Conditional Hook

The handler inspects the payload and decides whether to deliver. This is the most common real-world pattern.

Mode: `alert` (the handler may choose not to call deliver_result)

Example — smart CI notifications:

```
hook(action="create", name="ci-monitor", mode="alert", instructions="""
You receive a JSON payload from CI/CD:
- repo: repository name
- branch: branch name
- status: "success" | "failure" | "error" | "cancelled"
- workflow: workflow name
- url: link to the run
- error_message: present only on failure

Decision logic:
- status "failure" or "error" on main/master branch: ALWAYS deliver
- status "failure" on other branches: deliver ONLY if error_message contains
  "test" or "lint" (code quality issues)
- status "cancelled": never deliver
- status "success": deliver ONLY if the previous trigger (check the hook log
  at ~/.ragnarbot/hooks/logs/{hook_id}.jsonl) was a failure — this signals recovery

When delivering, include: repo, branch, status, and a direct link.
On recovery, note: "Previously failing, now passing."
""")
```

### Pattern 4: Action Hook

The handler takes action (runs commands, writes files, calls APIs) THEN reports what it did.

Mode: `alert`

Example — auto-restart on health check failure:

```
hook(action="create", name="service-guardian", mode="alert", instructions="""
You receive a JSON payload from a health check script:
- service: service name (e.g. "nginx", "postgres", "myapp")
- status: "healthy" | "unhealthy" | "timeout"
- endpoint: the URL that was checked
- response_time_ms: response time (if available)
- error: error message (if unhealthy)

If status is "healthy": do NOT deliver.

If status is "unhealthy" or "timeout":
1. Use exec to check if the service process is running: `pgrep -f {service}` or `systemctl is-active {service}`
2. If the process is not running, attempt restart: `systemctl restart {service}`
3. Wait 5 seconds, then check the endpoint again using web_fetch
4. Deliver a report:
   - What was wrong
   - What action was taken
   - Whether the service recovered
   - Current status

If you cannot restart (permission denied, not found), deliver the error details
and suggest manual intervention.
""")
```

### Pattern 5: History-Aware Hook

The handler reads previous trigger logs to detect trends, anomalies, or state changes over time.

Mode: `alert`

Example — anomaly detection:

```
hook(action="create", name="traffic-anomaly", mode="alert", instructions="""
You receive a JSON payload with web traffic metrics:
- requests_per_minute: integer
- error_rate_pct: float
- avg_response_ms: integer
- timestamp: ISO 8601

Read the trigger log at ~/.ragnarbot/hooks/logs/{hook_id}.jsonl using file_read.
Parse the last 20 entries to establish baseline.

Anomaly rules:
- If requests_per_minute is more than 3x the average of the last 20 entries: SPIKE alert
- If requests_per_minute drops below 10% of the average: DROP alert
- If error_rate_pct exceeds 5% and the previous 3 entries were below 2%: ERROR SURGE alert
- If avg_response_ms exceeds 2x the recent average for 5+ consecutive triggers: SLOWDOWN alert

When alerting, include:
- The anomaly type
- Current value vs baseline average
- How long the anomaly has persisted (count of consecutive anomalous triggers)
- A trend line description (accelerating, stabilizing, new occurrence)

If no anomaly detected, do NOT deliver.
""")
```

### Pattern 6: Multi-Hook Pipeline

Multiple hooks with different responsibilities triggered by the same event or in sequence. One event, several specialized responses.

Example — deployment pipeline with three hooks:

**Hook 1: Deploy validator** — checks if the deploy is safe

```
hook(action="create", name="deploy-validator", mode="alert", instructions="""
You receive a deploy intent payload with: app, version, environment, changes_url.

1. Use web_fetch on changes_url to get the changelog/diff
2. Check for risky patterns: database migrations, config changes, dependency updates
3. If environment is "production" and changes include migrations:
   deliver a WARNING with the migration details and ask for confirmation
4. If changes look routine: deliver a short "Deploy looks safe" confirmation
""")
```

**Hook 2: Deploy monitor** — watches for post-deploy issues

```
hook(action="create", name="deploy-monitor", mode="alert", instructions="""
You receive a post-deploy health payload with: app, version, environment, health_status, error_count, latency_ms.

Compare with the previous 5 triggers in the hook log.

If error_count increased by more than 50% since the pre-deploy baseline: deliver a ROLLBACK warning
If latency_ms increased by more than 100%: deliver a DEGRADATION warning
If health_status is "healthy" and metrics are stable: do NOT deliver
""")
```

**Hook 3: Deploy logger** — records everything silently

```
hook(action="create", name="deploy-log", mode="silent", instructions="""
Append the full payload as a structured entry to {workspace}/data/deploy-history.jsonl.
Include a parsed summary line. Do NOT deliver.
""")
```

The trigger scripts can fire all three from the same deployment pipeline at different stages.

## Relationship to Cron and Heartbeat

| Feature | Trigger | Best for |
|---------|---------|----------|
| **Cron** | Scheduled (time-based) | "Do X every N minutes" — polling, periodic tasks |
| **Heartbeat** | Periodic (interval-based) | Ongoing awareness, context review, nudges |
| **Hooks** | Reactive (event-based) | "When X happens, do Y" — external events, webhooks |

These are complementary, not competing:

**Cron triggers hooks**: a cron job runs a monitoring script every 5 minutes; the script collects data and triggers a hook for intelligent processing. The cron handles timing, the hook handles decision-making.

**Hooks create cron jobs**: a deploy hook receives notification of a new deployment and creates a temporary cron job to monitor health every minute for the next hour. After the hour, the cron job cleans itself up.

**Hooks trigger other hooks**: a data-collection hook receives raw data, processes it, and triggers a separate analysis hook with the processed results. Pipeline pattern.

**Cron + hooks as poll-and-react**: a cron job polls an API every 10 minutes, compares with the previous result, and triggers a hook ONLY if something changed. Efficient event detection without real-time webhooks.

When the user describes a need, choose the right primitive:
- "Tell me every morning about..." -> cron
- "When my CI fails..." -> hook
- "Keep an eye on..." -> cron (polling) + hook (processing), or heartbeat for lighter awareness
- "Run this every hour and alert me if..." -> cron triggers a hook

## Enabling and Configuration

Hooks are enabled by default. The HTTP server starts automatically with the gateway.

Configuration options:
- `hooks.enabled` (bool, default: true) — start the hooks HTTP server
- `hooks.port` (int, default: 18791) — HTTP server port
- `hooks.rateLimitPerHook` (int, default: 60) — max triggers per hook per minute
- `hooks.maxPayloadBytes` (int, default: 65536) — max POST body size in bytes

The server listens on all interfaces by default. The health endpoint is at `GET /hooks/health` — returns `{"status": "ok"}` when the server is running.

If hooks have been explicitly disabled by the user, re-enable them with `config(action="set", path="hooks.enabled", value="true")` and restart before creating hooks.

## Troubleshooting

**Hook not responding (connection refused)**:
- Hooks not enabled: check `config(action="get", path="hooks.enabled")`
- Wrong port: check `config(action="get", path="hooks.port")`
- Agent not running: the hooks server runs as part of the main agent process

**HTTP 404 (not found)**:
- Wrong hook ID in the URL
- Hook was deleted
- Hook is disabled (disabled hooks return 404)

**HTTP 413 (payload too large)**:
- Payload exceeds `maxPayloadBytes` (default 64KB)
- Solution: increase the limit or send less data (reference files by path instead)

**HTTP 429 (rate limited)**:
- Hook exceeded `rateLimitPerHook` triggers in the last minute (default 60)
- Solution: reduce trigger frequency or increase the limit

**Hook triggers but nothing is delivered**:
- Mode is "silent" and instructions don't explicitly call for delivery
- Instructions don't cover the payload's conditions (handler decided not to deliver)
- Handler errored before reaching deliver_result — check `hook(action="history", id="...")` for error status
- Check the trigger log at `~/.ragnarbot/hooks/logs/{hook_id}.jsonl` for details

**Handler behaves unexpectedly**:
- Instructions are the ONLY context — the handler has no conversation history
- Review instructions for ambiguity or missing payload field descriptions
- Use `hook(action="update", id="...", instructions="...")` to refine

## Security

- **Hook ID = secret.** The ID is a cryptographic token (`hk_` + 32 bytes URL-safe base64). Knowing it grants trigger access. Treat it like an API key.
- **Never expose hook IDs** in logs, commits, public repos, or chat messages shared externally.
- **Store securely**: environment variables, `.env` files (gitignored), CI/CD secrets managers.
- **Local by default**: the server listens on localhost. Triggers from external systems require a tunnel.
- **Tunnel setup for external access**: use `ngrok http 18791`, `cloudflared tunnel`, or SSH port forwarding. The tunnel URL becomes the trigger URL base.
- **Instructions are private**: the triggering script never sees the hook's instructions. Only the agent sees them. This means instructions can contain sensitive logic, internal thresholds, or references to private files.
- **Rate limiting is per-hook**: each hook has its own sliding window rate limit. One hook being hammered does not affect others.
- **Rotation**: if a hook ID is compromised, delete the hook and create a new one. Update all trigger scripts with the new ID.
