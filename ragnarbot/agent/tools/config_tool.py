"""Config tool for viewing and modifying bot configuration."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ragnarbot.agent.tools.base import Tool

if TYPE_CHECKING:
    from ragnarbot.agent.loop import AgentLoop


class ConfigTool(Tool):
    """Tool to view and modify bot configuration at runtime."""

    name = "config"
    description = (
        "View and modify bot configuration and secrets. "
        "Actions: schema (discover fields), get (read value), set (change value), "
        "list (all current values), diff (non-default values). "
        "Use 'secrets.*' paths to manage credentials (e.g. secrets.providers.anthropic.api_key)."
    )

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["schema", "get", "set", "list", "diff"],
                "description": "Action to perform",
            },
            "path": {
                "type": "string",
                "description": (
                    "Dot-notation path (e.g. 'agents.defaults.model'). For schema/get/set. "
                    "Use 'secrets.*' prefix for credentials (e.g. 'secrets.providers.anthropic.api_key')."
                ),
            },
            "value": {
                "type": "string",
                "description": "New value to set (for 'set' action). Strings are auto-coerced to the target type.",
            },
        },
        "required": ["action"],
    }

    def __init__(self, agent: AgentLoop):
        self._agent = agent

    async def execute(
        self,
        action: str,
        path: str | None = None,
        value: str | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "schema":
            return self._action_schema(path)
        elif action == "get":
            return self._action_get(path)
        elif action == "set":
            return self._action_set(path, value)
        elif action == "list":
            return self._action_list()
        elif action == "diff":
            return self._action_diff()
        return f"Unknown action: {action}"

    def _action_schema(self, path: str | None) -> str:
        from ragnarbot.agent.tools.secrets_helpers import secrets_schema
        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_all_paths, get_field_meta
        from ragnarbot.config.schema import Config

        # Secrets-only schema
        if path and path.startswith("secrets"):
            creds = load_credentials()
            filter_path = path if path != "secrets" else None
            return secrets_schema(creds, filter_path)

        from ragnarbot.config.providers import PROVIDERS

        config = load_config()
        all_paths = get_all_paths(config)
        model_fields = {"agents.defaults.model", "agents.fallback.model"}
        all_model_ids = [m["id"] for p in PROVIDERS for m in p["models"]]

        lines = []
        for p in sorted(all_paths.keys()):
            if path and not p.startswith(path):
                continue
            try:
                meta = get_field_meta(Config, p)
                reload_tag = f" [{meta['reload']}]" if meta.get("reload") else ""
                pattern = f" pattern={meta['pattern']}" if meta.get("pattern") else ""
                label = f" — {meta['label']}" if meta.get("label") else ""
                lines.append(
                    f"{p}: {meta['type']} = {meta['default']!r}{reload_tag}{pattern}{label}"
                )
                if p in model_fields:
                    lines.append(f"  choices: {', '.join(all_model_ids)}")
            except ValueError:
                lines.append(f"{p}: (metadata unavailable)")

        if not lines and path:
            return f"No fields matching '{path}'"

        # Append secrets schema when no path filter
        if not path:
            creds = load_credentials()
            sec = secrets_schema(creds)
            if sec:
                lines.append("")
                lines.append(sec)

        if not lines:
            return "No fields found"
        return "\n".join(lines)

    def _action_get(self, path: str | None) -> str:
        if not path:
            return "Error: 'path' is required for get action"

        if path.startswith("secrets."):
            from ragnarbot.agent.tools.secrets_helpers import secrets_get
            from ragnarbot.auth.credentials import load_credentials

            creds = load_credentials()
            return secrets_get(creds, path[8:])

        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_by_path, get_field_meta
        from ragnarbot.config.schema import Config

        try:
            config = load_config()
            value = get_by_path(config, path)
            meta = get_field_meta(Config, path)
            return json.dumps({
                "path": path,
                "value": value,
                "type": meta.get("type", "unknown"),
                "reload": meta.get("reload", "unknown"),
                "label": meta.get("label", ""),
            }, default=str)
        except ValueError as e:
            return f"Error: {e}"

    def _action_set(self, path: str | None, value: str | None) -> str:
        if not path:
            return "Error: 'path' is required for set action"
        if value is None:
            return "Error: 'value' is required for set action"

        if path.startswith("secrets."):
            from ragnarbot.agent.tools.secrets_helpers import secrets_set
            from ragnarbot.auth.credentials import load_credentials, save_credentials

            creds = load_credentials()
            creds, result_str = secrets_set(creds, path[8:], value)
            if not result_str.startswith("Error"):
                save_credentials(creds)
            return result_str

        from ragnarbot.agent.tools.secrets_helpers import check_config_dependency
        from ragnarbot.config.loader import load_config, save_config
        from ragnarbot.config.path_utils import get_by_path, get_field_meta, set_by_path
        from ragnarbot.config.schema import Config

        try:
            config = load_config()
            old_value = get_by_path(config, path)
            set_by_path(config, path, value)
            new_value = get_by_path(config, path)

            # Validate model against known provider models
            if path in ("agents.defaults.model", "agents.fallback.model"):
                from ragnarbot.config.providers import PROVIDERS

                all_model_ids = [
                    m["id"] for p in PROVIDERS for m in p["models"]
                ]
                if new_value not in all_model_ids:
                    set_by_path(config, path, old_value)
                    models_list = ", ".join(all_model_ids)
                    return f"Error: model '{new_value}' is not available. Choose from: {models_list}"

            # Validate auth_method
            if path in ("agents.defaults.auth_method", "agents.fallback.auth_method"):
                if new_value not in ("api_key", "oauth"):
                    set_by_path(config, path, old_value)
                    return f"Error: unknown auth method '{new_value}'. Choose: api_key, oauth"

                # For fallback, resolve the model from fallback config
                if path == "agents.fallback.auth_method":
                    fb_model = config.agents.fallback.model
                else:
                    fb_model = config.agents.defaults.model

                if fb_model and new_value == "oauth":
                    from ragnarbot.config.schema import OAUTH_SUPPORTED_PROVIDERS
                    provider_name = fb_model.split("/")[0] if "/" in fb_model else "anthropic"
                    if provider_name not in OAUTH_SUPPORTED_PROVIDERS:
                        set_by_path(config, path, old_value)
                        return (
                            f"Error: OAuth is not supported for provider "
                            f"'{provider_name}'. "
                            f"Supported: {', '.join(sorted(OAUTH_SUPPORTED_PROVIDERS))}"
                        )

            # Cross-validate fallback model+auth_method pair
            if path == "agents.fallback.model" and new_value:
                fb_auth = config.agents.fallback.auth_method
                error = self._validate_provider_auth(new_value, fb_auth)
                if error:
                    set_by_path(config, path, old_value)
                    return f"Error: {error}"

            if path == "agents.fallback.auth_method":
                fb_model = config.agents.fallback.model
                if fb_model:
                    error = self._validate_provider_auth(fb_model, new_value)
                    if error:
                        set_by_path(config, path, old_value)
                        return f"Error: {error}"

            # Check credential dependencies before persisting
            dep_error = check_config_dependency(path, str(new_value))
            if dep_error:
                # Rollback: restore old value
                set_by_path(config, path, old_value)
                return f"Error: {dep_error}"

            save_config(config)
            meta = get_field_meta(Config, path)
            reload_level = meta.get("reload", "unknown")

            result = {
                "path": path,
                "old_value": old_value,
                "new_value": new_value,
            }

            if reload_level == "hot":
                apply_msg = self._apply_hot_reload(path, new_value)
                result["status"] = "applied"
                result["detail"] = apply_msg or "Applied immediately."
            elif reload_level == "warm":
                warm_msg = self._apply_warm_reload(path, new_value)
                if warm_msg:
                    result["status"] = "applied"
                    result["detail"] = warm_msg
                else:
                    result["status"] = "saved"
                    result["detail"] = "Saved. Use the `restart` tool to apply."
            else:
                result["status"] = "saved"
                result["detail"] = "Saved. Requires full re-onboard to apply."

            return json.dumps(result, default=str)
        except ValueError as e:
            return f"Error: {e}"

    def _action_list(self) -> str:
        from ragnarbot.agent.tools.secrets_helpers import secrets_list
        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_all_paths

        config = load_config()
        all_paths = get_all_paths(config)
        lines = [f"{p} = {v!r}" for p, v in sorted(all_paths.items())]

        creds = load_credentials()
        sec = secrets_list(creds)
        if sec:
            lines.append("")
            lines.append(sec)

        return "\n".join(lines)

    def _action_diff(self) -> str:
        from ragnarbot.config.loader import load_config
        from ragnarbot.config.path_utils import get_all_paths
        from ragnarbot.config.schema import Config

        current = load_config()
        defaults = Config()
        current_paths = get_all_paths(current)
        default_paths = get_all_paths(defaults)

        diffs = []
        for p in sorted(current_paths.keys()):
            cur = current_paths[p]
            dflt = default_paths.get(p)
            if cur != dflt:
                diffs.append(f"{p}: {dflt!r} → {cur!r}")

        if not diffs:
            return "All values are at defaults.\n(Secrets excluded from diff — no defaults to compare against.)"
        diffs.append("\n(Secrets excluded from diff — no defaults to compare against.)")
        return "\n".join(diffs)

    def _apply_hot_reload(self, path: str, value: Any) -> str | None:
        """Apply a hot-reloadable config change to live agent/tool state."""
        agent = self._agent

        if path == "agents.defaults.temperature":
            agent.provider.set_temperature(float(value))
            return "Temperature updated on provider."

        if path == "agents.defaults.max_tokens":
            agent.provider.set_max_tokens(int(value))
            return "Max tokens updated on provider."

        if path == "agents.defaults.stream_steps":
            agent.stream_steps = bool(value) if isinstance(value, bool) else str(value).lower() in ("true", "1")
            return "Stream steps updated."

        if path == "agents.defaults.debounce_seconds":
            agent.debounce_seconds = float(value)
            return "Debounce seconds updated."

        if path == "agents.defaults.context_mode":
            agent.context_mode = str(value)
            return "Context mode updated."

        if path == "agents.defaults.max_context_tokens":
            tokens = int(value)
            agent.max_context_tokens = tokens
            agent.cache_manager.max_context_tokens = tokens
            agent.compactor.max_context_tokens = tokens
            return "Max context tokens updated."

        if path in ("tools.web.search.engine", "tools.web.search.max_results"):
            return self._reload_web_search()

        if path == "tools.exec.timeout":
            exec_tool = agent.tools.get("exec")
            if exec_tool and hasattr(exec_tool, "timeout"):
                exec_tool.timeout = int(value)
            return "Exec timeout updated."

        if path == "tools.exec.restrict_to_workspace":
            exec_tool = agent.tools.get("exec")
            if exec_tool and hasattr(exec_tool, "restrict_to_workspace"):
                val = bool(value) if isinstance(value, bool) else str(value).lower() in ("true", "1")
                exec_tool.restrict_to_workspace = val
            return "Exec restrict_to_workspace updated."

        # Browser config — hot reloadable fields
        if path.startswith("tools.browser."):
            browser_tool = agent.tools.get("browser")
            if browser_tool:
                mgr = browser_tool._manager
                field = path.split(".")[-1]
                if field == "idle_timeout":
                    mgr._config.idle_timeout = int(value)
                elif field == "headless":
                    mgr._config.headless = (
                        value if isinstance(value, bool)
                        else str(value).lower() in ("true", "1")
                    )
                elif field == "viewport_width":
                    mgr._config.viewport_width = int(value)
                elif field == "viewport_height":
                    mgr._config.viewport_height = int(value)
            return f"Browser {path.split('.')[-1]} updated (applies to new sessions)."

        # Fallback config — hot reloadable fields
        if path == "agents.fallback.consecutive_failures_threshold":
            if agent._fallback_config:
                agent._fallback_config.consecutive_failures_threshold = int(value)
            return "Fallback consecutive failures threshold updated."

        if path == "agents.fallback.recovery_probe_interval":
            if agent._fallback_config:
                agent._fallback_config.recovery_probe_interval = int(value)
            return "Fallback recovery probe interval updated."

        if path == "agents.fallback.max_tokens":
            if agent._fallback_config:
                agent._fallback_config.max_tokens = int(value) if value else None
            if agent._fallback_provider:
                agent._fallback_provider.set_max_tokens(int(value))
            return "Fallback max tokens updated."

        if path == "agents.fallback.temperature":
            if agent._fallback_config:
                agent._fallback_config.temperature = float(value) if value else None
            if agent._fallback_provider:
                agent._fallback_provider.set_temperature(float(value))
            return "Fallback temperature updated."

        return None

    def _apply_warm_reload(self, path: str, value: Any) -> str | None:
        """Apply warm-reloadable fallback config changes without restart."""
        agent = self._agent

        if path == "agents.fallback.model":
            agent._fallback_model = str(value) if value else None
            agent._fallback_provider = None
            if agent._fallback_config:
                agent._fallback_config.model = str(value) if value else None
            return "Fallback model updated. Will use new provider on next fallback."

        if path == "agents.fallback.auth_method":
            agent._fallback_provider = None
            if agent._fallback_config:
                agent._fallback_config.auth_method = str(value)
            return "Fallback auth method updated. Will use new provider on next fallback."

        return None

    @staticmethod
    def _validate_provider_auth(model: str, auth_method: str) -> str | None:
        """Validate that a provider has credentials for the given auth method.

        Returns an error message if credentials are missing, None if OK.
        Same checks as _validate_auth in commands.py but for runtime use.
        """
        from ragnarbot.auth.credentials import load_credentials
        from ragnarbot.config.schema import OAUTH_SUPPORTED_PROVIDERS

        provider_name = model.split("/")[0] if "/" in model else "anthropic"
        creds = load_credentials()
        provider_creds = getattr(creds.providers, provider_name, None)

        if auth_method == "oauth":
            if provider_name not in OAUTH_SUPPORTED_PROVIDERS:
                return (
                    f"OAuth is not supported for provider '{provider_name}'. "
                    f"Supported: {', '.join(sorted(OAUTH_SUPPORTED_PROVIDERS))}"
                )
            if provider_name == "gemini":
                from ragnarbot.auth.gemini_oauth import is_authenticated
                if not is_authenticated():
                    return "Gemini OAuth not configured. Run: ragnarbot oauth gemini"
            elif provider_name == "openai":
                from ragnarbot.auth.openai_oauth import is_authenticated
                if not is_authenticated():
                    return "OpenAI OAuth not configured. Run: ragnarbot oauth openai"
            elif not provider_creds or not provider_creds.oauth_key:
                return f"No OAuth token for '{provider_name}'"
        else:
            if not provider_creds or not provider_creds.api_key:
                return (
                    f"No API key for '{provider_name}'. "
                    f"Set via: config set secrets.providers.{provider_name}.api_key <key>"
                )

        return None

    def _reload_web_search(self) -> str:
        """Re-register WebSearchTool with current config values."""
        from ragnarbot.agent.tools.web import WebSearchTool
        from ragnarbot.config.loader import load_config

        config = load_config()
        agent = self._agent

        agent.tools.unregister("web_search")
        agent.tools.register(WebSearchTool(
            engine=config.tools.web.search.engine,
            api_key=agent.brave_api_key,
            max_results=config.tools.web.search.max_results,
        ))
        return "Web search tool re-registered."
