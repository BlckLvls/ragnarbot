"""Profile-aware instance resolution and runtime state helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_ENV_VAR = "RAGNARBOT_PROFILE"
DEFAULT_PROFILE = "default"
ROOT_NAME = ".ragnarbot"
ROOT_PREFIX = f"{ROOT_NAME}-"
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True)
class InstanceInfo:
    """Resolved paths and labels for the active profile."""

    profile: str
    runtime_name: str
    data_root: Path
    config_path: Path
    credentials_path: Path
    workspace_path: Path
    sessions_path: Path
    media_path: Path
    oauth_dir: Path
    browser_profile_path: Path
    browser_screenshots_path: Path
    cron_dir: Path
    cron_logs_path: Path
    log_dir: Path
    fallback_state_path: Path
    pending_grants_path: Path
    restart_marker_path: Path
    update_marker_path: Path
    pid_path: Path
    gateway_claim_path: Path
    runtime_state_path: Path
    pending_update_path: Path
    metadata_path: Path


class GatewayClaimError(RuntimeError):
    """Raised when a profile-local gateway ownership claim cannot be acquired."""

    def __init__(self, message: str, *, pid: int | None = None, profile: str | None = None):
        super().__init__(message)
        self.pid = pid
        self.profile = profile


def normalize_profile_name(profile: str | None) -> str:
    """Normalize and validate a profile name."""
    raw = DEFAULT_PROFILE if profile is None else profile
    normalized = raw.strip().lower()
    if not normalized:
        raise ValueError("Profile name cannot be empty")
    if "/" in normalized or "\\" in normalized or "." in normalized:
        raise ValueError("Profile name may not contain path separators or dots")
    if not _PROFILE_RE.fullmatch(normalized):
        raise ValueError(
            "Profile name must start with a letter or digit and contain only "
            "letters, digits, '-', or '_'"
        )
    return normalized


def resolve_active_profile(profile: str | None = None) -> str:
    """Resolve the effective profile from argument, env var, or default."""
    if profile is not None:
        return normalize_profile_name(profile)
    return normalize_profile_name(os.environ.get(PROFILE_ENV_VAR, DEFAULT_PROFILE))


def set_active_profile(profile: str | None) -> str:
    """Bind the process to a specific profile."""
    resolved = resolve_active_profile(profile)
    os.environ[PROFILE_ENV_VAR] = resolved
    return resolved


def runtime_name(profile: str | None = None) -> str:
    """Return the user-facing instance name."""
    resolved = resolve_active_profile(profile)
    return "ragnarbot" if resolved == DEFAULT_PROFILE else f"ragnarbot-{resolved}"


def data_root_for_profile(profile: str | None = None) -> Path:
    """Return the profile-specific data root."""
    resolved = resolve_active_profile(profile)
    if resolved == DEFAULT_PROFILE:
        return Path.home() / ROOT_NAME
    return Path.home() / f"{ROOT_PREFIX}{resolved}"


def workspace_config_value(profile: str | None = None) -> str:
    """Return the default workspace value as a tilde path."""
    resolved = resolve_active_profile(profile)
    if resolved == DEFAULT_PROFILE:
        return f"~/{ROOT_NAME}/workspace"
    return f"~/{ROOT_PREFIX}{resolved}/workspace"


def resolve_workspace_path(
    workspace: str | Path | None = None,
    profile: str | None = None,
) -> Path:
    """Resolve a configured workspace path for the active profile."""
    info = get_instance(profile)
    if workspace is None:
        return info.workspace_path.resolve()

    resolved = Path(workspace).expanduser()
    if not resolved.is_absolute():
        resolved = info.data_root / resolved
    return resolved.resolve()


def get_instance(profile: str | None = None) -> InstanceInfo:
    """Build the full instance descriptor for a profile."""
    resolved = resolve_active_profile(profile)
    data_root = data_root_for_profile(resolved)
    return InstanceInfo(
        profile=resolved,
        runtime_name=runtime_name(resolved),
        data_root=data_root,
        config_path=data_root / "config.json",
        credentials_path=data_root / "credentials.json",
        workspace_path=data_root / "workspace",
        sessions_path=data_root / "sessions",
        media_path=data_root / "media",
        oauth_dir=data_root / "oauth",
        browser_profile_path=data_root / "browser-profile",
        browser_screenshots_path=data_root / "browser-screenshots",
        cron_dir=data_root / "cron",
        cron_logs_path=data_root / "cron" / "logs",
        log_dir=data_root / "logs",
        fallback_state_path=data_root / "fallback_state.json",
        pending_grants_path=data_root / "pending_grants.json",
        restart_marker_path=data_root / ".restart_marker",
        update_marker_path=data_root / ".update_marker",
        pid_path=data_root / "gateway.pid",
        gateway_claim_path=data_root / "gateway.claim.json",
        runtime_state_path=data_root / "runtime_state.json",
        pending_update_path=data_root / "pending_update.json",
        metadata_path=data_root / "instance.json",
    )


def ensure_instance_root(profile: str | None = None) -> InstanceInfo:
    """Ensure the instance data root exists and contains metadata."""
    info = get_instance(profile)
    info.data_root.mkdir(parents=True, exist_ok=True)
    if not info.metadata_path.exists():
        info.metadata_path.write_text(json.dumps({
            "profile": info.profile,
            "runtime_name": info.runtime_name,
        }, indent=2))
    return info


def get_runtime_state(profile: str | None = None) -> dict[str, Any]:
    """Load persisted runtime state for a profile."""
    path = get_instance(profile).runtime_state_path
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def load_gateway_claim(profile: str | None = None) -> dict[str, Any] | None:
    """Load the persisted gateway ownership claim for a profile."""
    path = get_instance(profile).gateway_claim_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_runtime_state(state: dict[str, Any], profile: str | None = None) -> None:
    """Persist runtime state for a profile."""
    info = ensure_instance_root(profile)
    info.runtime_state_path.write_text(json.dumps(state, indent=2))


def update_runtime_state(profile: str | None = None, **changes: Any) -> dict[str, Any]:
    """Merge changes into runtime state and persist the result."""
    state = get_runtime_state(profile)
    state.update(changes)
    save_runtime_state(state, profile)
    return state


def clear_runtime_state_keys(*keys: str, profile: str | None = None) -> dict[str, Any]:
    """Delete keys from runtime state and persist the result."""
    state = get_runtime_state(profile)
    for key in keys:
        state.pop(key, None)
    save_runtime_state(state, profile)
    return state


def load_pending_update(profile: str | None = None) -> dict[str, Any] | None:
    """Load the pending-update payload for a profile."""
    path = get_instance(profile).pending_update_path
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_pending_update(payload: dict[str, Any], profile: str | None = None) -> None:
    """Persist the pending-update payload for a profile."""
    info = ensure_instance_root(profile)
    info.pending_update_path.write_text(json.dumps(payload, indent=2))
    update_runtime_state(profile, pending_update=payload)


def clear_pending_update(profile: str | None = None) -> None:
    """Remove the pending-update payload for a profile."""
    info = get_instance(profile)
    info.pending_update_path.unlink(missing_ok=True)
    clear_runtime_state_keys("pending_update", profile=profile)


def pending_update_target(
    payload: dict[str, Any] | None,
) -> tuple[str | None, str | None]:
    """Return the persisted target chat for a pending update payload."""
    if not payload:
        return None, None
    return payload.get("target_channel"), payload.get("target_chat_id")


def bind_pending_update_target(
    channel: str,
    chat_id: str,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Bind a pending update payload to a concrete delivery target."""
    payload = load_pending_update(profile)
    if not payload:
        return None
    payload["target_channel"] = channel
    payload["target_chat_id"] = chat_id
    save_pending_update(payload, profile)
    return payload


