"""Does the key-change trigger see a key changed through the rowid alias?"""
from _h import *
from safeagentdb import ShadowDB, SafeModel
which()
class P(SafeModel):
    __table_name__ = "parent"; id: int; user_id: int
eng = prod(["CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
            "INSERT INTO parent VALUES (1, 42)"])
with ShadowDB(eng, tables=["parent"], tenant_id=42) as sb:
    sb.execute("UPDATE parent SET rowid = 5 WHERE id = 1")
    cs = sb.diff()
    print("diffs:", [(d.diff_type.value, d.pk) for d in cs.diffs], "is_valid:", cs.is_valid)
    print("blocking:", [e[:80] for e in cs.blocking_errors])
