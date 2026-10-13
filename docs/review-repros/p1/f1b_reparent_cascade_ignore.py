"""Same, but the FK is ON DELETE CASCADE and on_conflict='ignore'."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int
class C(SafeModel):
    __table_name__ = "child"; id: int; user_id: int; parent_id: int

eng = prod([
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER NOT NULL REFERENCES parent(id) ON DELETE CASCADE)",
    "INSERT INTO parent VALUES (1, 42), (2, 42)",
    "INSERT INTO child VALUES (10, 42, 1)",
])
for mode in ("abort", "ignore"):
    eng2 = eng
    with ShadowDB(eng2, tables=["parent", "child"], tenant_id=42, on_conflict=mode) as sb:
        sb.execute("UPDATE child SET parent_id = 2 WHERE id = 10")
        sb.execute("DELETE FROM parent WHERE id = 1")
        cs = sb.diff()
        print(f"[{mode}] summary:", cs.summary, "is_valid:", cs.is_valid, "|", banner(cs))
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            try:
                print(f"[{mode}] commit wrote", sb.commit_to_production(changeset=cs))
            except SafeAgentDBError as e:
                print(f"[{mode}] commit raised", type(e).__name__, "->", e)
            for x in w: print(f"[{mode}] warning:", x.category.__name__)
        print(f"[{mode}] skipped:", [(s.table, s.row_key) for s in sb.skipped_conflicts])
    print(f"[{mode}] prod parent:", rows(eng, "SELECT * FROM parent"))
    print(f"[{mode}] prod child :", rows(eng, "SELECT * FROM child"))