def is_pid_running(pid: int | None) -> bool:
    """Return True when the PID appears to belong to a live process."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_process_cmdline(pid: int) -> list[str]:
    """Return the best-effort command line tokens for a PID."""
    if not pid or pid <= 0:
        return []

    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    if proc_cmdline.exists():
        try:
            raw = proc_cmdline.read_bytes()
        except OSError:
            raw = b""
        if raw:
            return [part.decode(errors="ignore") for part in raw.split(b"\0") if part]

    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return []

    command = result.stdout.strip()
    if not command:
        return []
    return command.split()


def _tokens_look_like_ragnarbot(tokens: list[str]) -> bool:
    """Return True when command tokens appear to launch RagnarBot."""
    for index, token in enumerate(tokens):
        if token == "ragnarbot" or token.endswith("/ragnarbot"):
            return True
        if token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == "ragnarbot":
            return True
    return False


def _profile_flag_from_tokens(tokens: list[str]) -> str | None:
    """Extract the explicit --profile value from command tokens, if any."""
    for index, token in enumerate(tokens):
        if token == "--profile" and index + 1 < len(tokens):
            return tokens[index + 1]
    return None


def gateway_process_matches(
    pid: int | None,
    profile: str | None = None,
    runtime_role: str = "gateway",
) -> bool:
    """Return True when a live PID appears to be this profile's gateway."""
    if not is_pid_running(pid):
        return False

    tokens = read_process_cmdline(pid)
    if not tokens:
        return False
    if runtime_role not in tokens:
        return False
    if not _tokens_look_like_ragnarbot(tokens):
        return False

    resolved = resolve_active_profile(profile)
    explicit_profile = _profile_flag_from_tokens(tokens)
    if explicit_profile is None:
        return resolved == DEFAULT_PROFILE
    return explicit_profile == resolved


