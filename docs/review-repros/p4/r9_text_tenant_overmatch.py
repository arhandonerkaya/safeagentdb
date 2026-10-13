"""same_tenant() normalises a referencing row's tenant value to int with
int(value.strip()), so in a TEXT tenant column '042', '+42' and ' 42' all count
as tenant 42. With allow_cascades=True the cascade into those rows is allowed."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()
class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int
DDL = [
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    # Not cloned. Its tenant column is TEXT, and '042' is a different tenant from '42'.
    "CREATE TABLE share (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,"
    " parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE)",
    "INSERT INTO parent VALUES (1, 42)",
    "INSERT INTO share VALUES (100, '042', 1)",
]
for allow in (False, True):
    eng = prod(DDL)
    with ShadowDB(eng, tables=["parent"], tenant_id=42, allow_cascades=allow) as sb:
        sb.execute("DELETE FROM parent WHERE id = 1")
        try:
            print(f"allow_cascades={allow}: commit wrote", sb.commit_to_production())
        except SafeAgentDBError as e:
            print(f"allow_cascades={allow}: {type(e).__name__} -> {str(e)[:160]}")
    print("   prod share:", rows(eng, "SELECT * FROM share"))
