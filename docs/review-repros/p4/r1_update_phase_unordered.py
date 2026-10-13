"""UPDATEs are not ordered by row references (only INSERT/DELETE phases are).
FK onto a UNIQUE non-PK column, plain NO ACTION. Agent re-points the child
away from parent A, then renames A's code. Sandbox accepts; production would
accept the same statements in the same order."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()

class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int; code: str
class C(SafeModel):
    __table_name__ = "child"; id: int; user_id: int; pcode: str

DDL = [
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, code TEXT NOT NULL UNIQUE)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " pcode TEXT NOT NULL REFERENCES parent(code))",
    "INSERT INTO parent VALUES (1, 42, 'A'), (2, 42, 'C')",
    "INSERT INTO child VALUES (10, 42, 'A')",
]
STMTS = ["UPDATE child SET pcode = 'C' WHERE id = 10",
         "UPDATE parent SET code = 'Z' WHERE id = 1"]

ctl = prod(DDL)
with ctl.begin() as c:
    for s in STMTS:
        c.exec_driver_sql(s)
print("control, same statements in prod:", rows(ctl, "SELECT * FROM parent"), rows(ctl, "SELECT * FROM child"))

eng = prod(DDL)
with ShadowDB(eng, tables=["parent", "child"], tenant_id=42) as sb:
    for s in STMTS:
        sb.execute(s)
    cs = sb.diff()
    print("diff:", [(d.table, d.diff_type.value, d.pk) for d in cs.diffs], "is_valid:", cs.is_valid)
    try:
        print("commit wrote", sb.commit_to_production(changeset=cs))
    except SafeAgentDBError as e:
        print("commit raised", type(e).__name__, "->", str(e)[:200])
print("prod parent:", rows(eng, "SELECT * FROM parent"))
print("prod child :", rows(eng, "SELECT * FROM child"))

# Variant: UPDATE of the referenced value, then INSERT of a child pointing at it.
eng = prod(DDL)
with ShadowDB(eng, tables=["parent", "child"], tenant_id=42) as sb:
    sb.execute("UPDATE parent SET code = 'B' WHERE id = 2")
    sb.execute("INSERT INTO child VALUES (11, 42, 'B')")
    cs = sb.diff()
    print("variant diff:", [(d.table, d.diff_type.value, d.pk) for d in cs.diffs], "is_valid:", cs.is_valid)
    try:
        print("variant commit wrote", sb.commit_to_production(changeset=cs))
    except SafeAgentDBError as e:
        print("variant commit raised", type(e).__name__, "->", str(e)[:120])
