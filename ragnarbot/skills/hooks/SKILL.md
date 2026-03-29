---
name: hooks
description: Guide for creating and managing webhook hooks — event-driven HTTP endpoints that let external scripts trigger the agent. Use when the user wants to set up hooks, write trigger scripts, or design hook-based automation.
---

# Hooks

Hooks are event-driven HTTP endpoints. External scripts decide WHEN to trigger; the agent decides WHAT to do. This is the opposite of cron (polling) — hooks are reactive.

## How Hooks Work

1. You create a hook with `hook(action="create", name="...", instructions="...")`.
2. The tool returns a **hook ID** (which is also the URL path and auth secret).
3. An external script sends `POST /hooks/{hook_id}` with a payload.
4. The gateway spawns an isolated agent session with the hook's instructions + the payload.
5. The session processes the payload and optionally delivers a result to the user's chat.

The hook ID is a long cryptographic token — knowing it grants trigger access. Treat it like a secret.

## Writing Instructions

Instructions are the system prompt for the isolated handler session. They must be **self-contained** — the handler has no conversation history and no knowledge of the user beyond workspace files.

Good instructions include:

- **What data arrives**: "You'll receive a JSON payload with `event`, `repo`, and `message` fields."
- **What to do**: "Summarize the event and deliver it to the user."
- **When to deliver**: "Only deliver if the event type is 'failure' or 'critical'."
- **Format**: "Format as a short alert with emoji indicators."

### Conditional delivery

The most powerful pattern — the handler decides whether to alert:

```
You'll receive a JSON payload from a CI/CD pipeline.

If the build status is "failed" or "error":
  - Deliver a concise alert with the repo name, branch, and error summary.

If the build status is "success":
  - Do NOT deliver. The trigger will be logged silently.

If the payload contains a "rollback" field:
  - Always deliver, regardless of status. Include the rollback reason.
```

### History-aware hooks

The handler can read previous trigger logs to detect patterns:

```
You'll receive server metrics as JSON (cpu, memory, disk, latency).

Use file_read to check the last 5 entries in the hook's log file at
{workspace}/hooks/logs/{hook_id}.jsonl.

If CPU > 90% for 3+ consecutive triggers, deliver an alert.
If memory usage increased by more than 20% since the previous trigger, deliver a warning.
Otherwise, do not deliver.
```

## Generating Trigger Scripts

When the user asks to set up a hook end-to-end, create both the hook AND the trigger script.

### curl (simplest)

```bash
#!/bin/bash
HOOK_URL="http://localhost:18791/hooks/hk_abc123..."

# Example: send JSON payload
curl -s -X POST "$HOOK_URL" \
  -H "Content-Type: application/json" \
  -d '{"event": "deploy", "status": "success", "repo": "myapp"}'
```

### Python

```python
import requests

HOOK_URL = "http://localhost:18791/hooks/hk_abc123..."

payload = {"event": "deploy", "status": "success"}
resp = requests.post(HOOK_URL, json=payload)
print(resp.status_code, resp.json())
```

### Scheduling triggers externally

Hook scripts can be scheduled with any external mechanism:

- **crontab**: `*/5 * * * * /path/to/trigger_script.sh` — every 5 minutes
- **launchd** (macOS): plist with `StartInterval`
- **systemd timer** (Linux): `.timer` unit
- **fswatch/inotifywait**: trigger on file changes
- **CI/CD webhooks**: GitHub Actions, GitLab CI, etc.

## Security

- The hook ID IS the secret. Never expose it in logs, commits, or public repos.
- Store the hook ID in an environment variable or a secrets file, not hardcoded in scripts.
- The hooks server only listens locally by default. For external access, use a tunnel (ngrok, cloudflared, SSH tunnel).
- Each hook has independent rate limiting.

## Patterns

### Alert hook
The default pattern. Every trigger delivers a message to the user.
- Mode: `alert`
- Instructions: "Summarize the payload and deliver it."
- Use for: notifications, CI alerts, monitoring

### Silent hook
Triggers are logged but nothing is delivered.
- Mode: `silent`
- Instructions: "Process the payload and save results to a file."
- Use for: data collection, logging, background sync

### Conditional hook
The handler inspects the payload and decides whether to deliver.
- Mode: `alert` (handler can choose not to call `deliver_result`)
- Instructions include conditional logic
- Use for: smart monitoring, threshold-based alerts, deduplication

### Action hook
The handler doesn't just alert — it takes action (runs commands, edits files, makes API calls) and then reports what it did.
- Mode: `alert`
- Instructions: "If the payload indicates a failing health check, restart the service via exec, then deliver the result."
- Use for: auto-remediation, automated workflows

## Relationship to Cron and Heartbeat

- **Cron**: polling — the agent runs tasks on a schedule. Best for "do X every N minutes."
- **Heartbeat**: periodic context check — the agent reviews tasks on an interval. Best for ongoing awareness.
- **Hooks**: reactive — external events trigger the agent. Best for "when X happens, do Y."

These complement each other:
- A cron job can run a script that triggers a hook (scheduled event-driven processing).
- A hook can create/modify cron jobs (event triggers recurring monitoring).
- Heartbeat provides context; hooks provide events; cron provides schedules.
