"""Any integer >= 2**52 in any column is read as a sandbox placeholder key,
in tables with no generated key at all, on UPDATEs of unrelated columns."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class A(SafeModel):
    __table_name__ = "accounts"; id: int; user_id: int; external_id: int; name: str

SNOWFLAKE = 1_234_567_890_123_456_789   # an ordinary 64-bit external id
eng = prod([
    "CREATE TABLE accounts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " external_id BIGINT NOT NULL, name TEXT NOT NULL)",
    f"INSERT INTO accounts VALUES (1, 42, {SNOWFLAKE}, 'old')",
])
with ShadowDB(eng, tables=["accounts"], tenant_id=42) as sb:
    print("provisional_key_columns:", sb.provisional_key_columns, "generated:", sb.generated_columns)
    sb.execute("UPDATE accounts SET name = 'new' WHERE id = 1")
    cs = sb.diff()
    print("is_valid:", cs.is_valid, "|", banner(cs))
    print("row message:", cs.validate_all()[0][2])
    try:
        sb.commit_to_production(changeset=cs)
    except SafeAgentDBError as e:
        print("commit raised", type(e).__name__, "columns=", e.columns)