def _clear_gateway_artifacts(profile: str | None = None) -> None:
    """Remove claim/pid files and clear runtime pid bookkeeping."""
    info = get_instance(profile)
    info.gateway_claim_path.unlink(missing_ok=True)
    info.pid_path.unlink(missing_ok=True)
    clear_runtime_state_keys("pid", "runtime_role", profile=profile)


def get_live_gateway_claim(
    profile: str | None = None,
    *,
    cleanup_stale: bool = True,
) -> dict[str, Any] | None:
    """Return the live validated gateway claim for a profile, if any."""
    resolved = resolve_active_profile(profile)
    claim = load_gateway_claim(resolved)
    if not claim:
        if cleanup_stale:
            _clear_gateway_artifacts(resolved)
        return None

    pid = claim.get("pid")
    claim_profile = claim.get("profile")
    runtime_role = claim.get("runtime_role", "gateway")
    if claim_profile != resolved or runtime_role != "gateway":
        if cleanup_stale:
            _clear_gateway_artifacts(resolved)
        return None

    if not gateway_process_matches(pid, resolved, runtime_role):
        if cleanup_stale:
            _clear_gateway_artifacts(resolved)
        return None

    return claim


def get_live_gateway_pid(profile: str | None = None) -> int | None:
    """Return the validated live gateway PID for a profile, if any."""
    claim = get_live_gateway_claim(profile)
    pid = claim.get("pid") if claim else None
    return pid if isinstance(pid, int) else None


