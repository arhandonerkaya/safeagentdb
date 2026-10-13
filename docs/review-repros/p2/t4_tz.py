"""Datetimes carrying a UTC offset in SQLite production."""
from typing import Any
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class E(SafeModel):
    __table_name__ = "ev"; id: int; user_id: int; note: str; at: Any

eng = prod(["CREATE TABLE ev (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, note TEXT NOT NULL, at DATETIME)",
            "INSERT INTO ev VALUES (1, 42, 'n', '2024-01-01 10:00:00+02:00')",
            "INSERT INTO ev VALUES (2, 42, 'n', '2024-01-01T10:00:00Z')"])
try:
    with ShadowDB(eng, tables=["ev"], tenant_id=42) as sb:
        print("sandbox:", sb.query("SELECT id, at FROM ev"))
        sb.execute("UPDATE ev SET note='x' WHERE id = 1")
        print("diff:", [(d.pk, d.changed_columns()) for d in sb.diff().diffs])
        print("commit:", sb.commit_to_production())
    print("prod:", rows(eng, "SELECT * FROM ev"))
except Exception as e:
    print(f"raised {type(e).__module__}.{type(e).__name__} (SafeAgentDBError: {isinstance(e, SafeAgentDBError)}): {e}")
