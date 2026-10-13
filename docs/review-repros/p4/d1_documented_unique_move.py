"""Documented limit (README 'Changesets the unit-of-work order does not suit'):
delete a row and insert a new one with the same UNIQUE value. Run on both versions."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()
class U(SafeModel):
    __table_name__ = "users"; id: int; user_id: int; email: str
eng = prod(["CREATE TABLE users (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, email TEXT NOT NULL UNIQUE)",
            "INSERT INTO users VALUES (1, 42, 'a@x')"])
with ShadowDB(eng, tables=["users"], tenant_id=42) as sb:
    sb.execute("DELETE FROM users WHERE id = 1")
    sb.execute("INSERT INTO users VALUES (2, 42, 'a@x')")
    try: print("commit wrote", sb.commit_to_production())
    except SafeAgentDBError as e: print("commit raised", type(e).__name__)
print("prod:", rows(eng, "SELECT * FROM users"))
