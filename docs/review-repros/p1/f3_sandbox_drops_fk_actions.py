"""Does the sandbox keep ON DELETE / ON UPDATE actions of a SQLite production FK?"""
from _h import *
from sqlalchemy import exc
from safeagentdb import ShadowDB

eng = prod([
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE ON UPDATE CASCADE)",
    "CREATE TABLE note (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER REFERENCES parent(id) ON DELETE SET NULL)",
    "INSERT INTO parent VALUES (1, 42)",
    "INSERT INTO child VALUES (10, 42, 1)",
    "INSERT INTO note VALUES (20, 42, 1)",
])
print("production DDL :", rows(eng, "SELECT sql FROM sqlite_master WHERE name='child'")[0][0])
with ShadowDB(eng, tables=["parent", "child", "note"], tenant_id=42) as sb:
    print("sandbox DDL    :", sb.query("SELECT sql FROM sqlite_master WHERE name='child'")[0]["sql"])
    print("cascade_references:", [(r.child_table, r.on_delete, r.on_update) for r in sb.cascade_references])
    for stmt in ("DELETE FROM parent WHERE id = 1", "UPDATE parent SET id = 5 WHERE id = 1"):
        try:
            sb.execute(stmt); print(stmt, "-> ok"); sb.session.rollback()
        except exc.IntegrityError as e:
            sb.session.rollback(); print(stmt, "-> sandbox raised", type(e).__name__, e.orig)
# control: the same delete directly in production
with eng.begin() as c:
    c.exec_driver_sql("DELETE FROM parent WHERE id = 1")
print("control in prod: child", rows(eng, "SELECT * FROM child"), "note", rows(eng, "SELECT * FROM note"))
