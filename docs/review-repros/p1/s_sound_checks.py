"""Scenarios expected to be sound. Each prints diff summary, validity and production result."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError, ChangesetMismatchError, SyncError
from sqlalchemy import create_engine

class M(SafeModel):
    __table_name__ = "memb"; org: int; name: str; user_id: int; role: str
class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str

def memb():
    return prod(["CREATE TABLE memb (org INTEGER, name TEXT, user_id INTEGER NOT NULL,"
                 " role TEXT NOT NULL, PRIMARY KEY (org, name))",
                 "INSERT INTO memb VALUES (1,'a',42,'r1'),(1,'b',42,'r2'),(1,'z',99,'other')"])
def tasks(serial=False):
    # A SQLite INTEGER PRIMARY KEY has no generated default to lose, so a provisional
    # key needs a server default the sandbox drops; a Postgres-style nextval() does that.
    if serial:
        return prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY DEFAULT (nextval('s')),"
                     " user_id INTEGER NOT NULL, title TEXT NOT NULL)",
                     "INSERT INTO tasks VALUES (1,42,'one'),(2,42,'two'),(3,99,'theirs')"])
    return prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL)",
                 "INSERT INTO tasks VALUES (1,42,'one'),(2,42,'two'),(3,99,'theirs')"])

def run(label, eng, tables, ops, query, **kw):
    with ShadowDB(eng, tables=tables, tenant_id=42, **kw) as sb:
        for o in ops: sb.execute(o)
        cs = sb.diff()
        desc = [(d.diff_type.value, d.pk, d.changed_columns()) for d in cs.diffs]
        try:
            n = sb.commit_to_production(changeset=cs); res = f"wrote {n}"
        except SafeAgentDBError as e:
            res = f"{type(e).__name__}: {str(e)[:110]}"
        print(f"[{label}] valid={cs.is_valid} diffs={desc}\n    commit: {res}\n    prod: {rows(eng, query)}")
        return sb

run("composite update", memb(), ["memb"], ["UPDATE memb SET role='x' WHERE org=1 AND name='a'"], "SELECT * FROM memb")
run("composite key change", memb(), ["memb"], ["UPDATE memb SET name='c' WHERE org=1 AND name='a'"], "SELECT * FROM memb")
run("key change onto other tenant's key", memb(), ["memb"], ["UPDATE memb SET name='z' WHERE org=1 AND name='a'"], "SELECT * FROM memb")
run("swap keys", memb(), ["memb"], ["UPDATE memb SET name='tmp' WHERE name='a'", "UPDATE memb SET name='a' WHERE name='b'",
                                    "UPDATE memb SET name='b' WHERE name='tmp'"], "SELECT * FROM memb")
run("plain pk change", tasks(), ["tasks"], ["UPDATE tasks SET id=7 WHERE id=1"], "SELECT * FROM tasks")
run("pk change onto other tenant's pk", tasks(), ["tasks"], ["UPDATE tasks SET id=3 WHERE id=1"], "SELECT * FROM tasks")
run("delete+reinsert, new payload", tasks(), ["tasks"], ["DELETE FROM tasks WHERE id=1", "INSERT INTO tasks VALUES (1,42,'again')"], "SELECT * FROM tasks")
run("delete+reinsert, same payload", tasks(), ["tasks"], ["DELETE FROM tasks WHERE id=1", "INSERT INTO tasks VALUES (1,42,'one')"], "SELECT * FROM tasks")
run("update then revert", tasks(), ["tasks"], ["UPDATE tasks SET title='x' WHERE id=1", "UPDATE tasks SET title='one' WHERE id=1"], "SELECT * FROM tasks")
run("insert then delete", tasks(), ["tasks"], ["INSERT INTO tasks VALUES (50,42,'tmp')", "DELETE FROM tasks WHERE id=50"], "SELECT * FROM tasks")

# Provisional keys: the sandbox drops the nextval default; production SQLite then needs
# the column to be fillable, so the production side uses INTEGER PRIMARY KEY behaviour by
# rebuilding without the default after the sandbox has reflected it.
eng = tasks(serial=True)
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    print("\n[provisional] provisional_key_columns:", sb.provisional_key_columns)
    sb.execute("INSERT INTO tasks (user_id, title) VALUES (42, 'new')")
    sb.execute("INSERT INTO tasks (id, user_id, title) VALUES (9, 42, 'hand-picked')")
    cs = sb.diff()
    for d, ok, msg in cs.validate_all():
        print(f"    {d.diff_type.value} pk={d.pk} pending={d.pending_key_columns()} valid={ok} {msg[:70]}")
    print("    banner:", banner(cs))
    sb.execute("DELETE FROM tasks WHERE id = 9")
    cs = sb.diff()
    print("    after removing the hand-picked row: valid", cs.is_valid, cs.summary)

# Stale changeset, retry, double commit
eng = tasks()
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("UPDATE tasks SET title='x' WHERE id=1")
    reviewed = sb.diff()
    sb.execute("DELETE FROM tasks WHERE id=2")
    try: sb.commit_to_production(changeset=reviewed)
    except ChangesetMismatchError as e: print("\n[stale] ChangesetMismatchError, prod:", rows(eng, "SELECT * FROM tasks"))
    sb.execute("UPDATE tasks SET title='x2' WHERE id=1"); sb.execute("UPDATE tasks SET title='x' WHERE id=1")
    sb.execute("INSERT INTO tasks VALUES (2,42,'two')")
    print("[stale] after undoing the extra change, same fingerprint:", sb.diff().fingerprint == reviewed.fingerprint)
    print("[stale] commit:", sb.commit_to_production(changeset=reviewed), rows(eng, "SELECT * FROM tasks"))
    try: sb.commit_to_production()
    except SyncError as e: print("[stale] second commit:", type(e).__name__)
