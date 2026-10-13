"""Re-point a child to another parent, then delete the old parent.
FK is plain (NO ACTION). Sandbox accepts it; production should too."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int
class C(SafeModel):
    __table_name__ = "child"; id: int; user_id: int; parent_id: int

eng = prod([
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER NOT NULL REFERENCES parent(id))",
    "INSERT INTO parent VALUES (1, 42), (2, 42)",
    "INSERT INTO child VALUES (10, 42, 1)",
])
with ShadowDB(eng, tables=["parent", "child"], tenant_id=42) as sb:
    sb.execute("UPDATE child SET parent_id = 2 WHERE id = 10")
    sb.execute("DELETE FROM parent WHERE id = 1")   # sandbox FK enforcement: accepted
    cs = sb.diff()
    print("summary:", cs.summary, "is_valid:", cs.is_valid)
    print("banner:", banner(cs))
    try:
        print("commit wrote", sb.commit_to_production(changeset=cs))
    except SafeAgentDBError as e:
        print("commit raised", type(e).__name__, "->", e)
print("prod parent:", rows(eng, "SELECT * FROM parent"))
print("prod child :", rows(eng, "SELECT * FROM child"))
