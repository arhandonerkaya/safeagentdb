"""Lifecycle and call-order misuse."""
from _h import *
from sqlalchemy import exc
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError, ConflictError

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str

def fresh():
    return prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL)",
                 "INSERT INTO tasks VALUES (1, 42, 'one'), (2, 42, 'two')"])
def show(label, fn):
    try: print(f"{label}: returned {fn()!r}")
    except Exception as e:
        tag = "in hierarchy" if isinstance(e, SafeAgentDBError) else "outside hierarchy"
        print(f"{label}: {tag} {type(e).__name__}: {str(e).splitlines()[0][:100]}")

eng = fresh()
sb = ShadowDB(eng, tables=["tasks"], tenant_id=42)
show("1. execute before __enter__", lambda: sb.execute("SELECT 1"))
show("2. commit before __enter__", sb.commit_to_production)
show("3. properties before __enter__", lambda: (sb.clone_stats, sb.row_keys))

with sb:
    sb.execute("UPDATE tasks SET title = 'x' WHERE id = 1")
    print("4. commit without diff first:", sb.commit_to_production())
    show("5. second commit", sb.commit_to_production)
    cs = sb.diff()
    print(f"6. diff after commit: {cs.summary}, is_valid={cs.is_valid}, banner={banner(cs)!r}")
show("7. execute after exit", lambda: sb.execute("SELECT 1"))
show("8. diff after exit", sb.diff)
show("9. commit after exit", sb.commit_to_production)
print("10. properties after exit:", sb.clone_stats, sb.row_keys)

eng2 = fresh()
sb2 = ShadowDB(eng2, tables=["tasks"], tenant_id=42)
with sb2:
    pass
with sb2:   # re-enter the same object
    sb2.execute("UPDATE tasks SET title = 'y' WHERE id = 2")
    show("11. re-entered unused object, commit", sb2.commit_to_production)
with sb:    # re-enter an object that already committed
    sb.execute("UPDATE tasks SET title = 'z' WHERE id = 2")
    print("12. re-entered committed object, diff:", sb.diff().summary)
    show("    commit", sb.commit_to_production)

# 13. nested __enter__ on an open sandbox
eng3 = fresh()
sb3 = ShadowDB(eng3, tables=["tasks"], tenant_id=42)
with sb3:
    sb3.execute("UPDATE tasks SET title = 'before' WHERE id = 1")
    first = sb3.sandbox_engine
    sb3.__enter__()
    print("13. nested enter: earlier edit still visible:", sb3.query("SELECT title FROM tasks WHERE id = 1"),
          "| old engine replaced:", first is not sb3.sandbox_engine)

# 14. exception inside the with block
eng4 = fresh()
try:
    with ShadowDB(eng4, tables=["tasks"], tenant_id=42) as sb4:
        sb4.execute("UPDATE tasks SET title = 'x' WHERE id = 1")
        raise RuntimeError("agent crashed")
except RuntimeError as e:
    print("14. exception propagated:", e, "| prod:", rows(eng4, "SELECT * FROM tasks"), "| sandbox closed:", sb4.session is None)

# 15. a failing agent statement in the middle of a session
eng5 = fresh()
with ShadowDB(eng5, tables=["tasks"], tenant_id=42) as sb5:
    sb5.execute("UPDATE tasks SET title = 'kept' WHERE id = 1")
    try: sb5.execute("INSERT INTO tasks VALUES (1, 42, 'dup')")
    except exc.IntegrityError as e: print("15. agent statement failed:", type(e).__name__)
    show("    next execute", lambda: sb5.query("SELECT title FROM tasks WHERE id = 1"))
    show("    diff", lambda: sb5.diff().summary)

# 16. retry after a failed commit
eng6 = fresh()
with ShadowDB(eng6, tables=["tasks"], tenant_id=42) as sb6:
    sb6.execute("UPDATE tasks SET title = 'mine' WHERE id = 1")
    with eng6.begin() as c: c.exec_driver_sql("UPDATE tasks SET title = 'theirs' WHERE id = 1")
    show("16. first commit (drift)", sb6.commit_to_production)
    with eng6.begin() as c: c.exec_driver_sql("UPDATE tasks SET title = 'one' WHERE id = 1")
    show("    retry after the drift is undone", sb6.commit_to_production)
    print("    prod:", rows(eng6, "SELECT * FROM tasks"))
