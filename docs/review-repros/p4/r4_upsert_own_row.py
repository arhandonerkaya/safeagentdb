"""Explicit-key trigger (BEFORE INSERT ... WHEN NEW.id >= 2^52) fires for an
upsert/INSERT OR IGNORE that names an already-generated key, even though no
new key is supplied: the row the sandbox generated is then classified as
explicit and blocked."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()

class A(SafeModel):
    __table_name__ = "accounts"; id: int; user_id: int; name: str

DDL = [
    "CREATE TABLE accounts (id INTEGER PRIMARY KEY DEFAULT (nextval('s')), user_id INTEGER NOT NULL, name TEXT)",
    "INSERT INTO accounts VALUES (1, 42, 'existing')",
]
for label, follow_up in [
    ("plain UPDATE (control)", "UPDATE accounts SET name = 'renamed' WHERE id = {g}"),
    ("ON CONFLICT DO UPDATE", "INSERT INTO accounts (id, user_id, name) VALUES ({g}, 42, 'renamed') "
                              "ON CONFLICT(id) DO UPDATE SET name = excluded.name"),
    ("INSERT OR IGNORE dup", "INSERT OR IGNORE INTO accounts (id, user_id, name) VALUES ({g}, 42, 'dup')"),
]:
    eng = prod_serial(DDL)
    with ShadowDB(eng, tables=["accounts"], tenant_id=42) as sb:
        g = sb.query("INSERT INTO accounts (user_id, name) VALUES (42, 'new') RETURNING id")[0]["id"]
        sb.execute(follow_up.format(g=g))
        cs = sb.diff()
        msgs = [m for _, ok, m in cs.validate_all() if not ok]
        print(f"[{label}] sandbox={sb.query('SELECT id, name FROM accounts')}")
        print(f"[{label}]   is_valid={cs.is_valid} {[m[:110] for m in msgs]}")
        try:
            print(f"[{label}]   commit wrote", sb.commit_to_production(changeset=cs))
        except SafeAgentDBError as e:
            print(f"[{label}]   commit raised {type(e).__name__}")
    print(f"[{label}]   prod:", rows(eng, "SELECT * FROM accounts"))
