"""Can the agent erase or forge a key-change log entry?"""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()
class E(SafeModel):
    __table_name__ = "emp"; id: int; user_id: int
for label, stmt in [("DELETE log", "DELETE FROM _safeagentdb_keys_0"),
                    ("UPDATE log", "UPDATE _safeagentdb_keys_0 SET n0 = 1"),
                    ("INSERT reverse move", "INSERT INTO _safeagentdb_keys_0 (o0, n0) VALUES (5, 1)")]:
    eng = prod(["CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)", "INSERT INTO emp VALUES (1, 42)"])
    with ShadowDB(eng, tables=["emp"], tenant_id=42) as sb:
        sb.execute("UPDATE emp SET id = 5 WHERE id = 1")
        try: sb.execute(stmt); r = "accepted"
        except SafeAgentDBError as e: r = f"refused ({str(e)[:60]})"
        cs = sb.diff()
        print(f"{label}: {r}; key change still reported: {bool(cs.blocking_errors)}; diffs={[(d.diff_type.value, d.pk) for d in cs.diffs]}")
