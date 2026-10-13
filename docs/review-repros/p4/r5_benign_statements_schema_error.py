"""_assert_schema_unchanged compares all of sqlite_master, so statements that
change no table the diff reads still make diff()/commit raise SchemaError."""
from _h import *
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
which()

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str

for stmt in ["ANALYZE", "CREATE INDEX scratch_idx ON tasks(title)", "CREATE VIEW v AS SELECT * FROM tasks",
             "CREATE TEMP TABLE scratch AS SELECT * FROM tasks"]:
    eng = prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT)",
                "INSERT INTO tasks VALUES (1, 42, 'a')"])
    with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
        sb.execute("UPDATE tasks SET title = 'b' WHERE id = 1")
        sb.execute(stmt)
        try:
            cs = sb.diff()
            print(f"{stmt!r}: diff ok, is_valid={cs.is_valid}")
        except SafeAgentDBError as e:
            print(f"{stmt!r}: {type(e).__name__} -> {str(e)[:130]}")
