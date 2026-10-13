"""README: 'MySQL / MariaDB ... ENUM, YEAR, TINYINT mapped'. TINYINT is remapped to String(255)."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class S(SafeModel):
    __table_name__ = "settings"; id: int; user_id: int; active: int; note: str

eng = prod(["CREATE TABLE settings (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
            " active TINYINT NOT NULL, note TEXT NOT NULL)",
            "INSERT INTO settings VALUES (1, 42, 1, 'n')"])
with ShadowDB(eng, tables=["settings"], tenant_id=42) as sb:
    print("reflected type:", type(sb._prod_metadata.tables["settings"].c.active.type).__name__,
          "| sandbox type:", type(sb._sandbox_metadata.tables["settings"].c.active.type).__name__)
    print("sandbox value:", repr(sb._original_snapshot["settings"][0]["active"]))
    sb.execute("UPDATE settings SET note = 'edited' WHERE id = 1")   # active untouched
    cs = sb.diff()
    print("is_valid:", cs.is_valid, "| message:", cs.validate_all()[0][2])
    try: sb.commit_to_production(changeset=cs)
    except Exception as e: print("commit:", type(e).__module__, type(e).__name__)
