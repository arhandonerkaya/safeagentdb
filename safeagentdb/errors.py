"""
errors.py - Exception hierarchy for SafeAgentDB.

Every error the library raises derives from SafeAgentDBError, so a caller can
wrap a whole agent session in a single ``except SafeAgentDBError``.

    SafeAgentDBError
    |-- SchemaError            production schema cannot be sandboxed safely
    |-- SyncError              a changeset was rejected or could not be applied
    |   +-- ConflictError      production drifted between clone and commit
    +-- MissingValidatorError  no SafeModel registered and one is required
"""

from __future__ import annotations

from typing import Any


class SafeAgentDBError(Exception):
    """Base class for every error raised by SafeAgentDB."""


class SchemaError(SafeAgentDBError):
    """Raised when a production schema cannot be cloned into a safe sandbox.

    Most commonly: a table has no primary key and no explicit ``row_key``, so
    individual rows cannot be identified.
    """


class SyncError(SafeAgentDBError):
    """Raised when sync validation or execution fails."""


class ConflictError(SyncError):
    """Raised when a production row changed between clone time and commit time.

    Attributes:
        table: Name of the table the conflict was detected on.
        row_key: The row-identifying key of the affected row.
        columns: Columns whose production value drifted from the clone-time
            value. Empty when the row was deleted outright.
    """

    def __init__(
        self,
        message: str,
        *,
        table: str | None = None,
        row_key: dict[str, Any] | None = None,
        columns: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.table = table
        self.row_key = dict(row_key or {})
        self.columns = list(columns or [])


class GeneratedValueError(SyncError):
    """Raised when a row depends on a value only production can generate.

    The sandbox is SQLite and has no access to the production sequence,
    identity or default function, so it cannot produce the value the
    production database would have produced.

    Attributes:
        table: Name of the table the row belongs to.
        columns: The columns production generates.
    """

    def __init__(
        self,
        message: str,
        *,
        table: str | None = None,
        columns: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.table = table
        self.columns = list(columns or [])


class MissingValidatorError(SafeAgentDBError, KeyError):
    """Raised when no SafeModel is registered for a table and one is required.

    Also subclasses KeyError so that code written against SafeAgentDB 0.1.x,
    which caught a bare KeyError here, keeps working.
    """


class MissingValidatorWarning(UserWarning):
    """Warned when a row is written without a validator and ``require_validators``
    is False."""


__all__ = [
    "ConflictError",
    "GeneratedValueError",
    "MissingValidatorError",
    "MissingValidatorWarning",
    "SafeAgentDBError",
    "SchemaError",
    "SyncError",
]
