"""B1 normalisation: a NUMERIC/DECIMAL tenant column makes an int tenant_id unusable."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
from decimal import Decimal
which()

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: Decimal; title: str

for tid in (42, "42", Decimal("42")):
    eng = prod([
        "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id NUMERIC(10,0) NOT NULL, title TEXT)",
        "INSERT INTO tasks VALUES (1, 42, 'a'), (2, 7, 'other tenant')",
    ])
    try:
        with ShadowDB(eng, tables=["tasks"], tenant_id=tid) as sb:
            print(f"tenant_id={tid!r}: cloned", sb.query("SELECT id FROM tasks"))
            sb.execute("UPDATE tasks SET title = 'b' WHERE id = 1")
            cs = sb.diff()
            print("   is_valid:", cs.is_valid, cs.blocking_errors)
            print("   commit wrote", sb.commit_to_production(changeset=cs))
    except SafeAgentDBError as e:
        print(f"tenant_id={tid!r}: raised {type(e).__name__} -> {e}")
    print("   prod:", rows(eng, "SELECT * FROM tasks"))
