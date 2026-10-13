"""Exceptions from public methods that are not SafeAgentDBError."""
import decimal, os, sqlite3, tempfile
from _h import *
from sqlalchemy import create_engine
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str

DDL = ["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL)",
       "INSERT INTO tasks VALUES (1, 42, 'one')"]

def show(label, fn):
    try:
        r = fn(); print(f"{label}: returned {r!r}")
    except Exception as e:
        tag = "in hierarchy" if isinstance(e, SafeAgentDBError) else "ESCAPES"
        print(f"{label}: {tag} {type(e).__module__}.{type(e).__name__}: {str(e).splitlines()[0][:110]}")

# __enter__: reflection runs before the try block that wraps SQLAlchemy errors
eng = prod(DDL)
show("enter, unknown table", lambda: ShadowDB(eng, tables=["taskz"], tenant_id=42).__enter__())
bad = create_engine("sqlite:///" + os.path.join(tempfile.mkdtemp(), "missing", "x.db"))
show("enter, production unreachable", lambda: ShadowDB(bad, tables=["tasks"], tenant_id=42).__enter__())

# execute(): text() and untyped binds
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    show("execute, time literal '12:30'", lambda: sb.execute("UPDATE tasks SET title = 'at 12:30' WHERE id = 1"))
    show("execute, Decimal bind", lambda: sb.execute("UPDATE tasks SET title = :v WHERE id = 1", {"v": decimal.Decimal("1.5")}))
    show("query, non-SELECT", lambda: sb.query("UPDATE tasks SET title = 'x' WHERE id = 1"))

# diff(): the agent changed the sandbox schema
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("ALTER TABLE tasks RENAME COLUMN title TO name")
    show("diff, after RENAME COLUMN", sb.diff)
    show("commit, after RENAME COLUMN", sb.commit_to_production)
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("DROP TABLE tasks")
    show("diff, after DROP TABLE", sb.diff)

# commit: production locked; reads in the commit path are not routed through _execute
eng = prod(DDL)
locked = create_engine(str(eng.url), connect_args={"timeout": 0.2})
with ShadowDB(locked, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("INSERT INTO tasks VALUES (2, 42, 'two')")      # INSERT: _refuse_existing_key reads first
    holder = sqlite3.connect(eng.url.database, timeout=0)
    holder.execute("BEGIN EXCLUSIVE")
    show("commit, INSERT while prod locked", sb.commit_to_production)
    holder.rollback(); holder.close()
with ShadowDB(locked, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("UPDATE tasks SET title = 'x' WHERE id = 1")    # UPDATE: first statement goes through _execute
    holder = sqlite3.connect(eng.url.database, timeout=0)
    holder.execute("BEGIN EXCLUSIVE")
    show("commit, UPDATE while prod locked", sb.commit_to_production)
    holder.rollback(); holder.close()

# Constructor and argument misuse
show("init, bad on_conflict", lambda: ShadowDB(eng, tables=["tasks"], tenant_id=42, on_conflict="skip"))
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    show("commit, changeset=dict", lambda: sb.commit_to_production(changeset={"x": 1}))
