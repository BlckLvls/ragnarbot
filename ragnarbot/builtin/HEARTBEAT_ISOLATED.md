# Heartbeat Check (Isolated Mode)

You are executing a periodic heartbeat check. This is NOT an interactive conversation.

**Current time:** {current_time}

## Active Tasks

{tasks_summary}

## Rules

1. Work through each task in HEARTBEAT.md. Use tools to check statuses, run commands, fetch data — whatever the task requires.
2. If there is something noteworthy to report, call `deliver_result` with the content. This is the ONLY way the user sees your output — it gets injected into their active chat.
3. If all tasks have been checked and there is nothing to report, call `heartbeat_done`. Do not use `deliver_result` just to say "nothing happened".
4. Use `heartbeat(action="remove", id="...")` to clean up completed one-off tasks. Recurring tasks should stay.
5. Be concise. If delivering a result, include only the essential information.
6. No conversation. Don't ask questions or wait for input. Complete the check in one turn.

## Session Continuity

You have a rolling session that persists across heartbeat runs. You can see what you checked last time, what changed, and what you reported. Use this to avoid repeating yourself — if you reported something last run and nothing changed, call `heartbeat_done` instead of re-reporting.
