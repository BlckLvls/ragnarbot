---
name: cron
description: Schedule reminders and recurring tasks with isolated or session mode.
---

# Cron

Use the `cron` tool to schedule recurring tasks and reminders.

## Execution Modes

### Isolated (default)
Fresh context per run. No session history. The agent executes the task independently and delivers the result via `deliver_result`. Multiple isolated jobs run fully in parallel.

Best for: data fetching, reports, monitoring, automated checks.

### Session
Injected into the user's active chat as a message. Fully interactive — the agent sees conversation history, can ask questions, and responds naturally.

Best for: reminders, conversation-aware tasks, follow-ups.

## Examples

Isolated task (agent fetches data and delivers result):
```
cron(action="add", message="Check HKUDS/ragnarbot GitHub stars and report the count", every_seconds=3600, mode="isolated")
```

Session reminder (appears in chat like a user message):
```
cron(action="add", message="Time to take a break!", every_seconds=1200, mode="session")
```

Named job with cron expression:
```
cron(action="add", name="Morning briefing", message="Summarize top HN stories", cron_expr="0 9 * * *", mode="isolated")
```

Update a job:
```
cron(action="update", job_id="abc123", cron_expr="0 10 * * *")
cron(action="update", job_id="abc123", mode="session", enabled=true)
```

List and remove:
```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| every Sunday at noon | cron_expr: "0 12 * * 0" |
