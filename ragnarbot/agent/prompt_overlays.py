"""Model-specific prompt overlays."""

OPENAI_STYLE_ADDENDUM = (
    "Do not end replies with unsolicited follow-up offers or optional-help phrases "
    "such as 'if you want, I can...' or similar. Only offer a next step when the user "
    "explicitly asked for options, or when that next step is necessary to complete the "
    "task correctly."
)


def is_openai_family_model(model: str | None) -> bool:
    """Return True when the model belongs to the OpenAI GPT family."""
    if not model:
        return False

    normalized = model.strip().lower()
    return (
        normalized.startswith("openai/")
        or normalized.startswith("gpt")
        or normalized.startswith("openrouter/openai/")
    )


def get_model_behavior_addendum(model: str | None) -> str:
    """Return a model-specific prompt addendum, if any."""
    if not is_openai_family_model(model):
        return ""
    return OPENAI_STYLE_ADDENDUM
