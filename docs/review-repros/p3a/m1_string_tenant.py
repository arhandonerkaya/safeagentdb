"""tenant_id passed as the string '42' (as it arrives from a URL or header) for an INTEGER column."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str

eng = prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL)",
            "INSERT INTO tasks VALUES (1, 42, 'one'), (2, 42, 'two'), (3, 99, 'theirs')"])
with ShadowDB(eng, tables=["tasks"], tenant_id="42") as sb:
    print("clone_stats:", sb.clone_stats, "(the tenant filter matched)")
    sb.execute("UPDATE tasks SET title = 'x' WHERE id = 1")
    sb.execute("DELETE FROM tasks WHERE id = 2")
    cs = sb.diff()
    print("is_valid:", cs.is_valid)
    for e in cs.blocking_errors: print("  blocking:", e)
    try: sb.commit_to_production()
    except SafeAgentDBError as e: print("commit:", type(e).__name__)
