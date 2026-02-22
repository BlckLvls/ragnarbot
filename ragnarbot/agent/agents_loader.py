"""Agents loader for sub-agent definitions."""

import re
from dataclasses import dataclass
from pathlib import Path

BUILTIN_AGENTS_DIR = Path(__file__).parent.parent / "agents"


@dataclass
class AgentDefinition:
    """Parsed agent definition from an AGENT.md file."""

    name: str
    description: str
    model: str  # "default" or "provider/model"
    allowed_tools: str | list[str]  # "all" or explicit list
    allowed_skills: str | list[str]  # "all", "none", or explicit list
    body: str  # markdown instructions (frontmatter stripped)
    path: str  # filesystem path to AGENT.md


class AgentsLoader:
    """
    Loader for agent definitions.

    Agents are markdown files (AGENT.md) that define sub-agent types with
    specific instructions, model preferences, and tool access.
    """

    def __init__(self, workspace: Path, builtin_agents_dir: Path | None = None):
        self.workspace_agents = workspace / "agents"
        self.builtin_agents = builtin_agents_dir or BUILTIN_AGENTS_DIR

    def list_agents(self) -> list[dict[str, str]]:
        """
        List all available agents (workspace first, builtin fill-in).

        Returns:
            List of dicts with name, description, path, source.
        """
        agents: list[dict[str, str]] = []
        seen: set[str] = set()

        # Workspace agents (highest priority)
        if self.workspace_agents.exists():
            for agent_dir in sorted(self.workspace_agents.iterdir()):
                if agent_dir.is_dir():
                    agent_file = agent_dir / "AGENT.md"
                    if agent_file.exists():
                        name = agent_dir.name
                        meta = self._parse_frontmatter(agent_file.read_text(encoding="utf-8"))
                        agents.append({
                            "name": name,
                            "description": meta.get("description", name),
                            "path": str(agent_file),
                            "source": "workspace",
                        })
                        seen.add(name)

        # Built-in agents
        if self.builtin_agents and self.builtin_agents.exists():
            for agent_dir in sorted(self.builtin_agents.iterdir()):
                if agent_dir.is_dir():
                    agent_file = agent_dir / "AGENT.md"
                    if agent_file.exists() and agent_dir.name not in seen:
                        name = agent_dir.name
                        meta = self._parse_frontmatter(agent_file.read_text(encoding="utf-8"))
                        agents.append({
                            "name": name,
                            "description": meta.get("description", name),
                            "path": str(agent_file),
                            "source": "builtin",
                        })

        return agents

    def load_agent(self, name: str) -> AgentDefinition | None:
        """
        Load and parse an AGENT.md by name. Workspace wins over builtin.

        Args:
            name: Agent name (directory name).

        Returns:
            AgentDefinition or None if not found.
        """
        # Check workspace first
        workspace_file = self.workspace_agents / name / "AGENT.md"
        if workspace_file.exists():
            return self._parse_agent(workspace_file)

        # Check built-in
        if self.builtin_agents:
            builtin_file = self.builtin_agents / name / "AGENT.md"
            if builtin_file.exists():
                return self._parse_agent(builtin_file)

        return None

    def build_agents_summary(self) -> str:
        """
        Build an XML summary of all agents for the system prompt.

        Returns:
            XML-formatted agents summary, or empty string if no agents.
        """
        all_agents = self.list_agents()
        if not all_agents:
            return ""

        def escape_xml(s: str) -> str:
            return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        lines = ["<agents>"]
        for a in all_agents:
            lines.append(f'  <agent source="{a["source"]}">')
            lines.append(f"    <name>{escape_xml(a['name'])}</name>")
            lines.append(f"    <description>{escape_xml(a['description'])}</description>")
            lines.append("  </agent>")
        lines.append("</agents>")

        return "\n".join(lines)

    def _parse_agent(self, path: Path) -> AgentDefinition:
        """Parse an AGENT.md file into an AgentDefinition."""
        content = path.read_text(encoding="utf-8")
        meta = self._parse_frontmatter(content)
        body = self._strip_frontmatter(content)

        # Parse allowedTools
        allowed_raw = meta.get("allowedTools", "all")
        if allowed_raw == "all":
            allowed_tools: str | list[str] = "all"
        elif allowed_raw.startswith("[") and allowed_raw.endswith("]"):
            # Parse YAML-style list: [tool1, tool2, tool3]
            inner = allowed_raw[1:-1]
            allowed_tools = [t.strip() for t in inner.split(",") if t.strip()]
        else:
            allowed_tools = allowed_raw

        # Parse allowedSkills
        skills_raw = meta.get("allowedSkills", "none")
        if skills_raw in ("all", "none"):
            allowed_skills: str | list[str] = skills_raw
        elif skills_raw.startswith("[") and skills_raw.endswith("]"):
            inner = skills_raw[1:-1]
            allowed_skills = [s.strip() for s in inner.split(",") if s.strip()]
        else:
            allowed_skills = skills_raw

        return AgentDefinition(
            name=meta.get("name", path.parent.name),
            description=meta.get("description", ""),
            model=meta.get("model", "default"),
            allowed_tools=allowed_tools,
            allowed_skills=allowed_skills,
            body=body,
            path=str(path),
        )

    def _parse_frontmatter(self, content: str) -> dict[str, str]:
        """Parse YAML frontmatter into a dict."""
        if not content.startswith("---"):
            return {}

        match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
        if not match:
            return {}

        metadata: dict[str, str] = {}
        for line in match.group(1).split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                metadata[key.strip()] = value.strip().strip("\"'")
        return metadata

    def _strip_frontmatter(self, content: str) -> str:
        """Remove YAML frontmatter from markdown content."""
        if content.startswith("---"):
            match = re.match(r"^---\n.*?\n---\n", content, re.DOTALL)
            if match:
                return content[match.end():].strip()
        return content
