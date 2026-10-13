"""JSON scalars: the string "123", true, and null, stored in a JSON column."""
from typing import Any, Optional
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError

class Prefs(SafeModel):
    __table_name__ = "prefs"; id: int; user_id: int; note: str
    code: Optional[str]      # documented as a JSON string
    enabled: Optional[bool]  # documented as a JSON boolean

def fresh():
    return prod(["CREATE TABLE prefs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, note TEXT NOT NULL,"
                 " code JSON, enabled JSON)",
                 """INSERT INTO prefs VALUES (1, 42, 'n', '"00123"', 'true')"""])

eng = fresh()
print("prod:", rows(eng, "SELECT code, typeof(code), enabled FROM prefs"))
with ShadowDB(eng, tables=["prefs"], tenant_id=42) as sb:
    print("sandbox:", sb.query("SELECT code, enabled FROM prefs"))
    sb.execute("UPDATE prefs SET note = 'edited' WHERE id = 1")      # neither JSON column touched
    cs = sb.diff()
    d = cs.diffs[0]
    print("changed:", d.changed_columns(), "| logical code/enabled:", repr(d.logical_row(d.new)["code"]), repr(d.logical_row(d.new)["enabled"]))
    print("is_valid:", cs.is_valid, "|", banner(cs))
    print("row message:", cs.validate_all()[0][2])
    try: sb.commit_to_production(changeset=cs)
    except SafeAgentDBError as e: print("commit raised", type(e).__name__)
    except Exception as e: print("commit raised (outside hierarchy)", type(e).__module__, type(e).__name__)

class PrefsAny(SafeModel):
    __table_name__ = "prefs"; id: int; user_id: int; note: str; code: Any; enabled: Any

eng = fresh()
with ShadowDB(eng, tables=["prefs"], tenant_id=42) as sb:
    # The agent duplicates the row without touching its values.
    sb.execute("INSERT INTO prefs SELECT 2, user_id, note, code, enabled FROM prefs WHERE id = 1")
    cs = sb.diff()
    print("\ncopy: is_valid", cs.is_valid, "|", banner(cs))
    for col, old, new in [r for r in [(c, None, v) for c, v in cs.diffs[0].new.items()] if r[0] in ("code", "enabled")]:
        print(f"  diff shows {col} = {new!r}")
    print("commit wrote", sb.commit_to_production(changeset=cs))
print("prod:", rows(eng, "SELECT id, code, enabled FROM prefs"))
