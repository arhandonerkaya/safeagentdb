"""AgentSQLError says 'Earlier changes are kept', but a statement with
OR ROLLBACK conflict resolution rolls back the agent's whole open transaction."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str

eng = prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT)",
            "INSERT INTO tasks VALUES (1, 42, 'a')"])
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("UPDATE tasks SET title = 'b' WHERE id = 1")
    sb.execute("INSERT INTO tasks VALUES (2, 42, 'c')")
    try:
        sb.execute("INSERT OR ROLLBACK INTO tasks VALUES (1, 42, 'dup')")
    except SafeAgentDBError as e:
        print(type(e).__name__, "->", e)
    print("sandbox now:", sb.query("SELECT * FROM tasks"))
    cs = sb.diff()
    print("diff:", [(d.diff_type.value, d.pk) for d in cs.diffs])
