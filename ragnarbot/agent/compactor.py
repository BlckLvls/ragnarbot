"""Auto-compaction engine for conversation history."""

import copy
import json
from datetime import datetime, timedelta

from loguru import logger

from ragnarbot.agent.cache import CacheManager
from ragnarbot.agent.context import ContextBuilder
from ragnarbot.agent.tokens import estimate_tokens
from ragnarbot.prompts.compaction import COMPACTION_SYSTEM_PROMPT
from ragnarbot.providers.base import LLMProvider
from ragnarbot.session.manager import Session


class Compactor:
    """Auto-compaction engine for conversation history."""

    THRESHOLDS = {"eco": 0.40, "normal": 0.60, "full": 0.85}
    TAIL_MIN = 10
    TAIL_MAX = 20
    TAIL_TOKEN_RATIO = 0.05  # 5% of max_context_tokens

    def __init__(
        self,
        provider: LLMProvider,
        cache_manager: CacheManager,
        max_context_tokens: int,
        model: str,
    ):
        self.provider = provider
        self.cache_manager = cache_manager
        self.max_context_tokens = max_context_tokens
        self.model = model

    def should_compact(
        self,
        messages: list[dict],
        context_mode: str,
        tools: list[dict] | None = None,
        session=None,
    ) -> bool:
        """Check if context exceeds threshold for current mode."""
        if context_mode not in self.THRESHOLDS:
            return False

        total = self.cache_manager.estimate_context_tokens(
            messages, self.model, tools=tools, session=session,
        )

        threshold = self.THRESHOLDS[context_mode] * self.max_context_tokens
        return total >= threshold

    async def compact(
        self,
        session: Session,
        context_mode: str,
        context_builder: ContextBuilder,
        messages: list[dict],
        new_start: int,
        tools: list[dict] | None,
        **build_kwargs,
    ) -> tuple[list[dict], int]:
        """Perform compaction and return rebuilt (messages, new_start)."""
        msgs = session.messages

        # Nothing to compact if fewer than TAIL_MIN messages in session
        if len(msgs) < self.TAIL_MIN:
            logger.warning("Compaction skipped: fewer than TAIL_MIN session messages")
            return messages, new_start

        # 1. Determine tail (messages to preserve)
        tail_count = self._determine_tail(msgs)

        # 2. Identify messages to compact
        last_compaction_idx = self._find_last_compaction_idx(msgs)
        compact_start = last_compaction_idx if last_compaction_idx is not None else 0
        compact_end = len(msgs) - tail_count

        if compact_end <= compact_start:
            logger.warning("Compaction skipped: nothing to compact between boundaries")
            return messages, new_start

        to_compact = msgs[compact_start:compact_end]

        # 3. Pre-compact flush on a copy
        flushed = copy.deepcopy(to_compact)
        # Convert session messages to LLM format for flushing
        flushed_llm = []
        for m in flushed:
            llm_msg = {"role": m["role"], "content": m.get("content", "")}
            if "tool_calls" in m:
                llm_msg["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                llm_msg["tool_call_id"] = m["tool_call_id"]
            if "name" in m:
                llm_msg["name"] = m["name"]
            flushed_llm.append(llm_msg)
        CacheManager.flush_for_compaction(flushed_llm, context_mode)

        # 4. Extract previous compaction summary if present
        prev_compaction = None
        if last_compaction_idx is not None:
            prev_compaction = flushed_llm[0]
            flushed_llm = flushed_llm[1:]

        # 5. Format input for compactor
        text = self._format_compaction_input(flushed_llm, prev_compaction)

        # 6. Call LLM with compaction prompt
        try:
            compaction_messages = [
                {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ]
            response = await self.provider.chat(
                messages=compaction_messages,
                tools=None,
                model=self.model,
                temperature=0.3,
            )
            summary = response.content
        except Exception as e:
            logger.warning(f"Compaction failed (LLM error): {e}")
            return messages, new_start

        if not summary or not summary.strip():
            logger.warning("Compaction skipped: empty summary from LLM")
            return messages, new_start

        logger.info(
            f"Compaction triggered ({context_mode}): "
            f"compacted {len(to_compact)} messages into summary "
            f"({len(summary)} chars)"
        )

        # 7. Inject compaction message into session
        self._inject_compaction(session, summary, tail_count, context_mode)

        # 8. Session is saved by the caller (agent loop) after the turn completes.

        # 9. Rebuild LLM messages from compacted session
        current_turn = messages[new_start:]
        history = session.get_history()
        base_messages = context_builder.build_messages(
            history=history,
            session_key=session.key,
            **build_kwargs,
        )
        new_messages = base_messages + current_turn
        new_new_start = len(base_messages)

        return new_messages, new_new_start

    def _determine_tail(self, session_messages: list[dict]) -> int:
        """Return the number of tail messages to preserve.

        After counting, validates boundary parity: if the first message
        of the tail is a tool response (orphaned from its tool_call),
        extends the tail backwards to include the preceding assistant
        message with tool_calls.
        """
        max_tail_tokens = int(self.max_context_tokens * self.TAIL_TOKEN_RATIO)
        tail_count = min(self.TAIL_MIN, len(session_messages))

        # Count tokens for initial tail
        tail_tokens = sum(
            estimate_tokens(m.get("content", "") or "") + 4
            for m in session_messages[-tail_count:]
        )

        # Grow tail while under limits
        while tail_count < self.TAIL_MAX and tail_count < len(session_messages):
            next_idx = -(tail_count + 1)
            if abs(next_idx) > len(session_messages):
                break
            next_msg = session_messages[next_idx]
            next_tokens = estimate_tokens(next_msg.get("content", "") or "") + 4
            if tail_tokens + next_tokens > max_tail_tokens:
                break
            tail_count += 1
            tail_tokens += next_tokens

        # Parity check: don't start tail on a tool response
        if tail_count < len(session_messages):
            first_tail = session_messages[-tail_count]
            if first_tail["role"] == "tool":
                # Walk backwards to find the assistant message with tool_calls
                while tail_count < len(session_messages):
                    tail_count += 1
                    check_msg = session_messages[-tail_count]
                    if check_msg["role"] == "assistant":
                        break

        return tail_count

    def _find_last_compaction_idx(self, messages: list[dict]) -> int | None:
        """Find index of the last compaction message in session.messages."""
        for i in range(len(messages) - 1, -1, -1):
            meta = messages[i].get("metadata", {})
            if meta.get("type") == "compaction":
                return i
        return None

    def _format_compaction_input(
        self, messages: list[dict], prev_compaction: dict | None
    ) -> str:
        """Format messages as text for compactor input."""
        parts = []

        if prev_compaction:
            parts.append(
                "=== PREVIOUS SUMMARY ===\n"
                + (prev_compaction.get("content", "") or "")
                + "\n"
            )

        parts.append("=== CONVERSATION ===\n")
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "") or ""

            if role == "user":
                parts.append(f"[user] {content}\n")
            elif role == "assistant":
                if msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args = fn.get("arguments", "")
                        if isinstance(args, dict):
                            args = json.dumps(args)
                        parts.append(f"[tool_call] {name}({args})\n")
                if content:
                    parts.append(f"[assistant] {content}\n")
            elif role == "tool":
                name = msg.get("name", "")
                parts.append(f"[tool_response:{name}] {content}\n")

        return "".join(parts)

    def _inject_compaction(
        self, session: Session, summary: str, tail_count: int, context_mode: str
    ) -> None:
        """Insert compaction message into session.messages before tail."""
        # Compute timestamp just before the tail starts
        tail_start_msg = session.messages[-tail_count]
        tail_ts = tail_start_msg.get("metadata", {}).get("timestamp")
        if tail_ts:
            try:
                ts_dt = datetime.fromisoformat(tail_ts) - timedelta(seconds=0.1)
                compaction_ts = ts_dt.isoformat()
            except (ValueError, TypeError):
                compaction_ts = datetime.now().isoformat()
        else:
            compaction_ts = datetime.now().isoformat()

        compaction_msg = {
            "role": "user",
            "content": f"[Conversation Summary]\n{summary}",
            "metadata": {
                "type": "compaction",
                "mode": context_mode,
                "compacted_at": datetime.now().isoformat(),
                "timestamp": compaction_ts,
            },
        }

        # Insert compaction marker before the tail
        insert_idx = len(session.messages) - tail_count
        session.messages.insert(insert_idx, compaction_msg)
