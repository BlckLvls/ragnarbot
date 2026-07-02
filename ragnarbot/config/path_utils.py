"""Dot-path navigation utilities for nested Pydantic models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, get_args, get_origin

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from ragnarbot.config.loader import camel_to_snake, snake_to_camel

if TYPE_CHECKING:
    from ragnarbot.config.schema import Config


def resolve_field_name(model_cls: type[BaseModel], segment: str) -> str | None:
    """Match a camelCase or snake_case segment to the actual field name on the model.

    Returns the field name as defined in the model, or None if not found.
    """
    fields = model_cls.model_fields
    if segment in fields:
        return segment
    # Try converting camelCase -> snake_case
    snake = camel_to_snake(segment)
    if snake in fields:
        return snake
    # Try converting snake_case -> camelCase
    camel = snake_to_camel(segment)
    if camel in fields:
        return camel
    return None


def _walk_path(config: BaseModel, path: str) -> tuple[BaseModel, str, FieldInfo]:
    """Walk a dot-path and return (parent_model, field_name, field_info).

    Raises ValueError if the path is invalid.
    """
    segments = path.split(".")
    current = config

    for i, segment in enumerate(segments):
        if not isinstance(current, BaseModel):
            raise ValueError(f"Cannot traverse into non-model at '{'.'.join(segments[:i])}'")

        field_name = resolve_field_name(type(current), segment)
        if field_name is None:
            raise ValueError(
                f"Unknown field '{segment}' on {type(current).__name__}. "
                f"Available: {', '.join(type(current).model_fields.keys())}"
            )

        field_info = type(current).model_fields[field_name]

        if i == len(segments) - 1:
            return current, field_name, field_info

        current = getattr(current, field_name)

    raise ValueError("Empty path")


def get_by_path(config: "Config", path: str) -> Any:
    """Get value at a dot-path.

    Args:
        config: Root Config instance.
        path: Dot-separated path like 'agents.defaults.model'.

    Returns:
        The value at the path.

    Raises:
        ValueError: If the path is invalid.
    """
    parent, field_name, _ = _walk_path(config, path)
    return getattr(parent, field_name)


def set_by_path(config: "Config", path: str, value: Any) -> None:
    """Set value at a dot-path with type coercion.

    Coerces string values to the target field type (str->float, str->bool, str->int).
    After setting, validates the parent model to catch constraint violations.

    Raises:
        ValueError: If the path is invalid or validation fails.
    """
    parent, field_name, field_info = _walk_path(config, path)

    # Coerce the value to the target type
    coerced = _coerce_value(value, field_info)

    # Set the value
    setattr(parent, field_name, coerced)

    # Validate the parent model to catch pattern/range violations
    try:
        type(parent).model_validate(parent.model_dump())
    except Exception as e:
        # Rollback is not strictly needed since we're about to raise,
        # but the caller should discard the config on error anyway.
        raise ValueError(f"Validation failed for '{path}': {e}") from e


def _coerce_value(value: Any, field_info: FieldInfo) -> Any:
    """Coerce a value to match the field's annotation."""
    annotation = field_info.annotation
    if annotation is None:
        return value

    # Unwrap Optional / Union types to get the base type
    origin = get_origin(annotation)
    if origin is not None:
        args = get_args(annotation)
        # For Union[X, None] (Optional[X]), use X
        non_none = [a for a in args if a is not type(None)]
        if non_none:
            annotation = non_none[0]

    if not isinstance(value, str):
        return value

    if annotation is bool:
        lower = value.lower()
        if lower in ("true", "1", "yes", "on"):
            return True
        if lower in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"Cannot convert '{value}' to bool")

    if annotation is int:
        return int(value)

    if annotation is float:
        return float(value)

    return value


def get_all_paths(model: BaseModel, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested model into {dot_path: value} for all leaf fields."""
    result: dict[str, Any] = {}
    for field_name, field_info in type(model).model_fields.items():
        path = f"{prefix}.{field_name}" if prefix else field_name
        value = getattr(model, field_name)
        if isinstance(value, BaseModel):
            result.update(get_all_paths(value, path))
        else:
            result[path] = value
    return result


def get_field_meta(model_cls: type[BaseModel], path: str) -> dict:
    """Get metadata about a field at a dot-path.

    Returns a dict with keys: type, default, description, reload, label, pattern, enum.
    """
    segments = path.split(".")
    current_cls = model_cls

    for i, segment in enumerate(segments):
        field_name = resolve_field_name(current_cls, segment)
        if field_name is None:
            raise ValueError(f"Unknown field '{segment}' on {current_cls.__name__}")

        field_info = current_cls.model_fields[field_name]

        if i < len(segments) - 1:
            # Traverse into nested model
            annotation = field_info.annotation
            origin = get_origin(annotation)
            if origin is not None:
                args = get_args(annotation)
                non_none = [a for a in args if a is not type(None)]
                if non_none:
                    annotation = non_none[0]
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                current_cls = annotation
            else:
                raise ValueError(f"'{segment}' is not a nested model, cannot traverse further")
            continue

        # Build metadata for the leaf field
        extra = field_info.json_schema_extra or {}
        annotation = field_info.annotation
        type_name = getattr(annotation, "__name__", str(annotation))

        meta: dict[str, Any] = {
            "type": type_name,
            "default": field_info.default,
            "reload": extra.get("reload", "unknown"),
            "label": extra.get("label", ""),
        }

        # Extract constraints from field metadata
        pattern = None
        for m in field_info.metadata:
            if hasattr(m, "pattern"):
                pattern = m.pattern
            if hasattr(m, "ge"):
                meta["ge"] = m.ge
            if hasattr(m, "le"):
                meta["le"] = m.le
        if pattern:
            meta["pattern"] = pattern

        return meta

    raise ValueError("Empty path")
