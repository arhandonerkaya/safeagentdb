"""
validators.py -- the SafeModel schemas Arm B validates against.

Kept in its own module for one reason: audit.py must not be able to reach
these. If the auditor shared a definition of "valid" with the thing being
measured, a gap in that definition would hide from both at once and the
benchmark would score itself. The split makes the independence structural
rather than a promise -- see audit.py's verify_independence().

Importing this module registers the validators globally, which is how
SafeAgentDB finds them. run.py imports it; audit.py does not.
"""

from __future__ import annotations

from typing import Literal

from safeagentdb import SafeModel


class PlanValidator(SafeModel):
    __table_name__ = "plans"

    code: str
    label: str
    monthly_cents: int


class UserValidator(SafeModel):
    __table_name__ = "users"

    id: int
    tenant_id: int
    email: str
    plan_code: Literal["free", "pro", "enterprise"]


class TaskValidator(SafeModel):
    __table_name__ = "tasks"

    id: int
    tenant_id: int
    owner_id: int
    title: str
    status: Literal["todo", "in_progress", "done", "archived"]
    priority: int


REGISTERED = ("plans", "users", "tasks")
