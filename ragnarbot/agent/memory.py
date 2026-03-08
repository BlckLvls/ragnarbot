"""Memory system for persistent agent memory."""

from datetime import datetime, timedelta
from pathlib import Path

from ragnarbot.utils.helpers import ensure_dir, today_date


class MemoryStore:
    """
    Memory system for the agent.
    
    Supports daily notes (memory/YYYY-MM-DD.md) and long-term memory (MEMORY.md).
    """
    
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"

    def get_day_file(self, date_str: str) -> Path:
        """Get path to a daily memory file."""
        return self.memory_dir / f"{date_str}.md"

    def get_today_file(self) -> Path:
        """Get path to today's memory file."""
        return self.get_day_file(today_date())

    def get_yesterday_file(self) -> Path:
        """Get path to yesterday's memory file."""
        yesterday = (datetime.now().date() - timedelta(days=1)).strftime("%Y-%m-%d")
        return self.get_day_file(yesterday)

    def read_day(self, date_str: str) -> str:
        """Read a daily memory file."""
        day_file = self.get_day_file(date_str)
        if day_file.exists():
            return day_file.read_text(encoding="utf-8")
        return ""

    def read_today(self) -> str:
        """Read today's memory notes."""
        return self.read_day(today_date())

    def read_yesterday(self) -> str:
        """Read yesterday's memory notes."""
        return self.read_day(self.get_yesterday_file().stem)

    def write_day(self, date_str: str, content: str) -> None:
        """Write the full contents of a daily memory file."""
        self.get_day_file(date_str).write_text(content, encoding="utf-8")

    def append_today(self, content: str) -> None:
        """Append content to today's memory notes."""
        today_file = self.get_today_file()
        
        if today_file.exists():
            existing = today_file.read_text(encoding="utf-8")
            content = existing + "\n" + content
        else:
            # Add header for new day
            header = f"# {today_date()}\n\n"
            content = header + content
        
        today_file.write_text(content, encoding="utf-8")
    
    def read_long_term(self) -> str:
        """Read long-term memory (MEMORY.md)."""
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""
    
    def write_long_term(self, content: str) -> None:
        """Write to long-term memory (MEMORY.md)."""
        self.memory_file.write_text(content, encoding="utf-8")
    
    def get_recent_memories(self, days: int = 7) -> str:
        """
        Get memories from the last N days.
        
        Args:
            days: Number of days to look back.
        
        Returns:
            Combined memory content.
        """
        memories = []
        today = datetime.now().date()
        
        for i in range(days):
            date = today - timedelta(days=i)
            date_str = date.strftime("%Y-%m-%d")
            file_path = self.get_day_file(date_str)
            
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                memories.append(content)
        
        return "\n\n---\n\n".join(memories)
    
    def list_memory_files(self) -> list[Path]:
        """List all memory files sorted by date (newest first)."""
        if not self.memory_dir.exists():
            return []
        
        files = list(self.memory_dir.glob("????-??-??.md"))
        return sorted(files, reverse=True)
    
    def get_memory_context(self) -> str:
        """
        Get memory context for the agent.
        
        Returns:
            Formatted memory context including long-term and recent memories.
        """
        parts = []
        
        # Long-term memory
        long_term = self.read_long_term()
        if long_term:
            parts.append("## Long-term Memory\n" + long_term)
        
        # Today's notes
        today = self.read_today()
        if today:
            parts.append("## Today's Notes\n" + today)

        yesterday = self.read_yesterday()
        if yesterday:
            parts.append("## Yesterday's Notes\n" + yesterday)
        
        return "\n\n".join(parts) if parts else ""
