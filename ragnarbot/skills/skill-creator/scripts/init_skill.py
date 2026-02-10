#!/usr/bin/env python3
"""Initialize a new skill directory with SKILL.md template and optional resource dirs."""

import argparse
import re
import sys
from pathlib import Path

DEFAULT_PATH = Path.home() / ".ragnarbot" / "workspace" / "skills"
VALID_RESOURCES = {"scripts", "references", "assets"}
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def validate_name(name: str) -> str:
    if len(name) > 64:
        raise argparse.ArgumentTypeError(f"name too long ({len(name)} chars, max 64)")
    if not NAME_RE.match(name):
        raise argparse.ArgumentTypeError(
            f"invalid name '{name}': use lowercase alphanumeric and hyphens (e.g. my-skill)"
        )
    return name


def parse_resources(value: str) -> list[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    for p in parts:
        if p not in VALID_RESOURCES:
            raise argparse.ArgumentTypeError(
                f"unknown resource '{p}': choose from {', '.join(sorted(VALID_RESOURCES))}"
            )
    return parts


SKILL_TEMPLATE = """\
---
name: {name}
description: TODO — describe what this skill does and when to use it.
---

# {title}

TODO — write instructions for using this skill.

## Workflow

1. TODO

## Bundled Resources

{resources_section}
"""

RESOURCES_NONE = "This skill has no bundled resources yet. Add `scripts/`, `references/`, or `assets/` as needed."

RESOURCE_DESCRIPTIONS = {
    "scripts": "- **scripts/** — executable code (Python/Bash) for deterministic, repeatable tasks",
    "references": "- **references/** — documentation loaded into context on demand",
    "assets": "- **assets/** — files used in output (templates, images, fonts, etc.)",
}


def build_resources_section(resources: list[str]) -> str:
    if not resources:
        return RESOURCES_NONE
    return "\n".join(RESOURCE_DESCRIPTIONS[r] for r in sorted(resources))


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize a new skill.")
    parser.add_argument("name", type=validate_name, help="Skill name (kebab-case)")
    parser.add_argument(
        "--path", type=Path, default=DEFAULT_PATH, help=f"Parent directory (default: {DEFAULT_PATH})"
    )
    parser.add_argument(
        "--resources", type=parse_resources, default=[], help="Comma-separated: scripts,references,assets"
    )
    args = parser.parse_args()

    skill_dir = args.path / args.name
    if skill_dir.exists():
        print(f"Error: {skill_dir} already exists", file=sys.stderr)
        sys.exit(1)

    skill_dir.mkdir(parents=True)

    title = args.name.replace("-", " ").title()
    content = SKILL_TEMPLATE.format(
        name=args.name,
        title=title,
        resources_section=build_resources_section(args.resources),
    )
    (skill_dir / "SKILL.md").write_text(content)

    for res in args.resources:
        (skill_dir / res).mkdir()

    print(f"Created skill '{args.name}' at {skill_dir}")
    print(f"  SKILL.md")
    for res in sorted(args.resources):
        print(f"  {res}/")


if __name__ == "__main__":
    main()
