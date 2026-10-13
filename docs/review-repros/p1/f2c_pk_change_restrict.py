"""Change a parent's PK where the child FK is ON UPDATE CASCADE, ON DELETE RESTRICT."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int

eng = prod([
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER NOT NULL REFERENCES parent(id)"
    " ON UPDATE CASCADE ON DELETE RESTRICT)",
    "INSERT INTO parent VALUES (1, 42)",
    "INSERT INTO child VALUES (10, 42, 1)",
])
with eng.begin() as c:   # control: the agent's statement, run directly, is legal
    c.exec_driver_sql("SAVEPOINT s"); c.exec_driver_sql("UPDATE parent SET id = 5 WHERE id = 1")
    print("control, direct UPDATE in prod:", rows(eng, "SELECT 1") and "ok", c.exec_driver_sql("SELECT * FROM child").fetchall())
    c.exec_driver_sql("ROLLBACK TO s")
with ShadowDB(eng, tables=["parent"], tenant_id=42) as sb:
    sb.execute("UPDATE parent SET id = 5 WHERE id = 1")
    cs = sb.diff()
    print("is_valid:", cs.is_valid, "|", banner(cs))
    try:
        print("commit wrote", sb.commit_to_production(changeset=cs))
    except SafeAgentDBError as e:
        print("commit raised", type(e).__name__, "->", e)
print("prod parent:", rows(eng, "SELECT * FROM parent"))
