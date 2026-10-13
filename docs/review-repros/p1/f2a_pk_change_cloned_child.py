"""Change a parent's primary key; the child (cloned) follows via ON UPDATE CASCADE."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int
class C(SafeModel):
    __table_name__ = "child"; id: int; user_id: int; parent_id: int

eng = prod([
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER NOT NULL REFERENCES parent(id)"
    " ON UPDATE CASCADE ON DELETE CASCADE)",
    "INSERT INTO parent VALUES (1, 42)",
    "INSERT INTO child VALUES (10, 42, 1)",
])
with ShadowDB(eng, tables=["parent", "child"], tenant_id=42) as sb:
    sb.execute("UPDATE parent SET id = 5 WHERE id = 1")
    print("sandbox child after cascade:", sb.query("SELECT * FROM child"))
    cs = sb.diff()
    print("diffs:", [(d.table, d.diff_type.value, d.pk) for d in cs.diffs])
    print("is_valid:", cs.is_valid, "|", banner(cs))
    try:
        print("commit wrote", sb.commit_to_production(changeset=cs))
    except SafeAgentDBError as e:
        print("commit raised", type(e).__name__, "->", e)
print("prod parent:", rows(eng, "SELECT * FROM parent"))
print("prod child :", rows(eng, "SELECT * FROM child"))
