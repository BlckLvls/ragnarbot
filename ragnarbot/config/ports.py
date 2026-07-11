"""Stable profile-aware network port assignment."""

import json
from typing import Any

from ragnarbot.instance import DEFAULT_PROFILE, get_instance, instance_profiles_on_disk
from ragnarbot.web.runtime import port_is_available

DEFAULT_PROFILE_BASE_PORT = 18790
PROFILE_PORT_STRIDE = 10
MAX_PROFILE_SLOTS = 100


def _configured_ports(*, excluding_profile: str) -> set[int]:
    ports: set[int] = set()
    for profile in instance_profiles_on_disk():
        if profile == excluding_profile:
            continue
        path = get_instance(profile).config_path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for section in ("gateway", "hooks", "web"):
            value = data.get(section, {}).get("port")
            if isinstance(value, int):
                ports.add(value)
    return ports


def assign_profile_ports(config: Any, *, profile: str | None = None) -> Any:
    """Assign a stable free port block to a new non-default profile config."""
    info = get_instance(profile)
    if info.profile == DEFAULT_PROFILE:
        return config

    used = _configured_ports(excluding_profile=info.profile)
    for slot in range(1, MAX_PROFILE_SLOTS + 1):
        base = DEFAULT_PROFILE_BASE_PORT + slot * PROFILE_PORT_STRIDE
        gateway_port, hooks_port, web_port = base, base + 1, base + 2
        candidates = {gateway_port, hooks_port, web_port}
        if candidates & used:
            continue
        if not port_is_available(config.gateway.host, hooks_port):
            continue
        if not port_is_available(config.web.host, web_port):
            continue
        config.gateway.port = gateway_port
        config.hooks.port = hooks_port
        config.web.port = web_port
        return config

    raise RuntimeError("could not allocate a free ragnarbot profile port block")
