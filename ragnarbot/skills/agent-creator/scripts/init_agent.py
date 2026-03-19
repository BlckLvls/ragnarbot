#!/usr/bin/env python3
"""Initialize a new agent directory with AGENT.md template."""

import argparse
import re
import sys
from pathlib import Path

from ragnarbot.instance import get_instance

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def default_path() -> Path:
    """Resolve the active profile's default agent directory."""
    return get_instance().workspace_path / "agents"

SAFE_TOOLS = [
    "file_read", "file_write", "file_edit", "list_dir",
    "exec", "web_search", "web_fetch", "browser",
    "exec_bg", "poll", "output", "kill", "dismiss",
]


def validate_name(name: str) -> str:
    if len(name) > 64:
        raise argparse.ArgumentTypeError(f"name too long ({len(name)} chars, max 64)")
    if not NAME_RE.match(name):
        raise argparse.ArgumentTypeError(
            f"invalid name '{name}': use lowercase alphanumeric and hyphens (e.g. my-agent)"
        )
    return name


AGENT_TEMPLATE = """\
---
name: {name}
description: TODO — describe what this agent does and when to spawn it.
model: default
reasoningLevel: inherit
allowedTools: all
---

# {title}

You are a TODO. Your job is to TODO.

## Process

1. TODO

## Output Format

TODO — describe the structure of your deliverable.
"""


def main() -> None:
    default = default_path()
    parser = argparse.ArgumentParser(description="Initialize a new agent definition.")
    parser.add_argument("name", type=validate_name, help="Agent name (kebab-case)")
    parser.add_argument(
        "--path", type=Path, default=default,
        help=f"Parent directory (default: {default})",
    )
    args = parser.parse_args()

    agent_dir = args.path / args.name
    if agent_dir.exists():
        print(f"Error: {agent_dir} already exists", file=sys.stderr)
        sys.exit(1)

    agent_dir.mkdir(parents=True)

    title = args.name.replace("-", " ").title()
    content = AGENT_TEMPLATE.format(name=args.name, title=title)
    (agent_dir / "AGENT.md").write_text(content)

    print(f"Created agent '{args.name}' at {agent_dir}")
    print("  AGENT.md")
    print()
    print("Available tools for allowedTools:")
    print(f"  {', '.join(SAFE_TOOLS)}")
    print()
    print("Use 'all' for all safe tools, '[]' for no tools, or pick specific ones:")
    print("  allowedTools: [web_search, web_fetch, file_read]")


if __name__ == "__main__":
    main()
