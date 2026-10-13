"""Generated-key tracking: a key the agent sets with an UPDATE on its own new
row is never seen by the explicit-key trigger (BEFORE INSERT only), so a value
>= 2^52 is classified as sandbox-generated and dropped at commit."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()

class A(SafeModel):
    __table_name__ = "accounts"; id: int; user_id: int; name: str
class N(SafeModel):
    __table_name__ = "notes"; id: int; user_id: int; account_id: int

SNOWFLAKE = 1234567890123456789  # > 2**52
DDL = [
    "CREATE TABLE accounts (id INTEGER PRIMARY KEY DEFAULT (nextval('accounts_id_seq')), user_id INTEGER NOT NULL, name TEXT)",
    "CREATE TABLE notes (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " account_id INTEGER NOT NULL REFERENCES accounts(id))",
    "INSERT INTO accounts VALUES (1, 42, 'existing')",
]

for label, stmts in [
    ("explicit INSERT", [f"INSERT INTO accounts VALUES ({SNOWFLAKE}, 42, 'new')"]),
    ("INSERT then UPDATE id", ["INSERT INTO accounts (user_id, name) VALUES (42, 'new')",
                               f"UPDATE accounts SET id = {SNOWFLAKE} WHERE name = 'new'"]),
]:
    eng = prod_serial(DDL)
    with ShadowDB(eng, tables=["accounts", "notes"], tenant_id=42) as sb:
        for s in stmts:
            sb.execute(s)
        print(f"[{label}] sandbox:", sb.query("SELECT id, name FROM accounts"))
        cs = sb.diff()
        for d in cs.diffs:
            print(f"[{label}]   {d.diff_type.value} new={d.new} pending={d.pending_key_columns()}")
        print(f"[{label}]   is_valid={cs.is_valid}")
        try:
            print(f"[{label}]   commit wrote", sb.commit_to_production(changeset=cs),
                  "assigned:", [(k.table, k.provisional, k.assigned) for k in sb.assigned_keys] if hasattr(sb, "assigned_keys") else "")
        except SafeAgentDBError as e:
            print(f"[{label}]   commit raised", type(e).__name__, "->", str(e)[:200])
    print(f"[{label}]   prod:", rows(eng, "SELECT * FROM accounts"))

# Same, now with a child referencing the agent's chosen id.
eng = prod_serial(DDL)
with ShadowDB(eng, tables=["accounts", "notes"], tenant_id=42) as sb:
    sb.execute("INSERT INTO accounts (user_id, name) VALUES (42, 'new')")
    sb.execute(f"UPDATE accounts SET id = {SNOWFLAKE} WHERE name = 'new'")
    sb.execute(f"INSERT INTO notes VALUES (5, 42, {SNOWFLAKE})")
    cs = sb.diff()
    print("[with child] is_valid:", cs.is_valid)
    for d in cs.diffs:
        ok, msg = d.validate()
        print("[with child]  ", d.table, d.diff_type.value, ok, msg[:160])
