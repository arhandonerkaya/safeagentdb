"""The agent changes each typed column. Production's stored value is compared with
what SQLAlchemy writes for the same Python value directly (the control)."""
import datetime as dt, decimal
from typing import Any
from _h import *
from sqlalchemy import MetaData, Table, Column, Integer, Text, Date, DateTime, Numeric, Boolean, LargeBinary, String, JSON, update
from safeagentdb import ShadowDB, SafeModel, SafeAgentDBError
from _fixture import fresh, raw, R

md = MetaData()
T = Table("r", md, Column("id", Integer, primary_key=True), Column("user_id", Integer), Column("note", Text),
          Column("d", Date), Column("ts", DateTime), Column("tstz", DateTime(timezone=True)),
          Column("amount", Numeric(10, 2)), Column("flag", Boolean), Column("blob", LargeBinary),
          Column("s", String(20)), Column("js", JSON))

def stored(eng, i, col):
    with eng.connect() as c:
        return c.exec_driver_sql(f"SELECT {col}, typeof({col}) FROM r WHERE id={i}").fetchone()

# (row, column, value the agent binds, the Python value it means)
cases = [
    (1, "d", "2025-03-01", dt.date(2025, 3, 1)),
    (1, "ts", "2025-03-01 08:30:00", dt.datetime(2025, 3, 1, 8, 30)),
    (3, "amount", "99.95", decimal.Decimal("99.95")),
    (3, "flag", False, False),
    (4, "blob", b"\x01\x02", b"\x01\x02"),
    (4, "s", "", ""),
    (5, "s", None, None),
    (6, "js", '{"z": [1, {"y": null}]}', {"z": [1, {"y": None}]}),
    (6, "js", None, None),
    (6, "js", '"just a string"', "just a string"),
    (11, "ts", "2025-03-01 08:30:00", dt.datetime(2025, 3, 1, 8, 30)),
    (14, "flag", False, False),
]
for i, col, bound, meant in cases:
    eng = t1_fresh = fresh()
    with ShadowDB(eng, tables=["r"], tenant_id=42) as sb:
        sb.execute(f"UPDATE r SET {col} = :v WHERE id = {i}", {"v": bound})
        cs = sb.diff()
        d = cs.diffs[0] if cs.diffs else None
        shown = (d.changed_columns(), repr(d.logical_row(d.new)[col])) if d else ("no diff", "")
        try: res = f"wrote {sb.commit_to_production(changeset=cs)}"
        except SafeAgentDBError as e: res = f"{type(e).__name__}: {str(e)[:120]}"
    got = stored(eng, i, col)
    ctl = fresh()
    with ctl.begin() as c: c.execute(update(T).where(T.c.id == i).values({col: meant}))
    want = stored(ctl, i, col)
    print(f"row {i} {col} <- {bound!r}: diff {shown} | {res}\n    prod {got}  control {want}  {'OK' if got == want else 'MISMATCH'}")
