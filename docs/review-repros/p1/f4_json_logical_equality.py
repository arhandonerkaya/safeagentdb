"""compute_diff compares raw sandbox text; changed_columns() compares parsed JSON
with Python ==, where 1 == True == 1.0. Two outcomes are checked."""
from typing import Any
from _h import *
from safeagentdb import ShadowDB, SafeModel

class Doc(SafeModel):
    __table_name__ = "docs"; id: int; user_id: int; data: Any

eng = prod([
    "CREATE TABLE docs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data JSON)",
    """INSERT INTO docs VALUES (1, 42, '{"enabled": 1}'), (2, 42, '{"a": 1}')""",
])
with ShadowDB(eng, tables=["docs"], tenant_id=42) as sb:
    print("converted:", sb._converted_columns)
    sb.execute("UPDATE docs SET data = :v WHERE id = 1", {"v": '{"enabled": true}'})  # a real change
    sb.execute("UPDATE docs SET data = :v WHERE id = 2", {"v": '{"a":1}'})          # no-op rewrite
    cs = sb.diff()
    for d in cs.diffs:
        print(f"row {d.pk}: {d.diff_type.value}, changed_columns={d.changed_columns()}")
    print("summary:", cs.summary, "is_valid:", cs.is_valid)
    print(cs._render_plain())
    print("commit wrote", sb.commit_to_production(changeset=cs))
print("prod:", rows(eng, "SELECT id, data FROM docs"))
