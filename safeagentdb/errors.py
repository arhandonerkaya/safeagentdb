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

from dataclasses import dataclass, field
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


class IntegrityViolationError(SyncError):
    """Raised when the production database rejects a row the sandbox accepted.

    The sandbox is a tenant-scoped SQLite copy, so it cannot see every rule
    production enforces -- most often a UNIQUE constraint whose colliding row
    belongs to another tenant, or a primary key that exists outside the clone.
    The whole changeset is rolled back. The driver's own exception is kept as
    ``__cause__``.

    Attributes:
        table: Name of the table the statement targeted.
        row_key: The row-identifying key of the offending row.
    """

    def __init__(
        self,
        message: str,
        *,
        table: str | None = None,
        row_key: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.table = table
        self.row_key = dict(row_key or {})


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


@dataclass(frozen=True)
class SkippedConflict:
    """One row that ``on_conflict="ignore"`` left unapplied.

    A changeset committed with ``on_conflict="ignore"`` is applied in part, so
    every skipped row is recorded here and surfaced on
    ``ShadowDB.skipped_conflicts``.

    Attributes:
        table: Name of the table the row belongs to.
        row_key: The row-identifying key of the skipped row.
        columns: Columns whose production value drifted. Empty when the row was
            deleted in production, or when the drift could not be pinned down.
        reason: The conflict message, as ConflictError would have reported it.
    """

    table: str
    row_key: dict[str, Any] = field(default_factory=dict)
    columns: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class AssignedKey:
    """A key production generated for a row the sandbox held provisionally.

    The sandbox has no access to the production sequence, so it fills a
    ``serial``/identity key with a placeholder. At commit the column is left out
    of the INSERT, production assigns the real value, and it is reported here
    and on ``ShadowDB.assigned_keys``.

    Attributes:
        table: Name of the table the row was inserted into.
        provisional: The placeholder key the sandbox used.
        assigned: The key production actually assigned.
    """

    table: str
    provisional: dict[str, Any] = field(default_factory=dict)
    assigned: dict[str, Any] = field(default_factory=dict)


class ConflictWarning(UserWarning):
    """Warned once per row skipped under ``on_conflict="ignore"``."""


class MissingValidatorWarning(UserWarning):
    """Warned when a row is written without a validator and ``require_validators``
    is False."""


__all__ = [
    "AssignedKey",
    "ConflictError",
    "ConflictWarning",
    "GeneratedValueError",
    "IntegrityViolationError",
    "MissingValidatorError",
    "MissingValidatorWarning",
    "SafeAgentDBError",
    "SchemaError",
    "SkippedConflict",
    "SyncError",
]