def acquire_gateway_claim(
    profile: str | None = None,
    *,
    pid: int | None = None,
    runtime_role: str = "gateway",
) -> dict[str, Any]:
    """Atomically claim ownership of the profile-local gateway slot."""
    info = ensure_instance_root(profile)
    current_pid = pid or os.getpid()
    claim = {
        "pid": current_pid,
        "profile": info.profile,
        "runtime_role": runtime_role,
    }

    for _ in range(3):
        try:
            fd = os.open(
                info.gateway_claim_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            existing = get_live_gateway_claim(info.profile, cleanup_stale=True)
            if existing is None:
                continue
            existing_pid = existing.get("pid")
            if existing_pid == current_pid:
                info.pid_path.write_text(str(current_pid))
                update_runtime_state(info.profile, pid=current_pid, runtime_role=runtime_role)
                return existing
            raise GatewayClaimError(
                "Gateway is already running for this profile.",
                pid=existing_pid if isinstance(existing_pid, int) else None,
                profile=info.profile,
            )
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(claim, handle, indent=2)
            info.pid_path.write_text(str(current_pid))
            update_runtime_state(info.profile, pid=current_pid, runtime_role=runtime_role)
            return claim

    raise GatewayClaimError(
        "Could not acquire gateway claim for this profile.",
        profile=info.profile,
    )


def release_gateway_claim(
    profile: str | None = None,
    *,
    pid: int | None = None,
) -> bool:
    """Release the gateway claim when it is owned by this PID or stale."""
    info = get_instance(profile)
    current_pid = pid or os.getpid()
    claim = load_gateway_claim(info.profile)
    if claim:
        claim_pid = claim.get("pid")
        if (
            isinstance(claim_pid, int)
            and claim_pid != current_pid
            and gateway_process_matches(claim_pid, info.profile, claim.get("runtime_role", "gateway"))
        ):
            return False

    _clear_gateway_artifacts(info.profile)
    return True


def signal_live_gateway(sig: int, profile: str | None = None) -> int | None:
    """Signal the validated live gateway process for a profile."""
    claim = get_live_gateway_claim(profile)
    if not claim:
        return None

    pid = claim.get("pid")
    if not isinstance(pid, int):
        _clear_gateway_artifacts(profile)
        return None

    try:
        os.kill(pid, sig)
    except OSError:
        _clear_gateway_artifacts(profile)
        return None
    return pid


def record_process_start(pid: int, started_version: str, profile: str | None = None) -> None:
    """Persist runtime details for a running gateway."""
    update_runtime_state(
        profile,
        pid=pid,
        started_version=started_version,
        runtime_role="gateway",
    )


def record_process_stop(profile: str | None = None) -> None:
    """Mark a gateway as no longer running while keeping historical metadata."""
    clear_runtime_state_keys("pid", "runtime_role", profile=profile)


def record_last_active_chat(channel: str, chat_id: str, profile: str | None = None) -> None:
    """Persist the most recent non-CLI chat for a profile."""
    update_runtime_state(
        profile,
        last_active_channel=channel,
        last_active_chat_id=chat_id,
    )


def last_active_chat(profile: str | None = None) -> tuple[str | None, str | None]:
    """Return the persisted last active chat tuple."""
    state = get_runtime_state(profile)
    return state.get("last_active_channel"), state.get("last_active_chat_id")


def instance_profiles_on_disk() -> list[str]:
    """List profiles that already have data roots on disk."""
    profiles: set[str] = set()
    home = Path.home()
    for path in home.iterdir():
        if not path.is_dir():
            continue
        if path.name == ROOT_NAME:
            profiles.add(DEFAULT_PROFILE)
            continue
        if not path.name.startswith(ROOT_PREFIX):
            continue
        profile = path.name[len(ROOT_PREFIX):]
        try:
            profiles.add(normalize_profile_name(profile))
        except ValueError:
            continue
    return sorted(profiles)


def running_instance_profiles() -> list[str]:
    """List profiles whose recorded PID still appears alive."""
    running: list[str] = []
    for profile in instance_profiles_on_disk():
        if get_live_gateway_pid(profile):
            running.append(profile)
    return sorted(running)


def instance_name_for_service(profile: str | None = None) -> str:
    """Return the suffix-safe profile name for service labels."""
    resolved = resolve_active_profile(profile)
    return "" if resolved == DEFAULT_PROFILE else resolved


def tilde_path(path: Path) -> str:
    """Render a path relative to the user's home directory when possible."""
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)
