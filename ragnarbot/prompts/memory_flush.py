"""System prompt loaders for background memory flushing."""

from ragnarbot.prompts.loader import load_prompt


DAILY_MEMORY_SYSTEM_PROMPT = load_prompt("memory_daily_system_prompt.txt")
LONG_TERM_MEMORY_SYSTEM_PROMPT = load_prompt("memory_long_term_system_prompt.txt")
