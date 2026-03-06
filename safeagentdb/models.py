"""
models.py - Pydantic v2 base model for validated AI outputs.

SafeModel provides:
1. Strict validation mode by default (no coercion surprises from LLM output)
2. A registry that maps table names -> validator models
3. Row-level validation before any sync to production
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict


# Global registry: table_name -> SafeModel subclass
_model_registry: dict[str, type[SafeModel]] = {}


class SafeModel(BaseModel):
    """Base model for all AI-writable table schemas.

    Subclass this and set `__table_name__` to register a validator
    for a specific table. All fields use strict validation by default.

    Example:
        class TaskValidator(SafeModel):
            __table_name__ = "tasks"

            id: int
            user_id: int
            title: str
            status: Literal["todo", "in_progress", "done"]
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    __table_name__: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__table_name__:
            _model_registry[cls.__table_name__] = cls


def get_validator(table_name: str) -> type[SafeModel] | None:
    """Look up the registered validator for a table."""
    return _model_registry.get(table_name)


def validate_row(table_name: str, row_data: dict[str, Any]) -> SafeModel:
    """Validate a single row dict against its registered SafeModel.

    Raises:
        KeyError: If no validator is registered for the table.
        pydantic.ValidationError: If the row data fails validation.
    """
    validator = _model_registry.get(table_name)
    if validator is None:
        raise KeyError(
            f"No SafeModel registered for table '{table_name}'. "
            f"Create a SafeModel subclass with __table_name__ = '{table_name}'."
        )
    return validator.model_validate(row_data)
