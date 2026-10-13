"""Every new commit-time error raised after earlier statements ran leaves production untouched."""
from _h import *
import warnings
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()
class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int; name: str
DDL = [
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT)",
    "CREATE TABLE kid (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE)",
    "INSERT INTO parent VALUES (1, 42, 'a'), (2, 42, 'b')",
    "INSERT INTO kid VALUES (9, 42, 1)",
]
def snap(e): return rows(e, "SELECT * FROM parent"), rows(e, "SELECT * FROM kid")

# CascadeError after an INSERT and an UPDATE already ran.
eng = prod(DDL); before = snap(eng)
with ShadowDB(eng, tables=["parent"], tenant_id=42) as sb:
    sb.execute("INSERT INTO parent VALUES (3, 42, 'new')")
    sb.execute("UPDATE parent SET name = 'B' WHERE id = 2")
    sb.execute("DELETE FROM parent WHERE id = 1")
    try: sb.commit_to_production()
    except SafeAgentDBError as e: print("CascadeError path:", type(e).__name__)
print("   untouched:", snap(eng) == before)

# ConflictError (drift) after an INSERT ran.
eng = prod(DDL); before = None
with ShadowDB(eng, tables=["parent", "kid"], tenant_id=42) as sb:
    sb.execute("INSERT INTO parent VALUES (3, 42, 'new')")
    sb.execute("UPDATE parent SET name = 'B' WHERE id = 2")
    with eng.begin() as c: c.exec_driver_sql("UPDATE parent SET name = 'drift' WHERE id = 2")
    before = snap(eng)
    try: sb.commit_to_production()
    except SafeAgentDBError as e: print("ConflictError path:", type(e).__name__)
print("   untouched:", snap(eng) == before)

# Bind-time error (StatementError) wrapped, after an INSERT ran: drop a column in prod
eng = prod(DDL)
with ShadowDB(eng, tables=["parent", "kid"], tenant_id=42) as sb:
    sb.execute("INSERT INTO parent VALUES (3, 42, 'new')")
    sb.execute("UPDATE parent SET name = 'B' WHERE id = 2")
    with eng.begin() as c: c.exec_driver_sql("ALTER TABLE parent DROP COLUMN name")
    before = snap(eng)
    try: sb.commit_to_production()
    except SafeAgentDBError as e: print("DB error path:", type(e).__name__, str(e)[:90])
    except Exception as e: print("ESCAPED:", type(e).__name__, e)
print("   untouched:", snap(eng) == before)

# A warning turned into an error mid-transaction (ignore mode, skipped row).
eng = prod(DDL)
with ShadowDB(eng, tables=["parent", "kid"], tenant_id=42, on_conflict="ignore") as sb:
    sb.execute("INSERT INTO parent VALUES (3, 42, 'new')")
    sb.execute("UPDATE parent SET name = 'B' WHERE id = 2")
    with eng.begin() as c: c.exec_driver_sql("UPDATE parent SET name = 'drift' WHERE id = 2")
    before = snap(eng)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        try: sb.commit_to_production()
        except Exception as e: print("warning-as-error path:", type(e).__name__, "SafeAgentDBError:", isinstance(e, SafeAgentDBError))
print("   untouched:", snap(eng) == before)
