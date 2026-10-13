"""_key_change_errors: holder is not cleared when an original row is DELETEd,
so a new row that reuses the deleted key and is then moved is reported as
the original row's key change. Diff itself is a plain DELETE + INSERT."""
from _h import *
from safeagentdb import ShadowDB, SafeModel
which()

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str

eng = prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT)",
            "INSERT INTO tasks VALUES (1, 42, 'a')"])
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("DELETE FROM tasks WHERE id = 1")
    sb.execute("INSERT INTO tasks VALUES (1, 42, 'brand new')")
    sb.execute("UPDATE tasks SET id = 7 WHERE id = 1")
    cs = sb.diff()
    print("diffs:", [(d.diff_type.value, d.pk) for d in cs.diffs])
    print("is_valid:", cs.is_valid)
    print("blocking:", [e[:150] for e in cs.blocking_errors])

# Control: the same end state reached without the intermediate key reuse.
eng = prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT)",
            "INSERT INTO tasks VALUES (1, 42, 'a')"])
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("DELETE FROM tasks WHERE id = 1")
    sb.execute("INSERT INTO tasks VALUES (7, 42, 'brand new')")
    cs = sb.diff()
    print("control diffs:", [(d.diff_type.value, d.pk) for d in cs.diffs], "is_valid:", cs.is_valid)
