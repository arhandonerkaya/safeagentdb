"""What the per-row validation message says."""
from typing import Literal
from _h import *
from safeagentdb import ShadowDB, SafeModel

class T(SafeModel):
    __table_name__ = "tasks"; id: int; user_id: int; title: str; status: Literal["todo", "done"]

eng = prod(["CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL)",
            "INSERT INTO tasks VALUES (1, 42, 'one', 'todo')"])
with ShadowDB(eng, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("UPDATE tasks SET status = 'yolo' WHERE id = 1")
    print("message:", repr(sb.diff().validate_all()[0][2]))
