"""Does a sandbox-assigned key collide with a row the clone never saw?

Production is SQLite with `id INTEGER PRIMARY KEY` -- a rowid alias with no
server default. Tenant 42 holds the low ids, tenant 99 the high ones.
"""
from _h import *
from safeagentdb import SafeModel, ShadowDB
which()

class T(SafeModel):
    __table_name__ = "tasks"; id: int; tenant_id: int; title: str

DDL = ["""CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            tenant_id INTEGER NOT NULL,
            title TEXT NOT NULL)""",
       """INSERT INTO tasks (id, tenant_id, title) VALUES
            (1, 42, 'a'), (2, 42, 'b'), (3, 42, 'c'), (4, 42, 'd'),
            (5, 99, 'e'), (6, 99, 'f')"""]

def insert_one(eng, tenant):
    with ShadowDB(eng, tables=["tasks"], tenant_id=tenant,
                  tenant_column="tenant_id") as sb:
        print(f"  cloned ids: {[r[0] for r in rows(eng, f'SELECT id FROM tasks WHERE tenant_id={tenant}')]}"
              f"  provisional_key_columns: {sb.provisional_key_columns}")
        sb.execute("INSERT INTO tasks (tenant_id, title) VALUES (:t, 'new')", {"t": tenant})
        cs = sb.diff()
        print(f"  diff: {'SAFE' if cs.is_valid else 'BLOCKED'}  "
              f"assigned pk: {[d.pk for d in cs.diffs]}")
        try:
            sb.commit_to_production(cs)
            print("  commit: OK")
        except Exception as exc:
            print(f"  commit: {type(exc).__name__}: {str(exc).split('.')[0]}.")

print("\n-- rowid alias (no server default): the provisional-key path never engages")
eng = prod(DDL)
for tenant in (42, 99):
    print(f" tenant {tenant}:")
    insert_one(eng, tenant)
print(" production:", rows(eng, "SELECT id, tenant_id, title FROM tasks ORDER BY id"))

print("\n-- same table, key declared with a server default: the path engages")
eng2 = prod_serial([DDL[0].replace("id INTEGER PRIMARY KEY",
                                   "id INTEGER PRIMARY KEY DEFAULT (nextval('tasks_id_seq'))"),
                    DDL[1]])
for tenant in (42, 99):
    print(f" tenant {tenant}:")
    insert_one(eng2, tenant)
print(" production:", rows(eng2, "SELECT id, tenant_id, title FROM tasks ORDER BY id"))
