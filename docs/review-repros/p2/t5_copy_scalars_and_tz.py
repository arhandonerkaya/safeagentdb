"""An agent duplicates rows without changing any value."""
from typing import Any
from _h import *
from safeagentdb import ShadowDB, SafeModel

class P(SafeModel):
    __table_name__ = "prefs"; id: int; user_id: int; code: Any; at: Any

eng = prod(["CREATE TABLE prefs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, code JSON, at DATETIME)",
            """INSERT INTO prefs VALUES (1, 42, '"123"', '2024-01-01 10:00:00+02:00')""",
            """INSERT INTO prefs VALUES (2, 42, '"null"', '2024-01-01T10:00:00Z')"""])
with ShadowDB(eng, tables=["prefs"], tenant_id=42) as sb:
    sb.execute("INSERT INTO prefs SELECT id + 10, user_id, code, at FROM prefs")
    cs = sb.diff()
    print("is_valid", cs.is_valid, "|", banner(cs))
    for d in cs.diffs:
        print(f"  INSERT {d.pk}: code shown {d.new['code']!r} -> writes {d.logical_row(d.new)['code']!r}; at {d.new['at']!r}")
    print("commit wrote", sb.commit_to_production(changeset=cs))
for r in rows(eng, "SELECT id, code, typeof(code), at FROM prefs ORDER BY id"): print("  prod", r)
