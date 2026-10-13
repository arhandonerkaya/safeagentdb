"""ChangeSet.fingerprint uses json.dumps(default=str): bytes b'x' and the text "b'x'"
serialise identically. Can the sandbox change after review and still match?"""
from typing import Any
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class F(SafeModel):
    __table_name__ = "files"; id: int; user_id: int; payload: Any

for decl in ("BLOB",):
    eng = prod([f"CREATE TABLE files (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, payload {decl})",
                "INSERT INTO files VALUES (1, 42, NULL)"])
    with ShadowDB(eng, tables=["files"], tenant_id=42) as sb:
        sb.execute("UPDATE files SET payload = :v WHERE id = 1", {"v": b"x"})
        reviewed = sb.diff()
        print(f"[{decl or 'untyped'}] reviewed new payload: {reviewed.diffs[0].new['payload']!r}")
        sb.execute("UPDATE files SET payload = :v WHERE id = 1", {"v": "b'x'"})
        try:
            now = sb.diff()
            print(f"    after change: {now.diffs[0].new['payload']!r}; same fingerprint: {now.fingerprint == reviewed.fingerprint}")
            print("    commit:", sb.commit_to_production(changeset=reviewed))
        except Exception as e:
            print(f"    raised {type(e).__module__}.{type(e).__name__} (SafeAgentDBError: {isinstance(e, SafeAgentDBError)}): {e}")
    print("    prod:", rows(eng, "SELECT id, payload, typeof(payload) FROM files"))
