"""A self-referencing table (manager_id -> id). Statement order inside one table is by key."""
from typing import Optional
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class E(SafeModel):
    __table_name__ = "emp"; id: int; user_id: int; manager_id: Optional[int]

def fresh():
    return prod(["CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                 " manager_id INTEGER REFERENCES emp(id))",
                 "INSERT INTO emp VALUES (1, 42, NULL), (2, 42, 1)"])
for label, ops in [
    ("delete manager and report", ["DELETE FROM emp WHERE id = 2", "DELETE FROM emp WHERE id = 1"]),
    ("insert report before its manager's id", ["INSERT INTO emp VALUES (20, 42, NULL)", "INSERT INTO emp VALUES (10, 42, 20)"]),
]:
    eng = fresh()
    with ShadowDB(eng, tables=["emp"], tenant_id=42) as sb:
        for o in ops: sb.execute(o)
        cs = sb.diff()
        print(f"[{label}] is_valid={cs.is_valid}")
        try: print("  commit wrote", sb.commit_to_production(changeset=cs))
        except SafeAgentDBError as e: print("  commit:", type(e).__name__, "->", e)
